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

from .. import boundaries, procedures
from .._version import __version__
from ..errors import ProviderUnavailableError, UnsupportedCaseError
from ..geometry import ImportedSurface
from ..model import Step
from ..provenance import content_fingerprint, file_sha256
from ..results import (
    Artifact,
    Check,
    FieldRecord,
    History,
    Quantity,
    SimulationResult,
)
from .base import ProviderDescriptor
from .openfoam import (
    _analysis_sha256,
    _control_dict as _flow_control_dict,
    _fv_schemes as _flow_fv_schemes,
    _fv_solution as _flow_fv_solution,
    _header,
    _latest_time_directory,
    _mesh_quality_quantities,
    _read_scalar_series,
    _runtime_version,
    _stop_timed_out_container,
    _transport_properties,
    _turbulence_properties,
    _write_mesh_manifest,
)


_CAPABILITY = "openfoam.imported-surface-mesh"
_FLOW_CAPABILITY = "openfoam.steady-laminar-imported-surface"
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


def _flow_velocity_field(step: Step) -> str:
    domain = step.model.domain
    assert isinstance(domain, ImportedSurface)
    conditions = step.model.boundary_conditions
    inlet_name = next(name for name, role in domain.boundary_roles if role == "inlet")
    inlet = conditions[inlet_name]
    assert isinstance(inlet, boundaries.VelocityInlet)
    blocks: list[str] = []
    for name, role in domain.boundary_roles:
        condition = conditions[name]
        if role == "inlet":
            assert isinstance(condition, boundaries.VelocityInlet)
            body = f"type fixedValue;\n        value uniform {_foam_point(condition.velocity)};"
        elif role == "outlet":
            body = "type zeroGradient;"
        elif role == "wall" and isinstance(condition, boundaries.NoSlipWall):
            body = "type noSlip;"
        elif role == "wall" and isinstance(condition, boundaries.SlipWall):
            body = "type slip;"
        elif role == "symmetry":
            body = "type symmetry;"
        else:
            body = "type empty;"
        blocks.append(f"    {name}\n    {{\n        {body}\n    }}")
    return (
        _header(object_name="U", class_name="volVectorField", location="0")
        + f"""dimensions [0 1 -1 0 0 0 0];
internalField uniform {_foam_point(inlet.velocity)};
boundaryField
{{
{chr(10).join(blocks)}
}}
"""
    )


def _flow_pressure_field(step: Step) -> str:
    domain = step.model.domain
    assert isinstance(domain, ImportedSurface)
    conditions = step.model.boundary_conditions
    blocks: list[str] = []
    for name, role in domain.boundary_roles:
        condition = conditions[name]
        if role == "outlet":
            assert isinstance(condition, boundaries.PressureOutlet)
            value = condition.gauge_pressure / step.model.fluid.density
            body = f"type fixedValue;\n        value uniform {_foam_scalar(value)};"
        elif role in {"symmetry", "empty"}:
            body = f"type {'symmetry' if role == 'symmetry' else 'empty'};"
        else:
            body = "type zeroGradient;"
        blocks.append(f"    {name}\n    {{\n        {body}\n    }}")
    return (
        _header(object_name="p", class_name="volScalarField", location="0")
        + f"""dimensions [0 2 -2 0 0 0 0];
internalField uniform 0;
boundaryField
{{
{chr(10).join(blocks)}
}}
"""
    )


def _write_imported_flow_files(
    step: Step, prepared: PreparedImportedMesh
) -> PreparedImportedMesh:
    domain = step.model.domain
    assert isinstance(domain, ImportedSurface)
    inlet = next(name for name, role in domain.boundary_roles if role == "inlet")
    outlet = next(name for name, role in domain.boundary_roles if role == "outlet")
    rendered = {
        "0/U": _flow_velocity_field(step),
        "0/p": _flow_pressure_field(step),
        "constant/transportProperties": _transport_properties(
            step.model.fluid.kinematic_viscosity
        ),
        "constant/turbulenceProperties": _turbulence_properties(turbulent=False),
        "system/controlDict": _flow_control_dict(
            step.procedure.maximum_iterations,
            inlet=inlet,
            outlet=outlet,
            turbulent=False,
            compress=True,
        ),
        "system/fvSchemes": _flow_fv_schemes(turbulent=False),
        "system/fvSolution": _flow_fv_solution(
            step.procedure.relative_tolerance,
            turbulent=False,
        ),
    }
    for relative, content in sorted(rendered.items()):
        path = prepared.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    hashes = {
        path.relative_to(prepared.directory).as_posix(): file_sha256(path)
        for path in sorted(prepared.directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    case_sha256 = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    updated = PreparedImportedMesh(
        directory=prepared.directory,
        mesh_plan=prepared.mesh_plan,
        case_sha256=case_sha256,
        files=hashes,
    )
    (prepared.directory / "agentcfd-imported-flow-case.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.openfoam-imported-flow-case/0.1",
                "capability": _FLOW_CAPABILITY,
                "model_sha256": step.model.fingerprint(),
                "analysis_sha256": _analysis_sha256(step),
                **updated.to_dict(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return updated


class OpenFOAMImportedProvider:
    """Bounded steady laminar solver for a checked imported fluid volume."""

    def __init__(
        self,
        *,
        source: str | Path,
        case_directory: str | Path | None = None,
        container_image: str | None = None,
        timeout_seconds: float = 3600.0,
    ) -> None:
        self.source = Path(source)
        self.case_directory = None if case_directory is None else Path(case_directory)
        self.container_image = str(container_image).strip() if container_image else None
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be positive and finite.")
        self.timeout_seconds = float(timeout_seconds)

    def descriptor(self) -> ProviderDescriptor:
        if self.container_image:
            available = shutil.which("docker") is not None
        else:
            available = all(
                shutil.which(name) is not None
                for name in ("blockMesh", "snappyHexMesh", "checkMesh", "simpleFoam")
            )
        return ProviderDescriptor(
            name="openfoam",
            version=self.container_image or "externally-managed",
            license="GPL-3.0-or-later (external program)",
            available=available,
            execution_boundary=(
                "filesystem-and-container-subprocess"
                if self.container_image
                else "filesystem-and-subprocess"
            ),
            capabilities=(_CAPABILITY, _FLOW_CAPABILITY),
        )

    def validate(self, step: Step) -> None:
        domain = step.model.domain
        if not isinstance(domain, ImportedSurface):
            raise UnsupportedCaseError(
                "Imported flow provider requires ImportedSurface geometry."
            )
        plan_imported_mesh(step)
        study = step.model.study
        if (
            not study.steady
            or study.compressible
            or study.energy
            or study.reacting
            or not study.laminar
        ):
            raise UnsupportedCaseError(
                "First imported flow slice supports steady incompressible isothermal laminar flow only."
            )
        if not isinstance(step.procedure, procedures.SteadyProcedure):
            raise UnsupportedCaseError(
                "First imported flow slice requires procedures.steady()."
            )
        if step.initialization is not None:
            raise UnsupportedCaseError(
                "First imported flow slice currently requires default initialization."
            )
        roles = dict(domain.boundary_roles)
        inlet_names = [name for name, role in roles.items() if role == "inlet"]
        outlet_names = [name for name, role in roles.items() if role == "outlet"]
        if len(inlet_names) != 1 or len(outlet_names) != 1:
            raise UnsupportedCaseError(
                "First imported flow slice requires exactly one inlet and one outlet."
            )
        conditions = step.model.boundary_conditions
        if not isinstance(conditions[inlet_names[0]], boundaries.VelocityInlet):
            raise UnsupportedCaseError(
                "Imported geometry requires boundaries.velocity_inlet((ux, uy, uz)); direction is never guessed."
            )
        if not isinstance(conditions[outlet_names[0]], boundaries.PressureOutlet):
            raise UnsupportedCaseError(
                "First imported flow slice requires a pressure_outlet."
            )
        for name, role in domain.boundary_roles:
            condition = conditions[name]
            if role == "wall" and not isinstance(
                condition, (boundaries.NoSlipWall, boundaries.SlipWall)
            ):
                raise UnsupportedCaseError(
                    f"Imported wall {name!r} requires no-slip or slip intent."
                )
            if (
                role == "wall"
                and isinstance(condition, boundaries.NoSlipWall)
                and condition.roughness not in {None, 0.0}
            ):
                raise UnsupportedCaseError(
                    "Wall roughness is not lowered in the first imported laminar slice."
                )
            if role in {"symmetry", "empty"} and not isinstance(
                condition, boundaries.Symmetry
            ):
                raise UnsupportedCaseError(
                    f"Imported {role} patch {name!r} requires boundaries.symmetry()."
                )
        unsupported_fields = set(step.output.fields) - {
            "fluid.velocity",
            "fluid.pressure",
        }
        unsupported_histories = set(step.output.histories) - {
            "flow.mass_balance",
            "flow.pressure_drop",
        }
        if unsupported_fields or unsupported_histories or step.output.reports:
            raise UnsupportedCaseError(
                "First imported flow slice supports velocity/pressure fields and mass-balance/pressure-drop histories only."
            )

    def prepare(self, step: Step) -> PreparedImportedMesh:
        step.model.validate()
        self.validate(step)
        if self.case_directory is None:
            raise ValueError("Imported OpenFOAM case_directory is required.")
        prepared = prepare_imported_mesh(
            step,
            source=self.source,
            directory=self.case_directory,
        )
        return _write_imported_flow_files(step, prepared)

    def _run_simple_foam(self, case: Path) -> tuple[int, float, str]:
        executable = shutil.which("docker" if self.container_image else "simpleFoam")
        if executable is None:
            raise ProviderUnavailableError(
                "Imported OpenFOAM flow execution requires "
                + ("docker." if self.container_image else "simpleFoam.")
            )
        log_path = case / "log.simpleFoam"
        cidfile = case / ".agentcfd-simpleFoam.cid" if self.container_image else None
        started = time.monotonic()
        try:
            with log_path.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(
                    _mesh_argv(
                        "simpleFoam",
                        executable,
                        case,
                        container_image=self.container_image,
                        cidfile=cidfile,
                    ),
                    check=False,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            code = completed.returncode
        except subprocess.TimeoutExpired:
            note = f"AgentCFD timeout after {self.timeout_seconds:g} seconds.\n"
            if cidfile is not None:
                note += _stop_timed_out_container(executable, cidfile)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(note)
            code = -124
        finally:
            duration = time.monotonic() - started
            if cidfile is not None:
                cidfile.unlink(missing_ok=True)
        return code, duration, log_path.read_text(encoding="utf-8", errors="replace")

    def run(self, step: Step) -> SimulationResult:
        prepared = self.prepare(step)
        mesh_result = execute_imported_mesh(
            prepared,
            container_image=self.container_image,
            timeout_seconds=self.timeout_seconds,
        )
        solver_code = None
        solver_duration = 0.0
        solver_log = ""
        if mesh_result.accepted:
            solver_code, solver_duration, solver_log = self._run_simple_foam(
                prepared.directory
            )
        process_ok = mesh_result.accepted and solver_code == 0
        reached_end = process_ok and bool(re.search(r"(?m)^End\s*$", solver_log))
        converged_marker = bool(
            re.search(r"SIMPLE solution converged in\s+\d+\s+iterations", solver_log)
        )
        inlet_flow = _read_scalar_series(prepared.directory, "agentcfd_inlet_flow")
        outlet_flow = _read_scalar_series(prepared.directory, "agentcfd_outlet_flow")
        shared = sorted(set(inlet_flow) & set(outlet_flow))
        imbalance = tuple(
            abs(inlet_flow[t] + outlet_flow[t])
            / max(abs(inlet_flow[t]), abs(outlet_flow[t]), 1.0e-30)
            for t in shared
        )
        inlet_pressure = _read_scalar_series(
            prepared.directory, "agentcfd_inlet_pressure"
        )
        outlet_pressure = _read_scalar_series(
            prepared.directory, "agentcfd_outlet_pressure"
        )
        pressure_times = sorted(set(inlet_pressure) & set(outlet_pressure))
        pressure_drop = tuple(
            (inlet_pressure[t] - outlet_pressure[t]) * step.model.fluid.density
            for t in pressure_times
        )
        quantities = _mesh_quality_quantities(
            (prepared.directory / "log.checkMesh").read_text(
                encoding="utf-8", errors="replace"
            )
        )
        quantities["runtime.simpleFoam.wall_seconds"] = Quantity(
            solver_duration, "s", kind="runtime_metric"
        )
        if imbalance:
            quantities["flow.relative_mass_imbalance"] = Quantity(imbalance[-1], "1")
        if pressure_drop:
            quantities["flow.pressure_drop"] = Quantity(pressure_drop[-1], "Pa")
        histories: dict[str, History] = {}
        if imbalance:
            histories["flow.relative_mass_imbalance"] = History(
                tuple(shared), imbalance, unit="1"
            )
        if pressure_drop:
            histories["flow.pressure_drop"] = History(
                tuple(pressure_times), pressure_drop, unit="Pa"
            )
        mesh_sha256, mesh_manifest = _write_mesh_manifest(prepared.directory)
        fields: dict[str, FieldRecord] = {}
        latest = _latest_time_directory(prepared.directory)
        if latest is not None:
            for native, canonical, unit, components in (
                ("U", "fluid.velocity", "m/s", ("x", "y", "z")),
                ("p", "fluid.pressure", "m^2/s^2", ()),
            ):
                path = latest / native
                if canonical in step.output.fields and path.is_file():
                    fields[native] = FieldRecord(
                        unit=unit,
                        location="cell",
                        artifact=str(path),
                        components=components,
                        mesh_sha256=mesh_sha256,
                    )
        requested_history_map = {
            "flow.mass_balance": "flow.relative_mass_imbalance",
            "flow.pressure_drop": "flow.pressure_drop",
        }
        missing = [
            name
            for name in step.output.histories
            if requested_history_map[name] not in histories
        ]
        requested_field_map = {"fluid.velocity": "U", "fluid.pressure": "p"}
        missing.extend(
            name
            for name in step.output.fields
            if requested_field_map[name] not in fields
        )
        artifacts = {
            "case_manifest": Artifact.from_path(
                prepared.directory / "agentcfd-imported-flow-case.json",
                role="input-manifest",
                media_type="application/json",
            ),
            **{
                f"log_{path.name.removeprefix('log.')}": Artifact.from_path(
                    path, role="execution-log", media_type="text/plain"
                )
                for path in sorted(prepared.directory.glob("log.*"))
            },
        }
        if mesh_manifest is not None:
            artifacts["mesh_manifest"] = Artifact.from_path(
                mesh_manifest, role="mesh-manifest", media_type="application/json"
            )
        runtime_version = _runtime_version(
            {
                path.name: path.read_text(encoding="utf-8", errors="replace")
                for path in prepared.directory.glob("log.*")
            },
            self.descriptor().version,
        )
        mass_ok = bool(imbalance) and imbalance[-1] <= 1.0e-4
        checks = (
            Check(
                "openfoam-process",
                process_ok,
                value=f"simpleFoam={solver_code}; mesh={mesh_result.accepted}",
                limit="mesh accepted and simpleFoam return code zero",
                kind="runtime",
            ),
            Check(
                "solver-completion-marker",
                reached_end,
                value="found" if reached_end else "missing",
                limit="simpleFoam log ends with End",
                kind="runtime",
            ),
            Check(
                "solver-convergence-marker",
                converged_marker,
                value="found" if converged_marker else "missing",
                limit="SIMPLE residualControl convergence marker",
                observable="provider.numerical_convergence",
            ),
            Check(
                "mesh-quality",
                mesh_result.accepted,
                value="accepted" if mesh_result.accepted else "failed",
                limit="all imported mesh native and quality gates pass",
                observable="mesh.quality",
            ),
            Check(
                "steady-mass-balance",
                mass_ok,
                value=imbalance[-1] if imbalance else None,
                limit=1.0e-4,
                observable="flow.mass_balance",
            ),
            Check(
                "mesh-identity",
                mesh_sha256 is not None,
                value=mesh_sha256 or "missing",
                limit="content-addressed polyMesh exists",
                observable="mesh.identity",
            ),
            Check(
                "openfoam-runtime-version",
                "2606" in runtime_version,
                value=runtime_version,
                limit="OpenCFD v2606",
                kind="runtime",
            ),
            Check(
                "requested-output-completeness",
                not missing,
                value="complete" if not missing else ", ".join(missing),
                limit="all requested fields and histories recovered",
                kind="runtime",
            ),
        )
        return SimulationResult(
            status="completed" if process_ok else "failed",
            converged=reached_end and converged_marker and mass_ok,
            provider="openfoam",
            quantities=quantities,
            histories=histories,
            fields=fields,
            artifacts=artifacts,
            checks=checks,
            scientific_inputs=step.to_dict(),
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": step.model.fingerprint(),
                "analysis_sha256": _analysis_sha256(step),
                "case_sha256": prepared.case_sha256,
                "mesh_sha256": mesh_sha256,
                "provider_capability": _FLOW_CAPABILITY,
                "runtime_version": runtime_version,
            },
            messages=(
                "Experimental steady laminar imported-volume workflow; accepted means numerical/workflow gates passed, not physical validation.",
            ),
            name=step.model.name,
        )


__all__ = [
    "ImportedMeshPlan",
    "ImportedMeshResult",
    "OpenFOAMImportedProvider",
    "PreparedImportedMesh",
    "plan_imported_mesh",
    "prepare_imported_mesh",
    "execute_imported_mesh",
]
