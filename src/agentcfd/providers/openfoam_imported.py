"""Deterministic, budgeted snappyHexMesh preparation for imported fluid volumes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import ProviderUnavailableError, UnsupportedCaseError
from ..geometry import ImportedSurface
from ..model import Step
from ..provenance import content_fingerprint, file_sha256
from .openfoam import _header, _mesh_quality_quantities, _stop_timed_out_container


_CAPABILITY = "openfoam.imported-surface-mesh"
_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_ROLES = {"inlet", "outlet", "wall", "symmetry", "empty"}


def _foam_scalar(value: float) -> str:
    return f"{value:.17g}"


def _foam_point(point: tuple[float, float, float]) -> str:
    return "(" + " ".join(_foam_scalar(value) for value in point) + ")"


def _patch_type(role: str) -> str:
    return {
        "wall": "wall",
        "symmetry": "symmetryPlane",
        "empty": "empty",
    }.get(role, "patch")


def _surface_level(base_size: float, target_size: float) -> int:
    return max(0, math.ceil(math.log2(base_size / target_size) - 1.0e-12))


@dataclass(frozen=True, slots=True)
class ImportedMeshPlan:
    """Inspectible lowering decisions before any OpenFOAM process starts."""

    source_sha256: str
    background_bounds_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    background_cells: tuple[int, int, int]
    background_cell_count: int
    maximum_cells: int
    surface_levels: tuple[tuple[str, int], ...]
    interior_point_m: tuple[float, float, float]
    quality_limits: tuple[tuple[str, float], ...]
    add_layers: bool

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "schema": "agentcfd.openfoam-imported-mesh-plan/0.1",
            "capability": _CAPABILITY,
            "source_sha256": self.source_sha256,
            "background_bounds_m": [list(point) for point in self.background_bounds_m],
            "background_cells": list(self.background_cells),
            "background_cell_count": self.background_cell_count,
            "maximum_cells": self.maximum_cells,
            "surface_levels": dict(self.surface_levels),
            "interior_point_m": list(self.interior_point_m),
            "quality_limits": dict(self.quality_limits),
            "add_layers": self.add_layers,
        }
        record["plan_sha256"] = content_fingerprint(record)
        return record


@dataclass(frozen=True, slots=True)
class PreparedImportedMesh:
    directory: Path
    mesh_plan: ImportedMeshPlan
    case_sha256: str
    files: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.openfoam-imported-mesh-case/0.1",
            "directory": str(self.directory),
            "case_sha256": self.case_sha256,
            "files": dict(sorted(self.files.items())),
            "mesh_plan": self.mesh_plan.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ImportedMeshResult:
    directory: Path
    accepted: bool
    checks: tuple[dict[str, object], ...]
    quantities: dict[str, dict[str, object]]
    return_codes: dict[str, int]
    durations_seconds: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.openfoam-imported-mesh-result/0.1",
            "directory": str(self.directory),
            "accepted": self.accepted,
            "checks": list(self.checks),
            "quantities": self.quantities,
            "return_codes": self.return_codes,
            "durations_seconds": self.durations_seconds,
        }


def plan_imported_mesh(step: Step) -> ImportedMeshPlan:
    """Validate and resolve a bounded first imported-surface meshing slice."""

    domain = step.model.domain
    if not isinstance(domain, ImportedSurface):
        raise UnsupportedCaseError(
            "Imported meshing requires ImportedSurface geometry."
        )
    if step.mesh is None or step.mesh.method != "automatic":
        raise UnsupportedCaseError(
            "Imported meshing requires explicit meshing.automatic(...) intent."
        )
    if domain.interior_point_m is None:
        raise UnsupportedCaseError(
            "Imported meshing requires an explicit interior_point_m; it is never guessed."
        )
    invalid_words = sorted(
        name for name in domain.surface_names if _FOAM_WORD.fullmatch(name) is None
    )
    if invalid_words:
        raise UnsupportedCaseError(
            "Imported surface names must be OpenFOAM-compatible words: "
            + ", ".join(invalid_words)
            + "."
        )
    unsupported_roles = sorted(
        {role for _name, role in domain.boundary_roles} - _SUPPORTED_ROLES
    )
    if unsupported_roles:
        raise UnsupportedCaseError(
            "First imported meshing slice does not lower boundary roles: "
            + ", ".join(unsupported_roles)
            + "."
        )
    if step.mesh.boundary_layers:
        raise UnsupportedCaseError(
            "Boundary-layer addition is not enabled in the first imported meshing slice."
        )
    if step.mesh.growth_rate != 1.2:
        raise UnsupportedCaseError(
            "First imported meshing slice requires the default growth_rate=1.2."
        )

    base_size = step.mesh.base_size
    lower = tuple(value - 2.0 * base_size for value in domain.bounds_m[0])
    upper = tuple(value + 2.0 * base_size for value in domain.bounds_m[1])
    cells = tuple(
        max(1, math.ceil((high - low) / base_size - 1.0e-12))
        for low, high in zip(lower, upper)
    )
    background_count = math.prod(cells)
    if background_count > step.mesh.maximum_cells:
        raise UnsupportedCaseError(
            f"Background mesh requires {background_count} cells, exceeding explicit "
            f"maximum_cells={step.mesh.maximum_cells}."
        )
    local_sizes = {
        region: control.size
        for control in step.mesh.local_sizing
        for region in control.regions
    }
    levels = tuple(
        (
            name,
            max(1, _surface_level(base_size, local_sizes.get(name, base_size / 2.0))),
        )
        for name in domain.surface_names
    )
    return ImportedMeshPlan(
        source_sha256=domain.source_sha256,
        background_bounds_m=(lower, upper),
        background_cells=cells,
        background_cell_count=background_count,
        maximum_cells=step.mesh.maximum_cells,
        surface_levels=levels,
        interior_point_m=domain.interior_point_m,
        quality_limits=(
            (
                "maximum_non_orthogonality",
                step.mesh.quality.maximum_non_orthogonality,
            ),
            ("maximum_skewness", step.mesh.quality.maximum_skewness),
            ("maximum_aspect_ratio", step.mesh.quality.maximum_aspect_ratio),
        ),
        add_layers=False,
    )


def _block_mesh_dict(plan: ImportedMeshPlan) -> str:
    low, high = plan.background_bounds_m
    vertices = (
        (low[0], low[1], low[2]),
        (high[0], low[1], low[2]),
        (high[0], high[1], low[2]),
        (low[0], high[1], low[2]),
        (low[0], low[1], high[2]),
        (high[0], low[1], high[2]),
        (high[0], high[1], high[2]),
        (low[0], high[1], high[2]),
    )
    rendered_vertices = "\n".join(f"    {_foam_point(point)}" for point in vertices)
    nx, ny, nz = plan.background_cells
    return f"""{_header(object_name="blockMeshDict", class_name="dictionary")}convertToMeters 1;

vertices
(
{rendered_vertices}
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);

edges ();

boundary
(
    background
    {{
        type patch;
        faces
        (
            (0 4 7 3) (1 2 6 5) (0 1 5 4)
            (3 7 6 2) (0 3 2 1) (4 5 6 7)
        );
    }}
);

mergePatchPairs ();
"""


def _snappy_dict(domain: ImportedSurface, plan: ImportedMeshPlan, filename: str) -> str:
    region_map = "\n".join(
        f"            {name} {{ name {name}; }}" for name in domain.surface_names
    )
    region_controls = "\n".join(
        f"            {name}\n"
        "            {\n"
        f"                level ({level} {level});\n"
        f"                patchInfo {{ type {_patch_type(dict(domain.boundary_roles)[name])}; }}\n"
        "            }"
        for name, level in plan.surface_levels
    )
    quality = dict(plan.quality_limits)
    return f"""{_header(object_name="snappyHexMeshDict", class_name="dictionary")}castellatedMesh true;
snap true;
addLayers false;
singleRegionName false;

geometry
{{
    {filename}
    {{
        type triSurfaceMesh;
        name importedFluid;
        scale {_foam_scalar(domain.scale_to_m)};
        regions
        {{
{region_map}
        }}
    }}
}}

castellatedMeshControls
{{
    maxLocalCells {plan.maximum_cells};
    maxGlobalCells {plan.maximum_cells};
    minRefinementCells 0;
    maxLoadUnbalance 0.10;
    nCellsBetweenLevels 3;
    features ();
    refinementSurfaces
    {{
        importedFluid
        {{
            level (1 1);
            regions
            {{
{region_controls}
            }}
        }}
    }}
    resolveFeatureAngle 30;
    refinementRegions {{}}
    locationInMesh {_foam_point(plan.interior_point_m)};
    allowFreeStandingZoneFaces false;
}}

snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 30;
    nRelaxIter 5;
    nFeatureSnapIter 10;
    implicitFeatureSnap true;
    explicitFeatureSnap false;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes false;
    layers {{}}
    expansionRatio 1.2;
    finalLayerThickness 0.3;
    minThickness 0.1;
    nGrow 0;
    featureAngle 60;
    slipFeatureAngle 30;
    nRelaxIter 3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals 3;
    nSmoothThickness 10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle 90;
    nBufferCellsNoExtrude 0;
    nLayerIter 50;
}}

meshQualityControls
{{
    maxNonOrtho {_foam_scalar(quality["maximum_non_orthogonality"])};
    maxBoundarySkewness 20;
    maxInternalSkewness {_foam_scalar(quality["maximum_skewness"])};
    maxConcave 80;
    minVol 1e-13;
    minTetQuality 1e-15;
    minArea -1;
    minTwist 0.02;
    minDeterminant 0.001;
    minFaceWeight 0.05;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
}}

mergeTolerance 1e-6;
"""


def _control_dict() -> str:
    return f"""{_header(object_name="controlDict", class_name="dictionary")}application simpleFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 1;
deltaT 1;
writeControl timeStep;
writeInterval 1;
purgeWrite 0;
writeFormat binary;
writePrecision 10;
writeCompression off;
timeFormat general;
timePrecision 8;
runTimeModifiable false;
"""


def _fv_schemes() -> str:
    return (
        _header(object_name="fvSchemes", class_name="dictionary", location="system")
        + """ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
"""
    )


def _fv_solution() -> str:
    return (
        _header(object_name="fvSolution", class_name="dictionary", location="system")
        + """solvers {}
SIMPLE
{
    nNonOrthogonalCorrectors 0;
}
"""
    )


def prepare_imported_mesh(
    step: Step,
    *,
    source: str | Path,
    directory: str | Path,
) -> PreparedImportedMesh:
    """Write a deterministic mesh-only OpenFOAM case without running a process."""

    plan = plan_imported_mesh(step)
    domain = step.model.domain
    assert isinstance(domain, ImportedSurface)
    selected = Path(source)
    if not selected.is_file():
        raise FileNotFoundError(selected)
    if "sha256:" + file_sha256(selected) != domain.source_sha256:
        raise ValueError("Imported source bytes do not match model source_sha256.")
    target = Path(directory)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"Imported mesh directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    suffix = ".stl" if domain.source_format == "stl" else ".obj"
    filename = "imported-fluid" + suffix
    rendered = {
        "system/blockMeshDict": _block_mesh_dict(plan),
        "system/snappyHexMeshDict": _snappy_dict(domain, plan, filename),
        "system/controlDict": _control_dict(),
        "system/fvSchemes": _fv_schemes(),
        "system/fvSolution": _fv_solution(),
    }
    hashes: dict[str, str] = {}
    for relative, content in sorted(rendered.items()):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = content.encode("utf-8")
        path.write_bytes(payload)
        hashes[relative] = hashlib.sha256(payload).hexdigest()
    copied = target / "constant" / "triSurface" / filename
    copied.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(selected, copied)
    relative_surface = copied.relative_to(target).as_posix()
    hashes[relative_surface] = file_sha256(copied)
    case_sha256 = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    prepared = PreparedImportedMesh(
        directory=target,
        mesh_plan=plan,
        case_sha256=case_sha256,
        files=hashes,
    )
    manifest = target / "agentcfd-imported-mesh.json"
    manifest.write_text(
        json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return prepared


def _mesh_commands(container_image: str | None) -> dict[str, str | None]:
    executable = shutil.which("docker") if container_image else None
    if container_image:
        return {
            "blockMesh": executable,
            "snappyHexMesh-check": executable,
            "snappyHexMesh": executable,
            "checkMesh": executable,
        }
    return {
        "blockMesh": shutil.which("blockMesh"),
        "snappyHexMesh-check": shutil.which("snappyHexMesh"),
        "snappyHexMesh": shutil.which("snappyHexMesh"),
        "checkMesh": shutil.which("checkMesh"),
    }


def _mesh_argv(
    name: str,
    executable: str,
    case: Path,
    *,
    container_image: str | None,
    cidfile: Path | None,
) -> list[str]:
    utility = name.removesuffix("-check")
    options = (
        ["-checkGeometry", "-dry-run"]
        if name == "snappyHexMesh-check"
        else ["-overwrite"]
        if name == "snappyHexMesh"
        else ["-allGeometry", "-allTopology"]
        if name == "checkMesh"
        else []
    )
    if container_image is None:
        return [executable, "-case", str(case), *options]
    argv = [executable, "run", "--rm"]
    if cidfile is not None:
        argv.extend(("--cidfile", str(cidfile)))
    return [
        *argv,
        "-v",
        f"{case.resolve()}:/case",
        "-w",
        "/case",
        container_image,
        utility,
        "-case",
        "/case",
        *options,
    ]


def execute_imported_mesh(
    prepared: PreparedImportedMesh,
    *,
    container_image: str | None = None,
    timeout_seconds: float = 3600.0,
) -> ImportedMeshResult:
    """Run native geometry/dry-run/mesh/quality gates and publish a compact report."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("Imported mesh timeout_seconds must be positive and finite.")
    commands = _mesh_commands(container_image)
    missing = [name for name, command in commands.items() if command is None]
    if missing:
        raise ProviderUnavailableError(
            "Imported OpenFOAM meshing requires: " + ", ".join(missing)
        )
    return_codes: dict[str, int] = {}
    durations: dict[str, float] = {}
    logs: dict[str, str] = {}
    for name, executable in commands.items():
        assert executable is not None
        log_path = prepared.directory / f"log.{name}"
        cidfile = (
            prepared.directory / f".agentcfd-{name}.cid" if container_image else None
        )
        started = time.monotonic()
        try:
            with log_path.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(
                    _mesh_argv(
                        name,
                        executable,
                        prepared.directory,
                        container_image=container_image,
                        cidfile=cidfile,
                    ),
                    check=False,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout_seconds,
                )
            return_codes[name] = completed.returncode
        except subprocess.TimeoutExpired:
            note = f"AgentCFD timeout after {timeout_seconds:g} seconds.\n"
            if cidfile is not None:
                note += _stop_timed_out_container(executable, cidfile)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(note)
            return_codes[name] = -124
        finally:
            durations[name] = time.monotonic() - started
            if cidfile is not None:
                cidfile.unlink(missing_ok=True)
        logs[name] = log_path.read_text(encoding="utf-8", errors="replace")
        if return_codes[name] != 0:
            break

    checks: list[dict[str, object]] = []
    for name in commands:
        status = "passed" if return_codes.get(name) == 0 else "failed"
        if name not in return_codes:
            status = "not-run"
        checks.append(
            {
                "code": "IMPORTED_MESH_" + name.upper().replace("-", "_"),
                "status": status,
                "log": f"log.{name}",
            }
        )
    check_log = logs.get("checkMesh", "")
    observed = _mesh_quality_quantities(check_log)
    quantities = {
        name: quantity.as_record(name) for name, quantity in sorted(observed.items())
    }
    quality = dict(prepared.mesh_plan.quality_limits)
    policy_checks = (
        ("mesh.cell_count", prepared.mesh_plan.maximum_cells),
        ("mesh.maximum_non_orthogonality", quality["maximum_non_orthogonality"]),
        ("mesh.maximum_skewness", quality["maximum_skewness"]),
        ("mesh.maximum_aspect_ratio", quality["maximum_aspect_ratio"]),
    )
    for quantity_name, limit in policy_checks:
        quantity = observed.get(quantity_name)
        passed = quantity is not None and quantity.value <= limit
        checks.append(
            {
                "code": "IMPORTED_MESH_LIMIT_"
                + quantity_name.removeprefix("mesh.").upper(),
                "status": "passed" if passed else "failed",
                "observed": None if quantity is None else quantity.value,
                "limit": limit,
            }
        )
    accepted = (
        len(return_codes) == len(commands)
        and all(code == 0 for code in return_codes.values())
        and "Mesh OK" in check_log
        and all(check["status"] == "passed" for check in checks)
    )
    result = ImportedMeshResult(
        directory=prepared.directory,
        accepted=accepted,
        checks=tuple(checks),
        quantities=quantities,
        return_codes=return_codes,
        durations_seconds=durations,
    )
    (prepared.directory / "agentcfd-imported-mesh-result.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


__all__ = [
    "ImportedMeshPlan",
    "ImportedMeshResult",
    "PreparedImportedMesh",
    "plan_imported_mesh",
    "prepare_imported_mesh",
    "execute_imported_mesh",
]
