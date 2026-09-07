"""Deterministic, budgeted snappyHexMesh preparation for imported fluid volumes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .. import boundaries, engineering, outputs, procedures
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
    _read_y_plus_series,
    _runtime_version,
    _stop_timed_out_container,
    _transport_properties,
    _turbulence_properties,
    _write_mesh_manifest,
)
from .openfoam_reports import (
    REPORT_OPERATIONS,
    RESERVED_REPORT_NAMES,
    foam_name,
    recover_reports,
    render_report_functions,
    report_recovered,
)


_CAPABILITY = "openfoam.imported-surface-mesh"
_FLOW_CAPABILITY = "openfoam.steady-laminar-imported-surface"
_RANS_FLOW_CAPABILITY = "openfoam.steady-rans-imported-surface"
_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_ROLES = {"inlet", "outlet", "wall", "symmetry", "empty"}
_MINIMUM_WALL_FUNCTION_Y_PLUS = 30.0
_MAXIMUM_WALL_FUNCTION_Y_PLUS = 300.0
_MESH_COMMAND_NAMES = (
    "blockMesh",
    "snappyHexMesh-check",
    "snappyHexMesh",
    "checkMesh",
)


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


def _mesh_cache_key(prepared: PreparedImportedMesh, runtime_identity: str) -> str:
    return content_fingerprint(
        {
            "mesh_plan_sha256": prepared.mesh_plan.to_dict()["plan_sha256"],
            "runtime_identity": runtime_identity,
        }
    ).removeprefix("sha256:")


def _mesh_file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _restore_cached_mesh(
    prepared: PreparedImportedMesh,
    cache_root: Path | None,
    runtime_identity: str,
) -> ImportedMeshResult | None:
    """Restore only a byte-verified mesh produced by the exact mesh plan."""

    if cache_root is None:
        return None
    key = _mesh_cache_key(prepared, runtime_identity)
    candidate = cache_root / key
    manifest_path = candidate / "mesh-cache.json"
    mesh_root = candidate / "polyMesh"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "agentcfd.imported-mesh-cache/0.1"
        or payload.get("mesh_plan_sha256")
        != prepared.mesh_plan.to_dict()["plan_sha256"]
        or payload.get("runtime_identity") != runtime_identity
        or payload.get("accepted") is not True
        or not isinstance(payload.get("files"), dict)
        or not isinstance(payload.get("logs"), dict)
        or not mesh_root.is_dir()
    ):
        return None
    expected_files = payload["files"]
    if _mesh_file_hashes(mesh_root) != expected_files:
        return None
    expected_logs = payload["logs"]
    if set(expected_logs) != set(_MESH_COMMAND_NAMES) or any(
        file_sha256(candidate / f"log.{name}") != expected_logs[name]
        for name in _MESH_COMMAND_NAMES
        if (candidate / f"log.{name}").is_file()
    ):
        return None
    if any(
        not (candidate / f"log.{name}").is_file()
        for name in _MESH_COMMAND_NAMES
    ):
        return None
    result_record = payload.get("mesh_result")
    if not isinstance(result_record, dict):
        return None
    try:
        result = ImportedMeshResult(
            directory=prepared.directory,
            accepted=result_record["accepted"] is True,
            checks=tuple(result_record["checks"]),
            quantities=dict(result_record["quantities"]),
            return_codes={
                str(name): int(code)
                for name, code in dict(result_record["return_codes"]).items()
            },
            durations_seconds={
                str(name): float(value)
                for name, value in dict(result_record["durations_seconds"]).items()
            },
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not result.accepted or any(
        result.return_codes.get(name) != 0 for name in _MESH_COMMAND_NAMES
    ):
        return None
    target = prepared.directory / "constant" / "polyMesh"
    if target.exists():
        return None
    shutil.copytree(mesh_root, target)
    for name in _MESH_COMMAND_NAMES:
        cached_log = candidate / f"log.{name}"
        if cached_log.is_file():
            shutil.copy2(cached_log, prepared.directory / cached_log.name)
    return result


def _store_cached_mesh(
    prepared: PreparedImportedMesh,
    mesh_result: ImportedMeshResult,
    cache_root: Path | None,
    runtime_identity: str,
) -> None:
    """Atomically publish one accepted mesh for later operating points."""

    mesh_root = prepared.directory / "constant" / "polyMesh"
    if cache_root is None or not mesh_result.accepted or not mesh_root.is_dir():
        return
    cache_root.mkdir(parents=True, exist_ok=True)
    key = _mesh_cache_key(prepared, runtime_identity)
    target = cache_root / key
    temporary = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=cache_root))
    try:
        copied_mesh = temporary / "polyMesh"
        shutil.copytree(mesh_root, copied_mesh)
        logs: dict[str, str] = {}
        for name in _MESH_COMMAND_NAMES:
            log_path = prepared.directory / f"log.{name}"
            if log_path.is_file():
                copied_log = temporary / log_path.name
                shutil.copy2(log_path, copied_log)
                logs[name] = file_sha256(copied_log)
        if set(logs) != set(_MESH_COMMAND_NAMES):
            return
        payload = {
            "schema": "agentcfd.imported-mesh-cache/0.1",
            "mesh_plan_sha256": prepared.mesh_plan.to_dict()["plan_sha256"],
            "runtime_identity": runtime_identity,
            "accepted": True,
            "files": _mesh_file_hashes(copied_mesh),
            "logs": logs,
            "mesh_result": mesh_result.to_dict(),
        }
        (temporary / "mesh-cache.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target)
        temporary.replace(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


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
    assert isinstance(
        inlet,
        (
            boundaries.MassFlowInlet,
            boundaries.VelocityInlet,
            boundaries.TurbulentVelocityInlet,
        ),
    )
    initial_velocity = (
        (0.0, 0.0, 0.0)
        if isinstance(inlet, boundaries.MassFlowInlet)
        else inlet.velocity
    )
    blocks: list[str] = []
    for name, role in domain.boundary_roles:
        condition = conditions[name]
        if role == "inlet":
            assert isinstance(
                condition,
                (
                    boundaries.MassFlowInlet,
                    boundaries.VelocityInlet,
                    boundaries.TurbulentVelocityInlet,
                ),
            )
            if isinstance(condition, boundaries.MassFlowInlet):
                volume_flow = condition.mass_flow_rate / step.model.fluid.density
                body = (
                    "type flowRateInletVelocity;\n        "
                    f"volumetricFlowRate constant {_foam_scalar(volume_flow)};\n"
                    "        extrapolateProfile false;\n        "
                    "value uniform (0 0 0);"
                )
            else:
                body = (
                    "type fixedValue;\n        "
                    f"value uniform {_foam_point(condition.velocity)};"
                )
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
internalField uniform {_foam_point(initial_velocity)};
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


def _imported_turbulence_fields(step: Step) -> dict[str, str]:
    """Render k-omega SST fields for arbitrary confirmed patch names."""

    domain = step.model.domain
    assert isinstance(domain, ImportedSurface)
    inlet_name = next(name for name, role in domain.boundary_roles if role == "inlet")
    outlet_name = next(name for name, role in domain.boundary_roles if role == "outlet")
    inlet = step.model.boundary_conditions[inlet_name]
    assert isinstance(inlet, boundaries.TurbulentVelocityInlet)
    estimate = engineering.turbulence_inlet_from_intensity(
        mean_velocity=inlet.magnitude,
        intensity=inlet.turbulence_intensity,
        length_scale=inlet.turbulence_length_scale,
    )
    kinetic_energy = estimate.turbulent_kinetic_energy
    omega = estimate.specific_dissipation_rate

    def patches(field: str) -> str:
        blocks: list[str] = []
        for name, role in domain.boundary_roles:
            if role == "inlet":
                body = (
                    f"type fixedValue;\n        value uniform "
                    f"{_foam_scalar(kinetic_energy if field == 'k' else omega)};"
                    if field in {"k", "omega"}
                    else "type calculated;\n        value uniform 0;"
                )
            elif role == "outlet":
                body = (
                    "type zeroGradient;"
                    if field in {"k", "omega"}
                    else "type calculated;\n        value uniform 0;"
                )
            elif role == "wall":
                body = {
                    "k": (
                        "type kqRWallFunction;\n        value uniform "
                        f"{_foam_scalar(kinetic_energy)};"
                    ),
                    "omega": (
                        "type omegaWallFunction;\n        blending binomial;\n        "
                        f"value uniform {_foam_scalar(omega)};"
                    ),
                    "nut": "type nutUBlendedWallFunction;\n        value uniform 0;",
                }[field]
            elif role == "symmetry":
                body = "type symmetry;"
            else:
                body = "type empty;"
            blocks.append(f"    {name}\n    {{\n        {body}\n    }}")
        return "\n".join(blocks)

    return {
        "0/k": (
            _header(object_name="k", class_name="volScalarField", location="0")
            + f"""dimensions [0 2 -2 0 0 0 0];
internalField uniform {_foam_scalar(kinetic_energy)};
boundaryField
{{
{patches("k")}
}}
"""
        ),
        "0/omega": (
            _header(object_name="omega", class_name="volScalarField", location="0")
            + f"""dimensions [0 0 -1 0 0 0 0];
internalField uniform {_foam_scalar(omega)};
boundaryField
{{
{patches("omega")}
}}
"""
        ),
        "0/nut": (
            _header(object_name="nut", class_name="volScalarField", location="0")
            + f"""dimensions [0 2 -1 0 0 0 0];
internalField uniform 0;
boundaryField
{{
{patches("nut")}
}}
"""
        ),
    }


def _write_imported_flow_files(
    step: Step, prepared: PreparedImportedMesh
) -> PreparedImportedMesh:
    domain = step.model.domain
    assert isinstance(domain, ImportedSurface)
    inlet = next(name for name, role in domain.boundary_roles if role == "inlet")
    outlet = next(name for name, role in domain.boundary_roles if role == "outlet")
    turbulent = not step.model.study.laminar
    flow_capability = _RANS_FLOW_CAPABILITY if turbulent else _FLOW_CAPABILITY
    rendered = {
        "0/U": _flow_velocity_field(step),
        "0/p": _flow_pressure_field(step),
        "constant/transportProperties": _transport_properties(
            step.model.fluid.kinematic_viscosity
        ),
        "constant/turbulenceProperties": _turbulence_properties(
            turbulent=turbulent,
            turbulence_model=step.model.study.turbulence or "k-omega-sst",
        ),
        "system/controlDict": _flow_control_dict(
            step.procedure.maximum_iterations,
            inlet=inlet,
            outlet=outlet,
            turbulent=turbulent,
            compress=True,
            extra_functions=render_report_functions(step),
        ),
        "system/fvSchemes": _flow_fv_schemes(turbulent=turbulent),
        "system/fvSolution": _flow_fv_solution(
            step.procedure.relative_tolerance,
            turbulent=turbulent,
        ),
    }
    if turbulent:
        rendered.update(_imported_turbulence_fields(step))
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
                "capability": flow_capability,
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
        mesh_cache_directory: str | Path | None = None,
        container_image: str | None = None,
        timeout_seconds: float = 3600.0,
    ) -> None:
        self.source = Path(source)
        self.case_directory = None if case_directory is None else Path(case_directory)
        self.mesh_cache_directory = (
            None if mesh_cache_directory is None else Path(mesh_cache_directory)
        )
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
            capabilities=(_CAPABILITY, _FLOW_CAPABILITY, _RANS_FLOW_CAPABILITY),
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
            or study.turbulence not in {None, "k-omega-sst"}
        ):
            raise UnsupportedCaseError(
                "Imported flow supports steady incompressible isothermal laminar or "
                "k-omega-sst RANS flow only."
            )
        if (
            not study.laminar
            and study.wall_treatment != "blended-wall-functions"
        ):
            raise UnsupportedCaseError(
                "Imported k-omega-sst flow requires explicitly declared "
                "blended-wall-functions treatment."
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
        expected_inlet = (
            (boundaries.VelocityInlet, boundaries.MassFlowInlet)
            if study.laminar
            else (boundaries.TurbulentVelocityInlet,)
        )
        if not isinstance(conditions[inlet_names[0]], expected_inlet):
            raise UnsupportedCaseError(
                "Imported geometry requires "
                + (
                    "boundaries.velocity_inlet((ux, uy, uz)) or "
                    "boundaries.mass_flow_inlet(kg_per_s)."
                    if study.laminar
                    else "boundaries.turbulent_velocity_inlet((ux, uy, uz), "
                    "intensity=..., length_scale=...)."
                )
                + " Cartesian direction is never guessed; mass flow follows the "
                "inlet patch normal, and turbulence assumptions must be explicit."
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
                    "Wall roughness is not lowered in the imported flow slice."
                )
            if (
                not study.laminar
                and role == "wall"
                and not isinstance(condition, boundaries.NoSlipWall)
            ):
                raise UnsupportedCaseError(
                    "Imported RANS wall functions require no-slip wall intent."
                )
            if role in {"symmetry", "empty"} and not isinstance(
                condition, boundaries.Symmetry
            ):
                raise UnsupportedCaseError(
                    f"Imported {role} patch {name!r} requires boundaries.symmetry()."
                )
        supported_fields = {"fluid.velocity", "fluid.pressure"}
        supported_histories = {"flow.mass_balance", "flow.pressure_drop"}
        if not study.laminar:
            supported_fields.update(
                {
                    "turbulence.kinetic_energy",
                    "turbulence.specific_dissipation_rate",
                    "turbulence.kinematic_eddy_viscosity",
                }
            )
            supported_histories.add("wall.y_plus")
        unsupported_fields = set(step.output.fields) - supported_fields
        unsupported_histories = set(step.output.histories) - supported_histories
        if unsupported_fields or unsupported_histories:
            raise UnsupportedCaseError(
                "Imported flow received fields or histories outside its declared "
                "laminar/RANS output contract."
            )
        lowered_report_names = [foam_name(report.name) for report in step.output.reports]
        if (
            len(set(lowered_report_names)) != len(lowered_report_names)
            or set(lowered_report_names) & RESERVED_REPORT_NAMES
        ):
            raise UnsupportedCaseError(
                "Report names collide with each other or a reserved provider monitor "
                "after deterministic OpenFOAM name lowering."
            )
        for report in step.output.reports:
            if isinstance(report, outputs.PointProbe):
                if set(report.fields) - {"fluid.velocity", "fluid.pressure"}:
                    raise UnsupportedCaseError(
                        f"Probe {report.name!r} supports velocity and pressure only."
                    )
                minimum, maximum = domain.bounds_m
                if any(
                    value <= low or value >= high
                    for value, low, high in zip(report.location, minimum, maximum)
                ):
                    raise UnsupportedCaseError(
                        f"Probe {report.name!r} must lie strictly inside the imported "
                        "fluid-volume bounds."
                    )
            elif isinstance(report, outputs.SurfaceReport):
                if report.region not in domain.surface_names:
                    raise UnsupportedCaseError(
                        f"Surface report {report.name!r} references unknown region "
                        f"{report.region!r}."
                    )
                if report.field != "fluid.pressure":
                    raise UnsupportedCaseError(
                        f"Surface report {report.name!r} must currently reduce scalar "
                        "pressure; vector velocity needs an explicit component or "
                        "magnitude contract."
                    )
                if report.operation not in REPORT_OPERATIONS:
                    raise UnsupportedCaseError(
                        f"Surface report {report.name!r} requests an unsupported operation."
                    )
            elif isinstance(report, outputs.ForceReport):
                unknown = set(report.regions) - set(domain.surface_names)
                nonwalls = {
                    name for name in report.regions if roles.get(name) != "wall"
                }
                if unknown:
                    raise UnsupportedCaseError(
                        f"Force report {report.name!r} references unknown regions."
                    )
                if nonwalls:
                    raise UnsupportedCaseError(
                        f"Force report {report.name!r} must target wall-role regions."
                    )
            else:
                raise UnsupportedCaseError(
                    "The imported flow provider received an unknown report type."
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
        turbulent = not step.model.study.laminar
        flow_capability = _RANS_FLOW_CAPABILITY if turbulent else _FLOW_CAPABILITY
        runtime_identity = self.container_image or "externally-managed-openfoam"
        mesh_result = _restore_cached_mesh(
            prepared,
            self.mesh_cache_directory,
            runtime_identity,
        )
        mesh_reuse = mesh_result is not None
        if mesh_result is None:
            mesh_result = execute_imported_mesh(
                prepared,
                container_image=self.container_image,
                timeout_seconds=self.timeout_seconds,
            )
            _store_cached_mesh(
                prepared,
                mesh_result,
                self.mesh_cache_directory,
                runtime_identity,
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
        inlet_name = next(
            name for name, role in step.model.domain.boundary_roles if role == "inlet"
        )
        inlet_condition = step.model.boundary_conditions[inlet_name]
        mass_flow_times = sorted(inlet_flow)
        inlet_mass_flow = tuple(
            abs(inlet_flow[t]) * step.model.fluid.density for t in mass_flow_times
        )
        requested_mass_flow = (
            inlet_condition.mass_flow_rate
            if isinstance(inlet_condition, boundaries.MassFlowInlet)
            else None
        )
        mass_flow_relative_error = (
            abs(inlet_mass_flow[-1] - requested_mass_flow) / requested_mass_flow
            if inlet_mass_flow and requested_mass_flow is not None
            else None
        )
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
        check_mesh_log = prepared.directory / "log.checkMesh"
        quantities = _mesh_quality_quantities(
            check_mesh_log.read_text(encoding="utf-8", errors="replace")
            if check_mesh_log.is_file()
            else ""
        )
        quantities["runtime.simpleFoam.wall_seconds"] = Quantity(
            solver_duration, "s", kind="runtime_metric"
        )
        if imbalance:
            quantities["flow.relative_mass_imbalance"] = Quantity(imbalance[-1], "1")
        if pressure_drop:
            quantities["flow.pressure_drop"] = Quantity(pressure_drop[-1], "Pa")
        if inlet_mass_flow and requested_mass_flow is not None:
            quantities["flow.inlet_mass_flow_rate"] = Quantity(
                inlet_mass_flow[-1], "kg/s"
            )
        if requested_mass_flow is not None:
            quantities["reference.flow.inlet_mass_flow_rate"] = Quantity(
                requested_mass_flow,
                "kg/s",
                kind="scientific_input",
            )
        if mass_flow_relative_error is not None:
            quantities["flow.inlet_mass_flow_relative_error"] = Quantity(
                mass_flow_relative_error,
                "1",
                kind="verification_metric",
            )
        histories: dict[str, History] = {}
        if imbalance:
            histories["flow.relative_mass_imbalance"] = History(
                tuple(shared),
                imbalance,
                unit="1",
                abscissa_name="solver_iteration",
                abscissa_unit="1",
            )
        if pressure_drop:
            histories["flow.pressure_drop"] = History(
                tuple(pressure_times),
                pressure_drop,
                unit="Pa",
                abscissa_name="solver_iteration",
                abscissa_unit="1",
            )
        if inlet_mass_flow and requested_mass_flow is not None:
            histories["flow.inlet_mass_flow_rate"] = History(
                tuple(mass_flow_times),
                inlet_mass_flow,
                unit="kg/s",
                abscissa_name="solver_iteration",
                abscissa_unit="1",
            )
        y_plus_recovered = False
        y_plus_range_ok = False
        if turbulent:
            raw_y_plus = _read_y_plus_series(prepared.directory)
            y_plus_times = sorted(
                set.intersection(*(set(values) for values in raw_y_plus.values()))
            )
            for statistic in ("minimum", "maximum", "average"):
                values = tuple(
                    raw_y_plus[statistic][time_value]
                    for time_value in y_plus_times
                )
                if not values:
                    continue
                name = f"wall.y_plus.{statistic}"
                histories[name] = History(
                    tuple(y_plus_times),
                    values,
                    unit="1",
                    abscissa_name="solver_iteration",
                    abscissa_unit="1",
                    description=(
                        f"Wall y-plus {statistic} reported by OpenFOAM; use the full "
                        "range to review the selected wall treatment."
                    ),
                )
                quantities[name] = Quantity(
                    values[-1],
                    "1",
                    kind="verification_metric",
                )
            y_plus_recovered = all(
                f"wall.y_plus.{statistic}" in histories
                for statistic in ("minimum", "maximum", "average")
            )
            if y_plus_recovered:
                y_plus_range_ok = bool(
                    quantities["wall.y_plus.minimum"].value
                    >= _MINIMUM_WALL_FUNCTION_Y_PLUS
                    and quantities["wall.y_plus.maximum"].value
                    <= _MAXIMUM_WALL_FUNCTION_Y_PLUS
                )
            inlet_name = next(
                name for name, role in step.model.domain.boundary_roles
                if role == "inlet"
            )
            inlet = step.model.boundary_conditions[inlet_name]
            assert isinstance(inlet, boundaries.TurbulentVelocityInlet)
            estimate = engineering.turbulence_inlet_from_intensity(
                mean_velocity=inlet.magnitude,
                intensity=inlet.turbulence_intensity,
                length_scale=inlet.turbulence_length_scale,
            )
            quantities["turbulence.inlet.kinetic_energy"] = Quantity(
                estimate.turbulent_kinetic_energy,
                "m^2/s^2",
                kind="scientific_input",
            )
            quantities["turbulence.inlet.specific_dissipation_rate"] = Quantity(
                estimate.specific_dissipation_rate,
                "1/s",
                kind="scientific_input",
            )
        mesh_sha256, mesh_manifest = _write_mesh_manifest(prepared.directory)
        fields: dict[str, FieldRecord] = {}
        latest = _latest_time_directory(prepared.directory)
        if latest is not None:
            requested_native_fields = (
                ("U", "fluid.velocity", "m/s", ("x", "y", "z")),
                ("p", "fluid.pressure", "m^2/s^2", ()),
                *(
                    (
                        (
                            "k",
                            "turbulence.kinetic_energy",
                            "m^2/s^2",
                            (),
                        ),
                        (
                            "omega",
                            "turbulence.specific_dissipation_rate",
                            "1/s",
                            (),
                        ),
                        (
                            "nut",
                            "turbulence.kinematic_eddy_viscosity",
                            "m^2/s",
                            (),
                        ),
                    )
                    if turbulent
                    else ()
                ),
            )
            for native, canonical, unit, components in requested_native_fields:
                path = latest / native
                if canonical in step.output.fields and path.is_file():
                    fields[native] = FieldRecord(
                        unit=unit,
                        location="cell",
                        artifact=str(path),
                        components=components,
                        mesh_sha256=mesh_sha256,
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
        if turbulent:
            y_plus_root = prepared.directory / "postProcessing/agentcfd_y_plus"
            if y_plus_root.is_dir():
                for index, path in enumerate(sorted(y_plus_root.rglob("*.dat"))):
                    artifacts[f"wall_y_plus_{index}"] = Artifact.from_path(
                        path,
                        role="compact-report",
                        media_type="text/plain",
                    )
        recover_reports(step, prepared.directory, quantities, histories, artifacts)
        requested_history_map = {
            "flow.mass_balance": "flow.relative_mass_imbalance",
            "flow.pressure_drop": "flow.pressure_drop",
            "wall.y_plus": "wall.y_plus.average",
        }
        missing = [
            name
            for name in step.output.histories
            if requested_history_map[name] not in histories
        ]
        requested_field_map = {
            "fluid.velocity": "U",
            "fluid.pressure": "p",
            "turbulence.kinetic_energy": "k",
            "turbulence.specific_dissipation_rate": "omega",
            "turbulence.kinematic_eddy_viscosity": "nut",
        }
        missing.extend(
            name
            for name in step.output.fields
            if requested_field_map[name] not in fields
        )
        missing.extend(
            f"report:{report.name}"
            for report in step.output.reports
            if not report_recovered(report, histories)
        )
        runtime_version = _runtime_version(
            {
                path.name: path.read_text(encoding="utf-8", errors="replace")
                for path in prepared.directory.glob("log.*")
            },
            self.descriptor().version,
        )
        mass_ok = bool(imbalance) and imbalance[-1] <= 1.0e-4
        requested_mass_flow_ok = (
            mass_flow_relative_error is not None
            and mass_flow_relative_error <= 1.0e-4
        )
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
            *(
                (
                    Check(
                        "mass-flow-inlet-target",
                        requested_mass_flow_ok,
                        value=mass_flow_relative_error,
                        limit=1.0e-4,
                        observable="flow.inlet_mass_flow_rate",
                        message=(
                            "The recovered inlet mass flow must match the explicit "
                            "constant-density request."
                        ),
                    ),
                )
                if requested_mass_flow is not None
                else ()
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
            *(
                (
                    Check(
                        "wall-y-plus-recovery",
                        y_plus_recovered,
                        value=(
                            len(histories["wall.y_plus.average"].values)
                            if y_plus_recovered
                            else 0
                        ),
                        limit="at least one complete min/max/average wall y-plus sample",
                        kind="verification",
                        observable="wall.y_plus",
                        message=(
                            "The arbitrary-geometry RANS slice requires complete "
                            "wall-resolution evidence before acceptance."
                        ),
                    ),
                    Check(
                        "wall-y-plus-range",
                        y_plus_range_ok,
                        value=(
                            f"{quantities['wall.y_plus.minimum'].value:.6g}.."
                            f"{quantities['wall.y_plus.maximum'].value:.6g}"
                            if y_plus_recovered
                            else None
                        ),
                        limit=(
                            f"all wall y+ in [{_MINIMUM_WALL_FUNCTION_Y_PLUS:g}, "
                            f"{_MAXIMUM_WALL_FUNCTION_Y_PLUS:g}]"
                        ),
                        kind="verification",
                        observable="wall.y_plus",
                        message=(
                            "The full wall range must match the declared blended "
                            "wall-function strategy; refine or coarsen near-wall "
                            "spacing and rerun before promotion."
                        ),
                    ),
                )
                if turbulent
                else ()
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
                "mesh_acquisition": "cache-hit" if mesh_reuse else "generated",
                "mesh_plan_sha256": prepared.mesh_plan.to_dict()["plan_sha256"],
                "provider_capability": flow_capability,
                "runtime_version": runtime_version,
                "turbulence_model": step.model.study.turbulence,
            },
            messages=(
                "Experimental steady imported-volume workflow; accepted means numerical/workflow gates passed, not physical validation.",
                *(
                    (
                        "RANS wall y-plus must pass the declared wall-function range; "
                        "prism-layer and grid evidence remain open gates.",
                    )
                    if turbulent
                    else ()
                ),
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
