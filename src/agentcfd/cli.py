"""Small, stable CLI for people, agents, CI, and future GUIs."""

from __future__ import annotations

import argparse
import json
import math
import platform
import shlex
import shutil
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import (
    benchmarks,
    boundaries,
    capabilities,
    contracts,
    data_exchange,
    engineering,
    fluids,
    geometry,
    geometry_io,
    licensing,
    outputs,
    procedures,
    projects,
    properties,
    studies,
)
from ._version import __version__
from .errors import AgentCFDError, ProjectError
from .jsonio import strict_json_object
from .model import Model
from .provenance import content_fingerprint, file_sha256
from .providers import (
    execute_imported_mesh,
    OpenFOAMMeshControls,
    OpenFOAMProvider,
    OpenFOAMTurbulentPrecursorProvider,
    prepare_pipe_grid_study,
    plan_imported_mesh,
    prepare_imported_mesh,
    prepare_turbulent_model_study,
    prepare_turbulent_wall_function_study,
    prepare_turbulent_wall_study,
    turbulent_pipe_wall_mesh_screen,
)
from .results import SimulationResult, read_result_record
from .verification import (
    assess_grid_convergence,
    assess_turbulent_model_study,
    assess_turbulent_model_sweep,
    assess_turbulent_precursor_grid_study,
    assess_turbulent_wall_function_study,
    assess_turbulent_wall_study,
    assess_validation_point,
    grid_convergence_from_result_records,
    time_step_sensitivity_from_result_records,
)


def _paraview_executable() -> str | None:
    command = shutil.which("paraview")
    if command is not None:
        return command
    if sys.platform == "darwin":
        candidates = sorted(
            Path("/Applications").glob("ParaView*.app/Contents/MacOS/paraview"),
            reverse=True,
        )
        if candidates:
            return str(candidates[0])
    return None


def _paraview_batch_executable() -> str | None:
    command = shutil.which("pvbatch")
    if command is not None:
        return command
    if sys.platform == "darwin":
        candidates = sorted(
            Path("/Applications").glob("ParaView*.app/Contents/bin/pvbatch"),
            reverse=True,
        )
        if candidates:
            return str(candidates[0])
    return None


def _project_parameter(value: str) -> tuple[str, object]:
    name, separator, encoded = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(
            "Project parameters must use NAME=VALUE, for example velocity=0.8."
        )
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError:
        decoded = encoded
    if isinstance(decoded, (list, dict)):
        raise argparse.ArgumentTypeError(
            "Project parameter values must be JSON scalars, not arrays or objects."
        )
    return name, decoded


def _project_parameters(
    assignments: list[tuple[str, object]] | None,
    parameter_file: Path | None = None,
) -> dict[str, object]:
    selected = _parameter_set(parameter_file) if parameter_file is not None else {}
    command_line_names: set[str] = set()
    for name, value in assignments or []:
        if name in command_line_names:
            raise ValueError(f"Project parameter {name!r} was supplied more than once.")
        command_line_names.add(name)
        selected[name] = value
    return selected


def _parameter_set(path: Path) -> dict[str, object]:
    """Read one portable operating point without importing the project model."""

    try:
        payload = strict_json_object(
            path.read_text(encoding="utf-8"),
            label="project parameter set",
        )
    except OSError as error:
        raise ProjectError(f"Cannot read project parameter set {path}: {error}") from error
    if payload.get("schema") != "agentcfd.parameter-set/0.1":
        raise ProjectError(
            "Project parameter set must declare agentcfd.parameter-set/0.1."
        )
    unknown = set(payload) - {"schema", "parameters"}
    if unknown:
        rendered = ", ".join(sorted(unknown))
        raise ProjectError(f"Unknown project parameter set fields: {rendered}.")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ProjectError("Project parameter set parameters must be a JSON object.")
    for name, value in parameters.items():
        if not name.strip():
            raise ProjectError("Project parameter set names must not be empty.")
        if isinstance(value, (list, dict)):
            raise ProjectError(
                f"Project parameter {name!r} must be a JSON scalar, not an array or object."
            )
    return dict(parameters)


def _campaign_request(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = strict_json_object(
            path.read_text(encoding="utf-8"),
            label="campaign request",
        )
    except OSError as error:
        raise ProjectError(f"Cannot read campaign request {path}: {error}") from error
    if payload.get("schema") != "agentcfd.campaign-request/0.1":
        raise ProjectError("Campaign request must declare agentcfd.campaign-request/0.1.")
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ProjectError("Campaign request points must be a non-empty list.")
    points: dict[str, dict[str, object]] = {}
    for item in raw_points:
        if not isinstance(item, dict):
            raise ProjectError("Each campaign point must be an object.")
        name = item.get("name")
        parameters = item.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            raise ProjectError("Each campaign point requires name and parameters.")
        if name in points:
            raise ProjectError(f"Campaign point name {name!r} is duplicated.")
        points[name] = parameters
    return points


def _boundary_role_map(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    try:
        payload = strict_json_object(
            path.read_text(encoding="utf-8"),
            label="boundary role map",
        )
    except OSError as error:
        raise ProjectError(f"Cannot read boundary role map {path}: {error}") from error
    if payload.get("schema") != "agentcfd.boundary-role-map/0.1":
        raise ProjectError(
            "Boundary role map must declare agentcfd.boundary-role-map/0.1."
        )
    roles = payload.get("regions")
    if not isinstance(roles, dict):
        raise ProjectError("Boundary role map requires an object named regions.")
    return roles


def _doctor() -> dict[str, object]:
    openfoam = OpenFOAMProvider().descriptor()
    coolprop = properties.CoolPropPropertyProvider().descriptor()
    portable_io = data_exchange.io_available()
    try:
        numpy_version: str | None = version("numpy")
    except PackageNotFoundError:
        numpy_version = None
    return {
        "schema": "agentcfd.doctor/0.1",
        "healthy": True,
        "agentcfd": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": numpy_version,
        "executables": {
            "blockMesh": shutil.which("blockMesh"),
            "checkMesh": shutil.which("checkMesh"),
            "simpleFoam": shutil.which("simpleFoam"),
            "potentialFoam": shutil.which("potentialFoam"),
            "pimpleFoam": shutil.which("pimpleFoam"),
            "foamToVTK": shutil.which("foamToVTK"),
            "docker": shutil.which("docker"),
            "paraview": _paraview_executable(),
            "pvbatch": _paraview_batch_executable(),
        },
        "providers": {
            "reference-pipe": True,
            "openfoam-runtime": openfoam.available,
            "coolprop-properties": coolprop.available,
            "portable-xdmf-hdf5": portable_io,
        },
        "next_actions": [
            *(
                ["Install `agentcfd[io]` for standard XDMF/HDF5 field publication."]
                if not portable_io
                else []
            ),
            *(
                [
                    "Install OpenFOAM locally or use a configured container image for numerical CFD."
                ]
                if not openfoam.available
                else []
            ),
        ],
    }


def _watch_summary(report: dict[str, object]) -> str:
    """Render one compact append-only progress line for terminals and logs."""

    parts = [str(report["state"]).upper()]
    progress = report.get("progress")
    if not isinstance(progress, dict):
        return " | ".join(parts)
    if progress.get("current_command"):
        parts.append(str(progress["current_command"]))
    coordinate = progress.get("coordinate")
    if isinstance(coordinate, dict) and coordinate.get("current") is not None:
        current = float(coordinate["current"])
        target = coordinate.get("target")
        unit = "s" if coordinate.get("unit") == "s" else "iter"
        position = f"{current:g}{unit}"
        if target is not None:
            position += f"/{float(target):g}{unit}"
        if coordinate.get("fraction") is not None:
            position += f" {100.0 * float(coordinate['fraction']):.1f}%"
        parts.append(position)
    residuals = progress.get("latest_residuals")
    if isinstance(residuals, dict) and residuals:
        worst = max(float(item["initial"]) for item in residuals.values())
        parts.append(f"residual≤{worst:.3g}")
    monitors = progress.get("monitors")
    if (
        isinstance(monitors, dict)
        and monitors.get("relative_mass_imbalance") is not None
    ):
        parts.append(f"imbalance={float(monitors['relative_mass_imbalance']):.3g}")
    if progress.get("elapsed_display"):
        parts.append(str(progress["elapsed_display"]))
    workspace = progress.get("workspace")
    if isinstance(workspace, dict) and workspace.get("display") is not None:
        parts.append(str(workspace["display"]))
    return " | ".join(parts)


def _result_cli_payload(result: SimulationResult) -> dict[str, object]:
    """Add a compact decision surface without changing the result contract."""

    payload = result.summary()
    failed = [check.as_dict() for check in result.checks if not check.passed]
    payload["decision"] = {
        "accepted": result.accepted,
        "failed_check_count": len(failed),
        "failed_checks": failed,
        "guidance": (
            "Result is accepted for its declared capability and policy."
            if result.accepted
            else "Resolve the failed checks; do not promote this result to training or design evidence."
        ),
    }
    return payload


def _result_quantity_group(name: str, quantity: dict[str, object]) -> str:
    """Group flat canonical quantities without changing the machine contract."""

    if name.startswith("thermal."):
        return "Thermal results"
    kind = quantity.get("kind")
    if kind == "scientific_input" or name.startswith("reference."):
        return "Inputs"
    if kind == "runtime_metric" or name.startswith("runtime."):
        return "Runtime"
    if kind == "verification_metric":
        return "Verification"
    if name.startswith("mesh."):
        return "Mesh quality"
    if name.startswith("flow."):
        return "Flow results"
    return "Other results"


def _error_cli_payload(error: Exception) -> dict[str, object]:
    repairs = {
        FileNotFoundError: "Check the project path or run `agentcfd init` to create one.",
        FileExistsError: "Choose an empty destination or preserve the existing user-owned files.",
        ProjectError: "Run `agentcfd status . --json` and follow its next_action.",
        ValueError: "Correct the reported input value, then retry the same command.",
    }
    repair = next(
        (message for kind, message in repairs.items() if isinstance(error, kind)),
        "Inspect the error and project status before retrying.",
    )
    name = type(error).__name__
    code = "".join(
        ("_" if index and character.isupper() else "") + character.upper()
        for index, character in enumerate(name)
    )
    return {
        "schema": "agentcfd.error/0.1",
        "ok": False,
        "error": {
            "code": code,
            "type": name,
            "message": str(error),
            "repair": repair,
            "safe_to_retry": not isinstance(error, FileExistsError),
        },
    }


def _pipe_model(*, fully_developed: bool = False) -> Model:
    inlet = (
        boundaries.fully_developed_velocity_inlet(0.02)
        if fully_developed
        else boundaries.mean_velocity_inlet(0.02)
    )
    return Model(
        name="laminar-water-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=10.0, diameter=0.05),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=inlet,
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def _pipe_grid_benchmark_model() -> Model:
    """Return the bounded, aspect-ratio-conscious three-grid pipe benchmark."""

    return Model(
        name="laminar-water-pipe-grid-benchmark",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=0.5, diameter=0.1),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.fully_developed_velocity_inlet(0.01),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def _turbulent_pipe_model(
    *,
    turbulence_model: str = "k-omega-sst",
    velocity: float = 1.0,
    turbulence_intensity: float = 0.05,
    turbulence_length_scale: float = 0.007,
) -> Model:
    """Return an explicit smooth-pipe two-equation RANS benchmark."""

    return Model(
        name="turbulent-water-pipe",
        study=studies.internal_flow(
            turbulence=turbulence_model,
            wall_treatment="blended-wall-functions",
        ),
        domain=geometry.circular_pipe(length=3.0, diameter=0.1),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.turbulent_mean_velocity_inlet(
            velocity,
            intensity=turbulence_intensity,
            length_scale=turbulence_length_scale,
        ),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def _pipe_demo(output_path: Path) -> dict[str, object]:
    result = (
        _pipe_model()
        .step(procedure=procedures.steady(), output=outputs.standard())
        .run()
    )
    result.write(output_path)
    return result.to_dict()


def _prepare_openfoam_pipe(
    case_directory: Path,
    *,
    fully_developed: bool = False,
    cross_section_cells: int = 8,
    axial_cells: int | None = None,
    nominal_wall_cell_fraction: float | None = None,
) -> dict[str, object]:
    step = _pipe_model(fully_developed=fully_developed).step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    mesh = OpenFOAMMeshControls(
        cross_section_cells=cross_section_cells,
        axial_cells=axial_cells,
        nominal_wall_cell_fraction=nominal_wall_cell_fraction,
    )
    return (
        OpenFOAMProvider(case_directory=case_directory, mesh=mesh)
        .prepare(step)
        .to_dict()
    )


def _run_openfoam_pipe(
    case_directory: Path,
    *,
    result_path: Path | None,
    fully_developed: bool,
    container_image: str | None,
    cross_section_cells: int,
    axial_cells: int | None,
    nominal_wall_cell_fraction: float | None,
    prepared: bool,
    timeout_seconds: float,
):
    step = _pipe_model(fully_developed=fully_developed).step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    provider = OpenFOAMProvider(
        case_directory=case_directory,
        container_image=container_image,
        timeout_seconds=timeout_seconds,
        mesh=OpenFOAMMeshControls(
            cross_section_cells=cross_section_cells,
            axial_cells=axial_cells,
            nominal_wall_cell_fraction=nominal_wall_cell_fraction,
        ),
    )
    result = provider.run_prepared(step) if prepared else provider.run(step)
    target = result_path or case_directory / "agentcfd-result.json"
    result.write(target)
    return result, target


def _turbulent_pipe_step(
    *,
    turbulence_model: str = "k-omega-sst",
    velocity: float,
    turbulence_intensity: float,
    turbulence_length_scale: float,
):
    return _turbulent_pipe_model(
        turbulence_model=turbulence_model,
        velocity=velocity,
        turbulence_intensity=turbulence_intensity,
        turbulence_length_scale=turbulence_length_scale,
    ).step(
        procedure=procedures.steady(
            relative_tolerance=1.0e-4,
            maximum_iterations=300,
        ),
        output=outputs.turbulent_internal_flow(turbulence_model=turbulence_model),
    )


def _turbulent_openfoam_provider(
    case_directory: Path,
    *,
    cross_section_cells: int,
    axial_cells: int,
    nominal_wall_cell_fraction: float | None = None,
    precursor_case: Path | None = None,
    container_image: str | None = None,
    timeout_seconds: float = 3600.0,
) -> OpenFOAMProvider:
    return OpenFOAMProvider(
        case_directory=case_directory,
        precursor_case=precursor_case,
        container_image=container_image,
        timeout_seconds=timeout_seconds,
        mesh=OpenFOAMMeshControls(
            cross_section_cells=cross_section_cells,
            axial_cells=axial_cells,
            nominal_wall_cell_fraction=nominal_wall_cell_fraction,
        ),
    )


def _turbulent_precursor_provider(
    case_directory: Path,
    *,
    cross_section_cells: int,
    maximum_iterations: int,
    nominal_wall_cell_fraction: float | None = None,
    nut_wall_function: str = "nutUBlendedWallFunction",
    container_image: str | None = None,
    timeout_seconds: float = 3600.0,
) -> OpenFOAMTurbulentPrecursorProvider:
    return OpenFOAMTurbulentPrecursorProvider(
        case_directory=case_directory,
        cross_section_cells=cross_section_cells,
        nominal_wall_cell_fraction=nominal_wall_cell_fraction,
        nut_wall_function=nut_wall_function,
        maximum_iterations=maximum_iterations,
        container_image=container_image,
        timeout_seconds=timeout_seconds,
    )


def _prepare_openfoam_pipe_grid(
    directory: Path,
    *,
    cross_section_cells: tuple[int, int, int],
    base_axial_cells: int,
) -> dict[str, object]:
    step = _pipe_grid_benchmark_model().step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    return prepare_pipe_grid_study(
        step,
        directory,
        cross_section_cells=cross_section_cells,
        base_axial_cells=base_axial_cells,
    ).to_dict()


def _prepare_openfoam_turbulent_wall_study(
    directory: Path,
    *,
    cross_section_cells: tuple[int, int, int],
    nominal_wall_cell_fraction: float,
    nut_wall_function: str,
    maximum_iterations: tuple[int, int, int],
) -> dict[str, object]:
    step = _turbulent_pipe_step(
        velocity=1.0,
        turbulence_intensity=0.05,
        turbulence_length_scale=0.007,
    )
    return prepare_turbulent_wall_study(
        step,
        directory,
        cross_section_cells=cross_section_cells,
        nominal_wall_cell_fraction=nominal_wall_cell_fraction,
        nut_wall_function=nut_wall_function,
        maximum_iterations=maximum_iterations,
    ).to_dict()


def _prepare_openfoam_turbulent_wall_function_study(
    directory: Path,
    *,
    cross_section_cells: int,
    nominal_wall_cell_fraction: float,
    maximum_iterations: int,
) -> dict[str, object]:
    step = _turbulent_pipe_step(
        velocity=1.0,
        turbulence_intensity=0.05,
        turbulence_length_scale=0.007,
    )
    return prepare_turbulent_wall_function_study(
        step,
        directory,
        cross_section_cells=cross_section_cells,
        nominal_wall_cell_fraction=nominal_wall_cell_fraction,
        maximum_iterations=maximum_iterations,
    ).to_dict()


def _prepare_openfoam_turbulent_model_study(
    directory: Path,
    *,
    velocity: float,
    turbulence_intensity: float,
    turbulence_length_scale: float,
    cross_section_cells: int,
    nominal_wall_cell_fraction: float | None,
    target_y_plus: float | None,
    maximum_iterations: int,
) -> dict[str, object]:
    common = {
        "velocity": velocity,
        "turbulence_intensity": turbulence_intensity,
        "turbulence_length_scale": turbulence_length_scale,
    }
    sst_step = _turbulent_pipe_step(turbulence_model="k-omega-sst", **common)
    fraction = (
        0.0625 if nominal_wall_cell_fraction is None else nominal_wall_cell_fraction
    )
    if target_y_plus is not None:
        fraction = float(
            turbulent_pipe_wall_mesh_screen(
                sst_step,
                nominal_wall_cell_fraction=0.0625,
                target_y_plus=target_y_plus,
            )["recommended_nominal_wall_cell_fraction"]
        )
    return prepare_turbulent_model_study(
        sst_step,
        _turbulent_pipe_step(turbulence_model="k-epsilon", **common),
        directory,
        cross_section_cells=cross_section_cells,
        nominal_wall_cell_fraction=fraction,
        target_y_plus=50.0 if target_y_plus is None else target_y_plus,
        maximum_iterations=maximum_iterations,
    ).to_dict()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _prepare_openfoam_turbulent_model_sweep(
    directory: Path,
    *,
    velocities: tuple[float, ...],
    target_y_plus: float,
    turbulence_intensity: float,
    turbulence_length_scale: float,
    cross_section_cells: int,
    maximum_iterations: int,
) -> dict[str, object]:
    if len(velocities) < 3:
        raise ValueError("A turbulent model sweep requires at least three velocities.")
    selected = tuple(float(value) for value in velocities)
    if any(not math.isfinite(value) or value <= 0.0 for value in selected):
        raise ValueError(
            "Turbulent model sweep velocities must be finite and positive."
        )
    if len(set(selected)) != len(selected):
        raise ValueError("Turbulent model sweep velocities must be distinct.")
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(
            f"OpenFOAM model-sweep directory is not empty: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    point_records: list[dict[str, object]] = []
    for index, velocity in enumerate(sorted(selected), start=1):
        relative = Path(f"point-{index:02d}")
        point_directory = directory / relative
        point = _prepare_openfoam_turbulent_model_study(
            point_directory,
            velocity=velocity,
            turbulence_intensity=turbulence_intensity,
            turbulence_length_scale=turbulence_length_scale,
            cross_section_cells=cross_section_cells,
            nominal_wall_cell_fraction=None,
            target_y_plus=target_y_plus,
            maximum_iterations=maximum_iterations,
        )
        plan_path = point_directory / "agentcfd-turbulent-model-study.json"
        screen = point["wall_resolution_screen"]
        assert isinstance(screen, dict)
        point_records.append(
            {
                "index": index,
                "directory": str(relative),
                "velocity_m_per_s": velocity,
                "reynolds_number": screen["reynolds_number"],
                "nominal_wall_cell_fraction": point["nominal_wall_cell_fraction"],
                "plan": str(relative / plan_path.name),
                "plan_sha256": file_sha256(plan_path),
                "assessment": str(
                    relative / "agentcfd-turbulent-model-assessment.json"
                ),
            }
        )
    payload: dict[str, object] = {
        "schema": "agentcfd.openfoam-turbulent-model-sweep/0.1",
        "target_y_plus": float(target_y_plus),
        "turbulence_intensity": float(turbulence_intensity),
        "turbulence_length_scale_m": float(turbulence_length_scale),
        "cross_section_cells": int(cross_section_cells),
        "maximum_iterations": int(maximum_iterations),
        "points": point_records,
    }
    payload["campaign_sha256"] = content_fingerprint(payload)
    _write_json_atomic(directory / "agentcfd-turbulent-model-sweep.json", payload)
    return payload


def _run_openfoam_turbulent_model_sweep(
    directory: Path,
    *,
    container_image: str | None,
    timeout_seconds: float,
    resume: bool,
) -> tuple[dict[str, object], Path]:
    plan_path = directory / "agentcfd-turbulent-model-sweep.json"
    plan = strict_json_object(
        plan_path.read_text(encoding="utf-8"),
        label=f"OpenFOAM turbulent model-sweep plan {plan_path}",
    )
    if plan.get("schema") != "agentcfd.openfoam-turbulent-model-sweep/0.1":
        raise ValueError("OpenFOAM turbulent model-sweep plan schema is unsupported.")
    recorded_identity = plan.get("campaign_sha256")
    identity_payload = dict(plan)
    identity_payload.pop("campaign_sha256", None)
    if recorded_identity != content_fingerprint(identity_payload):
        raise ValueError("OpenFOAM turbulent model-sweep campaign identity changed.")
    points = plan.get("points")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError(
            "OpenFOAM turbulent model sweep requires at least three points."
        )
    root = directory.resolve()
    assessments: list[Path] = []
    progress: dict[str, object] = {
        "schema": "agentcfd.campaign-progress/0.1",
        "campaign_sha256": recorded_identity,
        "total_points": len(points),
        "completed_points": 0,
        "status": "running",
    }
    progress_path = directory / "agentcfd-campaign-progress.json"
    _write_json_atomic(progress_path, progress)
    for position, point in enumerate(points, start=1):
        if not isinstance(point, dict):
            raise ValueError("OpenFOAM turbulent model-sweep point is invalid.")
        point_directory = (directory / str(point.get("directory", ""))).resolve()
        try:
            point_directory.relative_to(root)
        except ValueError as error:
            raise ValueError(
                "OpenFOAM model-sweep point escapes its directory."
            ) from error
        nested_plan = point_directory / "agentcfd-turbulent-model-study.json"
        if file_sha256(nested_plan) != point.get("plan_sha256"):
            raise ValueError("OpenFOAM model-sweep point plan identity changed.")
        assessment_path = point_directory / "agentcfd-turbulent-model-assessment.json"
        try:
            if resume and assessment_path.is_file():
                nested = strict_json_object(
                    nested_plan.read_text(encoding="utf-8"),
                    label=f"OpenFOAM turbulent model-study plan {nested_plan}",
                )
                cases = nested.get("cases")
                if not isinstance(cases, list) or len(cases) != 2:
                    raise ValueError("OpenFOAM model-sweep point has invalid cases.")
                results = [point_directory / str(case["result"]) for case in cases]
                refreshed = _turbulent_model_study_payload(results)
                _write_json_atomic(assessment_path, refreshed)
            else:
                _run_openfoam_turbulent_model_study(
                    point_directory,
                    container_image=container_image,
                    timeout_seconds=timeout_seconds,
                )
        except (AgentCFDError, OSError, ValueError) as error:
            progress["status"] = "failed"
            progress["failed_point"] = position
            progress["error"] = str(error)
            _write_json_atomic(progress_path, progress)
            raise
        assessments.append(assessment_path)
        progress["completed_points"] = position
        _write_json_atomic(progress_path, progress)
    payload = _turbulent_model_sweep_payload(assessments)
    target = directory / "agentcfd-turbulent-model-sweep-assessment.json"
    _write_json_atomic(target, payload)
    progress["status"] = "completed"
    progress["assessment"] = target.name
    _write_json_atomic(progress_path, progress)
    return payload, target


def _grid_convergence_payload(
    paths: list[Path],
    *,
    quantity: str,
) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    study = grid_convergence_from_result_records(records, quantity=quantity)
    return {
        "schema": "agentcfd.grid-convergence/0.1",
        "quantity": quantity,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
        **study.to_dict(),
        "acceptance": assess_grid_convergence(study),
    }


def _time_step_sensitivity_payload(
    paths: list[Path],
    *,
    quantity: str,
    maximum_relative_change: float,
) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    study = time_step_sensitivity_from_result_records(
        records,
        quantity=quantity,
        maximum_relative_change=maximum_relative_change,
    )
    return {
        **study.to_dict(),
        "quantity": quantity,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
    }


def _turbulent_wall_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_wall_study(records)
    return {
        **assessment,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
    }


def _turbulent_precursor_grid_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_precursor_grid_study(records)
    return {
        **assessment,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
    }


def _turbulent_wall_function_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_wall_function_study(records)
    return {
        **assessment,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
    }


def _turbulent_model_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_model_study(records)
    return {
        **assessment,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
    }


def _turbulent_model_sweep_payload(paths: list[Path]) -> dict[str, object]:
    studies = [
        strict_json_object(
            path.read_text(encoding="utf-8"),
            label=f"Turbulent model study {path}",
        )
        for path in paths
    ]
    assessment = assess_turbulent_model_sweep(studies)
    return {
        **assessment,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
    }


def _run_openfoam_pipe_grid(
    directory: Path,
    *,
    container_image: str | None,
    timeout_seconds: float,
) -> tuple[dict[str, object], Path]:
    plan_path = directory / "agentcfd-grid-study.json"
    plan = strict_json_object(
        plan_path.read_text(encoding="utf-8"),
        label=f"OpenFOAM grid-study plan {plan_path}",
    )
    if plan.get("schema") != "agentcfd.openfoam-grid-study/0.1":
        raise ValueError("OpenFOAM grid-study plan schema is unsupported.")
    step = _pipe_grid_benchmark_model().step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    if plan.get("model_sha256") != step.model.fingerprint():
        raise ValueError(
            "OpenFOAM grid-study plan belongs to a different benchmark model."
        )
    expected_inputs = json.loads(
        json.dumps(
            {
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
            },
            allow_nan=False,
        )
    )
    if plan.get("scientific_inputs") != expected_inputs:
        raise ValueError(
            "OpenFOAM grid-study plan uses different model, procedure, or output inputs."
        )
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError("OpenFOAM grid-study plan must contain exactly three cases.")

    root = directory.resolve()
    result_paths: list[Path] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("OpenFOAM grid-study case record is invalid.")
        case_directory = (directory / str(case.get("directory", ""))).resolve()
        try:
            case_directory.relative_to(root)
        except ValueError as error:
            raise ValueError(
                "OpenFOAM grid-study case escapes the study directory."
            ) from error
        manifest_path = case_directory / "agentcfd-case.json"
        manifest = strict_json_object(
            manifest_path.read_text(encoding="utf-8"),
            label=f"OpenFOAM case manifest {manifest_path}",
        )
        if manifest.get("case_sha256") != case.get("case_sha256"):
            raise ValueError("OpenFOAM grid-study case identity differs from its plan.")
        mesh = OpenFOAMMeshControls(
            cross_section_cells=case.get("cross_section_cells"),
            axial_cells=case.get("axial_cells"),
        )
        result = OpenFOAMProvider(
            case_directory=case_directory,
            mesh=mesh,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
        ).run_prepared(step)
        result_path = case_directory / "agentcfd-result.json"
        result.write(result_path)
        result_paths.append(result_path)

    payload = _grid_convergence_payload(result_paths, quantity="flow.pressure_drop")
    target = directory / "agentcfd-grid-convergence.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return payload, target


def _run_openfoam_turbulent_wall_study(
    directory: Path,
    *,
    container_image: str | None,
    timeout_seconds: float,
) -> tuple[dict[str, object], Path]:
    plan_path = directory / "agentcfd-turbulent-wall-study.json"
    plan = strict_json_object(
        plan_path.read_text(encoding="utf-8"),
        label=f"OpenFOAM turbulent wall-study plan {plan_path}",
    )
    if plan.get("schema") != "agentcfd.openfoam-turbulent-wall-study/0.1":
        raise ValueError("OpenFOAM turbulent wall-study plan schema is unsupported.")
    step = _turbulent_pipe_step(
        velocity=1.0,
        turbulence_intensity=0.05,
        turbulence_length_scale=0.007,
    )
    if plan.get("model_sha256") != step.model.fingerprint():
        raise ValueError("OpenFOAM turbulent wall-study uses a different model.")
    expected_inputs = json.loads(
        json.dumps(
            {
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
            },
            allow_nan=False,
        )
    )
    if plan.get("scientific_inputs") != expected_inputs:
        raise ValueError(
            "OpenFOAM turbulent wall-study inputs changed after preparation."
        )
    fraction = plan.get("nominal_wall_cell_fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ValueError(
            "OpenFOAM turbulent wall-study has an invalid wall-cell fraction."
        )
    nut_wall_function = plan.get("nut_wall_function")
    if not isinstance(nut_wall_function, str):
        raise ValueError("OpenFOAM turbulent wall-study has no momentum wall function.")
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError(
            "OpenFOAM turbulent wall-study must contain exactly three cases."
        )

    root = directory.resolve()
    result_paths: list[Path] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("OpenFOAM turbulent wall-study case record is invalid.")
        case_directory = (directory / str(case.get("directory", ""))).resolve()
        try:
            case_directory.relative_to(root)
        except ValueError as error:
            raise ValueError(
                "OpenFOAM turbulent wall-study case escapes the study directory."
            ) from error
        manifest = strict_json_object(
            (case_directory / "agentcfd-case.json").read_text(encoding="utf-8"),
            label=f"OpenFOAM case manifest {case_directory}",
        )
        if manifest.get("case_sha256") != case.get("case_sha256"):
            raise ValueError("OpenFOAM turbulent wall-study case identity changed.")
        cross_cells = case.get("cross_section_cells")
        iteration_limit = case.get("maximum_iterations")
        if isinstance(cross_cells, bool) or not isinstance(cross_cells, int):
            raise ValueError("OpenFOAM turbulent wall-study grid count is invalid.")
        if isinstance(iteration_limit, bool) or not isinstance(iteration_limit, int):
            raise ValueError(
                "OpenFOAM turbulent wall-study iteration limit is invalid."
            )
        provider = OpenFOAMTurbulentPrecursorProvider(
            case_directory=case_directory,
            cross_section_cells=cross_cells,
            nominal_wall_cell_fraction=float(fraction),
            nut_wall_function=nut_wall_function,
            maximum_iterations=iteration_limit,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
        )
        result = provider.run_prepared(step)
        result_path = case_directory / "agentcfd-result.json"
        result.write(result_path)
        result_paths.append(result_path)

    payload = _turbulent_wall_study_payload(result_paths)
    target = directory / "agentcfd-turbulent-wall-assessment.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return payload, target


def _run_openfoam_turbulent_wall_function_study(
    directory: Path,
    *,
    container_image: str | None,
    timeout_seconds: float,
) -> tuple[dict[str, object], Path]:
    plan_path = directory / "agentcfd-turbulent-wall-function-study.json"
    plan = strict_json_object(
        plan_path.read_text(encoding="utf-8"),
        label=f"OpenFOAM turbulent wall-function plan {plan_path}",
    )
    if plan.get("schema") != "agentcfd.openfoam-turbulent-wall-function-study/0.1":
        raise ValueError("OpenFOAM turbulent wall-function plan schema is unsupported.")
    step = _turbulent_pipe_step(
        velocity=1.0,
        turbulence_intensity=0.05,
        turbulence_length_scale=0.007,
    )
    if plan.get("model_sha256") != step.model.fingerprint():
        raise ValueError(
            "OpenFOAM turbulent wall-function study uses a different model."
        )
    expected_inputs = json.loads(
        json.dumps(
            {
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
            },
            allow_nan=False,
        )
    )
    if plan.get("scientific_inputs") != expected_inputs:
        raise ValueError("OpenFOAM turbulent wall-function study inputs changed.")
    cross_cells = plan.get("cross_section_cells")
    fraction = plan.get("nominal_wall_cell_fraction")
    iteration_limit = plan.get("maximum_iterations")
    if isinstance(cross_cells, bool) or not isinstance(cross_cells, int):
        raise ValueError("OpenFOAM wall-function study grid count is invalid.")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ValueError("OpenFOAM wall-function study wall-cell fraction is invalid.")
    if isinstance(iteration_limit, bool) or not isinstance(iteration_limit, int):
        raise ValueError("OpenFOAM wall-function study iteration limit is invalid.")
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError(
            "OpenFOAM wall-function study must contain exactly three cases."
        )

    root = directory.resolve()
    result_paths: list[Path] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("OpenFOAM wall-function case record is invalid.")
        case_directory = (directory / str(case.get("directory", ""))).resolve()
        try:
            case_directory.relative_to(root)
        except ValueError as error:
            raise ValueError(
                "OpenFOAM wall-function case escapes its study directory."
            ) from error
        manifest = strict_json_object(
            (case_directory / "agentcfd-case.json").read_text(encoding="utf-8"),
            label=f"OpenFOAM case manifest {case_directory}",
        )
        if manifest.get("case_sha256") != case.get("case_sha256"):
            raise ValueError("OpenFOAM wall-function case identity changed.")
        wall_function = case.get("nut_wall_function")
        if not isinstance(wall_function, str):
            raise ValueError(
                "OpenFOAM wall-function case has no implementation identity."
            )
        result = OpenFOAMTurbulentPrecursorProvider(
            case_directory=case_directory,
            cross_section_cells=cross_cells,
            nominal_wall_cell_fraction=float(fraction),
            nut_wall_function=wall_function,
            maximum_iterations=iteration_limit,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
        ).run_prepared(step)
        result_path = case_directory / "agentcfd-result.json"
        result.write(result_path)
        result_paths.append(result_path)
    payload = _turbulent_wall_function_study_payload(result_paths)
    target = directory / "agentcfd-turbulent-wall-function-assessment.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return payload, target


def _run_openfoam_turbulent_model_study(
    directory: Path,
    *,
    container_image: str | None,
    timeout_seconds: float,
) -> tuple[dict[str, object], Path]:
    plan_path = directory / "agentcfd-turbulent-model-study.json"
    plan = strict_json_object(
        plan_path.read_text(encoding="utf-8"),
        label=f"OpenFOAM turbulent model-study plan {plan_path}",
    )
    if plan.get("schema") != "agentcfd.openfoam-turbulent-model-study/0.1":
        raise ValueError("OpenFOAM turbulent model-study plan schema is unsupported.")
    cross_cells = plan.get("cross_section_cells")
    fraction = plan.get("nominal_wall_cell_fraction")
    iteration_limit = plan.get("maximum_iterations")
    if isinstance(cross_cells, bool) or not isinstance(cross_cells, int):
        raise ValueError("OpenFOAM model study grid count is invalid.")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ValueError("OpenFOAM model study wall-cell fraction is invalid.")
    if isinstance(iteration_limit, bool) or not isinstance(iteration_limit, int):
        raise ValueError("OpenFOAM model study iteration limit is invalid.")
    planned_inputs = plan.get("scientific_inputs")
    planned_cases = (
        planned_inputs.get("cases") if isinstance(planned_inputs, dict) else None
    )
    sst_inputs = (
        planned_cases.get("k-omega-sst") if isinstance(planned_cases, dict) else None
    )
    planned_model = sst_inputs.get("model") if isinstance(sst_inputs, dict) else None
    planned_boundaries = (
        planned_model.get("boundaries") if isinstance(planned_model, dict) else None
    )
    planned_inlets = (
        [
            value
            for value in planned_boundaries.values()
            if isinstance(value, dict)
            and value.get("type") == "turbulent-mean-velocity-inlet"
        ]
        if isinstance(planned_boundaries, dict)
        else []
    )
    if len(planned_inlets) != 1:
        raise ValueError("OpenFOAM model study has no unique turbulent inlet input.")
    planned_inlet = planned_inlets[0]
    common = {
        "velocity": planned_inlet.get("velocity"),
        "turbulence_intensity": planned_inlet.get("turbulence_intensity"),
        "turbulence_length_scale": planned_inlet.get("turbulence_length_scale"),
    }
    steps = {
        "k-omega-sst": _turbulent_pipe_step(turbulence_model="k-omega-sst", **common),
        "k-epsilon": _turbulent_pipe_step(turbulence_model="k-epsilon", **common),
    }
    expected_inputs = json.loads(
        json.dumps(
            {
                "cases": {
                    name: {
                        "model": step.model.to_dict(),
                        "procedure": step.procedure.to_dict(),
                        "output_request": step.output.to_dict(),
                    }
                    for name, step in steps.items()
                }
            },
            allow_nan=False,
        )
    )
    if plan.get("scientific_inputs") != expected_inputs:
        raise ValueError("OpenFOAM turbulent model-study inputs changed.")
    planned_screen = plan.get("wall_resolution_screen")
    if not isinstance(planned_screen, dict):
        raise ValueError(
            "OpenFOAM turbulent model-study has no wall-resolution screen."
        )
    expected_screen = turbulent_pipe_wall_mesh_screen(
        steps["k-omega-sst"],
        nominal_wall_cell_fraction=float(fraction),
        target_y_plus=planned_screen.get("target_y_plus"),
    )
    if plan.get("wall_resolution_screen") != expected_screen:
        raise ValueError(
            "OpenFOAM turbulent model-study wall-resolution screen changed."
        )
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError("OpenFOAM model study must contain exactly two cases.")
    wall_functions = {
        "k-omega-sst": "nutUSpaldingWallFunction",
        "k-epsilon": "nutkWallFunction",
    }
    root = directory.resolve()
    result_paths: list[Path] = []
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("OpenFOAM model-study case record is invalid.")
        turbulence_model = case.get("turbulence_model")
        if not isinstance(turbulence_model, str) or turbulence_model not in steps:
            raise ValueError("OpenFOAM model-study turbulence model is invalid.")
        if turbulence_model in seen:
            raise ValueError("OpenFOAM model-study turbulence model is duplicated.")
        seen.add(turbulence_model)
        if case.get("nut_wall_function") != wall_functions[turbulence_model]:
            raise ValueError("OpenFOAM model-study wall-function pairing changed.")
        step = steps[turbulence_model]
        if case.get("model_sha256") != step.model.fingerprint():
            raise ValueError("OpenFOAM model-study model identity changed.")
        case_directory = (directory / str(case.get("directory", ""))).resolve()
        try:
            case_directory.relative_to(root)
        except ValueError as error:
            raise ValueError(
                "OpenFOAM model-study case escapes its directory."
            ) from error
        manifest = strict_json_object(
            (case_directory / "agentcfd-case.json").read_text(encoding="utf-8"),
            label=f"OpenFOAM case manifest {case_directory}",
        )
        if manifest.get("case_sha256") != case.get("case_sha256"):
            raise ValueError("OpenFOAM model-study case identity changed.")
        result = OpenFOAMTurbulentPrecursorProvider(
            case_directory=case_directory,
            cross_section_cells=cross_cells,
            nominal_wall_cell_fraction=float(fraction),
            nut_wall_function=wall_functions[turbulence_model],
            maximum_iterations=iteration_limit,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
        ).run_prepared(step)
        result_path = case_directory / "agentcfd-result.json"
        result.write(result_path)
        result_paths.append(result_path)
    if seen != set(steps):
        raise ValueError("OpenFOAM model study does not cover both supported models.")
    payload = _turbulent_model_study_payload(result_paths)
    target = directory / "agentcfd-turbulent-model-assessment.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return payload, target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentcfd",
        description="AI-native CFD for humans and agents.",
        epilog=(
            "Start with `agentcfd init my-flow`, then run `agentcfd status my-flow` "
            "and follow its recommended next action."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"AgentCFD {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="Inspect the installed runtime or audit one project in context.",
    )
    doctor.add_argument("project", nargs="?", type=Path)
    doctor.add_argument("--json", action="store_true", dest="as_json")

    init = subparsers.add_parser(
        "init",
        help="Create a readable AgentCFD engineering project.",
    )
    init.add_argument("directory", nargs="?", type=Path, default=Path("."))
    init.add_argument(
        "--template",
        choices=(
            "industrial-pipe",
            "heated-pipe",
            "baffle-channel",
            "imported-internal-flow",
        ),
        default=None,
    )
    init.add_argument(
        "--provider",
        choices=("reference", "openfoam"),
        default=None,
    )
    init.add_argument("--geometry", type=Path)
    init.add_argument("--unit", choices=("m", "mm", "cm", "um", "in", "ft"))
    init_roles = init.add_mutually_exclusive_group()
    init_roles.add_argument("--roles", type=Path)
    init_roles.add_argument(
        "--accept-name-roles",
        action="store_true",
        help=(
            "Explicitly confirm every unambiguous name-based role suggestion; "
            "fail if any surface name is ambiguous."
        ),
    )
    init.add_argument(
        "--interior-point-m",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
    )
    init_inlet = init.add_mutually_exclusive_group()
    init_inlet.add_argument(
        "--inlet-velocity-m-s",
        nargs=3,
        type=float,
        metavar=("UX", "UY", "UZ"),
    )
    init_inlet.add_argument(
        "--inlet-mass-flow-kg-s",
        type=float,
        help="Positive inlet mass flow for constant-density laminar flow.",
    )
    init_inlet.add_argument(
        "--inlet-total-gauge-pressure-pa",
        type=float,
        help="Positive inlet total gauge pressure for pressure-driven laminar flow.",
    )
    init.add_argument("--base-size-m", type=float)
    init.add_argument("--maximum-cells", type=int)
    init.add_argument(
        "--request",
        type=Path,
        help="Versioned JSON project-creation request; paths resolve beside the file.",
    )
    init.add_argument("--json", action="store_true", dest="as_json")

    check = subparsers.add_parser(
        "check",
        help="Validate project intent and provider compatibility without running.",
    )
    check.add_argument("project", nargs="?", type=Path, default=Path("."))
    check.add_argument("--provider", choices=("reference", "openfoam"))
    check.add_argument("--container-image")
    check.add_argument(
        "--param",
        action="append",
        type=_project_parameter,
        help="Pass NAME=JSON_SCALAR to the case.py build() factory; repeat as needed.",
    )
    check.add_argument(
        "--param-file",
        type=Path,
        help="Load agentcfd.parameter-set/0.1; explicit --param values take precedence.",
    )
    check.add_argument(
        "--summary-only",
        action="store_true",
        help="Preflight an OpenFOAM result with compact evidence and no field bundle.",
    )
    check.add_argument("--json", action="store_true", dest="as_json")

    plan = subparsers.add_parser(
        "plan",
        help="Resolve a deterministic, inspectable solution plan.",
    )
    plan.add_argument("project", nargs="?", type=Path, default=Path("."))
    plan.add_argument("--provider", choices=("reference", "openfoam"))
    plan.add_argument("--container-image")
    plan.add_argument(
        "--param",
        action="append",
        type=_project_parameter,
        help="Pass NAME=JSON_SCALAR to the case.py build() factory; repeat as needed.",
    )
    plan.add_argument(
        "--param-file",
        type=Path,
        help="Load agentcfd.parameter-set/0.1; explicit --param values take precedence.",
    )
    plan.add_argument(
        "--summary-only",
        action="store_true",
        help="Plan an OpenFOAM result with compact evidence and no field bundle.",
    )
    plan.add_argument("--output", type=Path)
    plan.add_argument("--json", action="store_true", dest="as_json")

    inspect = subparsers.add_parser(
        "inspect",
        help="Inspect project readiness and its latest structured run.",
    )
    inspect.add_argument("project", nargs="?", type=Path, default=Path("."))
    inspect.add_argument("--json", action="store_true", dest="as_json")

    geometry_check = subparsers.add_parser(
        "geometry-check",
        help="Inspect imported STL/OBJ units, bounds, regions, and topology read-only.",
    )
    geometry_check.add_argument("path", type=Path)
    geometry_check.add_argument(
        "--unit",
        choices=("m", "mm", "cm", "um", "in", "ft"),
        help="Explicit physical unit of source coordinates.",
    )
    geometry_check.add_argument(
        "--allow-open",
        action="store_true",
        help="Do not treat boundary edges as an error for an intentional open surface.",
    )
    geometry_check.add_argument(
        "--max-topology-triangles",
        type=int,
        default=1_000_000,
        help="Memory guard for edge topology (default: 1000000 triangles).",
    )
    geometry_check.add_argument(
        "--merge-tolerance",
        type=float,
        default=0.0,
        help="Explicit vertex merge tolerance in source units (default: exact).",
    )
    geometry_check.add_argument(
        "--roles",
        type=Path,
        help="Versioned JSON map from exact surface region names to CFD roles.",
    )
    geometry_check.add_argument(
        "--internal-flow",
        action="store_true",
        help="Require at least one explicitly confirmed inlet and outlet.",
    )
    geometry_check.add_argument(
        "--output",
        type=Path,
        help="Atomically write the versioned inspection JSON for case.py reuse.",
    )
    geometry_check.add_argument("--json", action="store_true", dest="as_json")

    mesh = subparsers.add_parser(
        "mesh",
        help="Plan, prepare, and verify an imported-surface OpenFOAM mesh.",
    )
    mesh.add_argument("project", nargs="?", type=Path, default=Path("."))
    mesh.add_argument(
        "--output",
        type=Path,
        help="New directory for the inspectable mesh-only OpenFOAM case.",
    )
    mesh.add_argument(
        "--plan-only",
        action="store_true",
        help="Resolve cell/refinement/quality budgets without writing or running.",
    )
    mesh.add_argument(
        "--prepare-only",
        action="store_true",
        help="Write the mesh case but do not start OpenFOAM.",
    )
    mesh.add_argument("--container-image")
    mesh.add_argument("--timeout", type=float, default=3600.0)
    mesh.add_argument(
        "--param",
        action="append",
        type=_project_parameter,
        help="Pass NAME=JSON_SCALAR to the case.py build() factory; repeat as needed.",
    )
    mesh.add_argument(
        "--param-file",
        type=Path,
        help="Load agentcfd.parameter-set/0.1; explicit --param values take precedence.",
    )
    mesh.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser(
        "status",
        help="Show project state and the single recommended next action.",
    )
    status.add_argument("project", nargs="?", type=Path, default=Path("."))
    status.add_argument(
        "--storage",
        action="store_true",
        help="Include a managed-data scan (slower for very large projects).",
    )
    status.add_argument("--json", action="store_true", dest="as_json")

    project_snapshot = subparsers.add_parser(
        "project",
        help="Show one unified project, result, output, storage, and agent surface.",
    )
    project_snapshot.add_argument(
        "project", nargs="?", type=Path, default=Path(".")
    )
    project_snapshot.add_argument(
        "--no-result",
        action="store_false",
        dest="include_result",
        help="Skip reading the compact result.json record.",
    )
    project_snapshot.add_argument(
        "--storage",
        action="store_true",
        help="Include a recursive managed-storage inventory.",
    )
    project_snapshot.add_argument("--json", action="store_true", dest="as_json")

    params = subparsers.add_parser(
        "params",
        help="Inspect or export one validated, reusable project operating point.",
    )
    params.add_argument("project", nargs="?", type=Path, default=Path("."))
    params.add_argument(
        "--param-file",
        type=Path,
        help="Load an existing agentcfd.parameter-set/0.1 baseline.",
    )
    params.add_argument(
        "--param",
        action="append",
        type=_project_parameter,
        help="Override NAME=JSON_SCALAR for this exported operating point.",
    )
    params.add_argument(
        "--output",
        type=Path,
        help="Write the resolved parameter set without replacing an existing file.",
    )
    params.add_argument("--json", action="store_true", dest="as_json")

    result_command = subparsers.add_parser(
        "result",
        help="Read compact quantities, checks, and field metadata without opening HDF5.",
    )
    result_command.add_argument("project", nargs="?", type=Path, default=Path("."))
    result_command.add_argument(
        "--run-id",
        help="Select one immutable campaign or historical run instead of the latest.",
    )
    result_command.add_argument(
        "--quantity",
        action="append",
        default=[],
        help="Return one canonical scalar quantity; repeat to select several.",
    )
    result_command.add_argument("--json", action="store_true", dest="as_json")

    watch = subparsers.add_parser(
        "watch",
        help="Follow lightweight project progress until the run reaches a terminal state.",
    )
    watch.add_argument("project", nargs="?", type=Path, default=Path("."))
    watch.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds (minimum 0.2; default: 2).",
    )
    watch.add_argument(
        "--storage",
        action="store_true",
        help="Include recursive workspace size in every snapshot (higher I/O).",
    )
    watch.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one project-status JSON object per line.",
    )

    logs = subparsers.add_parser(
        "logs",
        help="Show a bounded tail from the latest live or published solver log.",
    )
    logs.add_argument("project", nargs="?", type=Path, default=Path("."))
    logs.add_argument(
        "--command",
        dest="solver_command",
        help="Select one provider command, for example pimpleFoam or checkMesh.",
    )
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument(
        "--run-id",
        help="Inspect one immutable campaign or historical run instead of the latest.",
    )
    logs.add_argument("--json", action="store_true", dest="as_json")

    diagnose = subparsers.add_parser(
        "diagnose",
        help="Classify bounded solver evidence and recommend a safe next action.",
    )
    diagnose.add_argument("project", nargs="?", type=Path, default=Path("."))
    diagnose.add_argument(
        "--command",
        dest="solver_command",
        help="Limit diagnosis to one provider command, for example pimpleFoam.",
    )
    diagnose.add_argument(
        "--run-id",
        help="Diagnose one immutable campaign or historical run instead of the latest.",
    )
    diagnose.add_argument("--json", action="store_true", dest="as_json")

    resume = subparsers.add_parser(
        "resume",
        help="Resume an identical failed transient project from its last checkpoint.",
    )
    resume.add_argument("project", nargs="?", type=Path, default=Path("."))
    resume.add_argument("--container-image")
    resume.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Retain the new generated workspace after successful publication.",
    )
    resume.add_argument("--json", action="store_true", dest="as_json")

    storage_command = subparsers.add_parser(
        "storage",
        help="Inventory outputs, campaigns, and reclaimable temporary data.",
    )
    storage_command.add_argument("project", nargs="?", type=Path, default=Path("."))
    storage_command.add_argument("--json", action="store_true", dest="as_json")
    performance_command = subparsers.add_parser(
        "performance",
        help="Show bounded runtime history and comparable-run ETA calibration.",
    )
    performance_command.add_argument(
        "project", nargs="?", type=Path, default=Path(".")
    )
    performance_command.add_argument("--json", action="store_true", dest="as_json")

    campaigns = subparsers.add_parser(
        "campaigns",
        help="List immutable campaign design points without opening field payloads.",
    )
    campaigns.add_argument("project", nargs="?", type=Path, default=Path("."))
    campaigns.add_argument(
        "--storage",
        action="store_true",
        help="Recursively measure each campaign directory (higher I/O).",
    )
    campaigns.add_argument(
        "--export-csv",
        type=Path,
        help="Write a compact design-point table with canonical quantity columns.",
    )
    campaigns.add_argument("--json", action="store_true", dest="as_json")

    sweep = subparsers.add_parser(
        "sweep",
        help="Preflight and execute a named parameter campaign with accepted-run reuse.",
    )
    sweep.add_argument("project", type=Path)
    sweep.add_argument("request", type=Path)
    sweep.add_argument("--provider", choices=("reference", "openfoam"))
    sweep.add_argument("--container-image")
    sweep.add_argument(
        "--plan-only",
        action="store_true",
        help="Report readiness, reuse, and solve count without executing any point.",
    )
    sweep.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first runtime failure; preflight always checks every point.",
    )
    sweep.add_argument(
        "--max-runs",
        type=int,
        help="Fail before execution if more than this many new solver runs are needed.",
    )
    sweep.add_argument(
        "--summary-only",
        action="store_true",
        help="Keep quantities and evidence but skip permanent XDMF/HDF5 fields.",
    )
    sweep.add_argument("--json", action="store_true", dest="as_json")

    promote = subparsers.add_parser(
        "promote",
        help="Rerun one accepted summary-only campaign point with full fields.",
    )
    promote.add_argument("project", type=Path)
    promote.add_argument("run_id")
    promote.add_argument("--container-image")
    promote.add_argument("--json", action="store_true", dest="as_json")

    compact = subparsers.add_parser(
        "compact",
        help="Preview removal of reproducible full-field bulk from one campaign run.",
    )
    compact.add_argument("project", type=Path)
    compact.add_argument("run_id")
    compact.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the run as summary-only and remove listed field artifacts.",
    )
    compact.add_argument("--json", action="store_true", dest="as_json")

    clean = subparsers.add_parser(
        "clean",
        help="Preview removal of temporary solver workspaces while preserving results.",
    )
    clean.add_argument("project", nargs="?", type=Path, default=Path("."))
    clean.add_argument(
        "--apply",
        action="store_true",
        help="Apply the previewed cleanup; output/ and campaigns/ are always preserved.",
    )
    clean.add_argument(
        "--include-retained",
        action="store_true",
        help="Also target inactive workspaces explicitly retained by CLI or manifest.",
    )
    clean.add_argument(
        "--include-cache",
        action="store_true",
        help="Also target reusable imported-geometry mesh cache after preview.",
    )
    clean.add_argument("--json", action="store_true", dest="as_json")

    view = subparsers.add_parser(
        "view",
        help="Locate the latest result and optionally launch ParaView for XDMF fields.",
    )
    view.add_argument("project", nargs="?", type=Path, default=Path("."))
    view_selection = view.add_mutually_exclusive_group()
    view_selection.add_argument(
        "--recipe",
        help="Select a named reproducible visual or line-profile recipe.",
    )
    view_selection.add_argument(
        "--layout",
        help="Select a named multi-view engineering overview.",
    )
    view_action = view.add_mutually_exclusive_group()
    view_action.add_argument("--launch", action="store_true")
    view_action.add_argument(
        "--batch",
        action="store_true",
        help="Execute a named recipe headlessly with ParaView pvbatch.",
    )
    view.add_argument("--json", action="store_true", dest="as_json")

    catalog = subparsers.add_parser(
        "capabilities", help="Show truthful capability boundaries."
    )
    catalog.add_argument("--json", action="store_true", dest="as_json")

    benchmark_catalog = subparsers.add_parser(
        "benchmarks",
        help="Show the evidence-gated benchmark roadmap.",
    )
    benchmark_catalog.add_argument("--json", action="store_true", dest="as_json")

    contract_catalog = subparsers.add_parser(
        "contracts",
        help="Locate installed AgentCFD and AgentCAE JSON contracts.",
    )
    contract_catalog.add_argument("--json", action="store_true", dest="as_json")
    contract_catalog.add_argument(
        "--check-agentcae",
        action="store_true",
        help="Audit emitted contract identities against the optional AgentCAE package.",
    )

    license_catalog = subparsers.add_parser(
        "licenses",
        help="Show dependency and external-solver license boundaries.",
    )
    license_catalog.add_argument("--json", action="store_true", dest="as_json")

    export = subparsers.add_parser(
        "export",
        help="Export portable fields for visualization, coupling, and AI datasets.",
    )
    export_subparsers = export.add_subparsers(dest="export_format", required=True)
    field_bundle = export_subparsers.add_parser(
        "openfoam",
        help="Export OpenFOAM field frames as XDMF/H5 with optional safe NPZ.",
    )
    field_bundle.add_argument("case_directory", type=Path)
    field_bundle.add_argument("output_directory", type=Path)
    field_bundle.add_argument(
        "--container-image",
        help="Run foamToVTK through Docker, for example opencfd/openfoam-run:2606.",
    )
    field_bundle.add_argument(
        "--skip-conversion",
        action="store_true",
        help="Reuse an existing case_directory/VTK series.",
    )
    field_bundle.add_argument(
        "--density",
        type=float,
        help="Constant density used to derive fluid.pressure in Pa from kinematic p.",
    )
    field_bundle.add_argument(
        "--profile",
        choices=("visualization", "native", "both"),
        default="visualization",
        help=(
            "Point fields for ordinary viewing, native cell fields for training, "
            "or both for expert interchange."
        ),
    )
    field_bundle.add_argument(
        "--field",
        action="append",
        dest="fields",
        help="Canonical or OpenFOAM field name; repeat to select multiple fields.",
    )
    field_bundle.add_argument(
        "--with-npz",
        action="store_true",
        help="Also write a pickle-free NPZ mirror for NumPy and ML workflows.",
    )
    field_bundle.add_argument(
        "--compression",
        choices=("gzip", "lzf", "none"),
        default="gzip",
        help="HDF5 dataset compression (default: gzip).",
    )
    field_bundle.add_argument(
        "--storage-budget",
        type=outputs.parse_storage_size,
        help="Fail before export when selected fields exceed a budget such as '2 GiB'.",
    )
    field_bundle.add_argument("--timeout-seconds", type=float, default=3600.0)
    field_bundle.add_argument("--json", action="store_true", dest="as_json")
    field_sample = export_subparsers.add_parser(
        "field-sample",
        help="Extract one AgentFEM- and tensor-ready field NPZ from a bundle.",
    )
    field_sample.add_argument("bundle_directory", type=Path)
    field_sample.add_argument("output", type=Path)
    field_sample.add_argument("--field", required=True, help="Canonical field name.")
    field_sample.add_argument(
        "--association", choices=("point", "cell"), default="point"
    )
    field_sample.add_argument("--frame", type=int, default=-1)
    field_sample.add_argument("--cell-block", type=int, default=0)
    field_sample.add_argument("--json", action="store_true", dest="as_json")

    calculate = subparsers.add_parser(
        "calculate",
        help="Run dependency-free industrial engineering calculations.",
    )
    calculate_subparsers = calculate.add_subparsers(dest="calculation", required=True)
    pipe_loss = calculate_subparsers.add_parser(
        "pipe-loss",
        help="Calculate major and local incompressible pipe loss.",
    )
    pipe_flow = calculate_subparsers.add_parser(
        "pipe-flow",
        help="Invert available pipe pressure loss into velocity and flow.",
    )
    for command in (pipe_loss, pipe_flow):
        command.add_argument("--density", type=float, required=True)
        command.add_argument("--viscosity", type=float, required=True)
        command.add_argument("--length", type=float, required=True)
        command.add_argument("--diameter", type=float, required=True)
        command.add_argument("--roughness", type=float, default=0.0)
        command.add_argument("--loss-coefficient", type=float, default=0.0)
        command.add_argument("--json", action="store_true", dest="as_json")
    pipe_loss.add_argument("--velocity", type=float, required=True)
    pipe_flow.add_argument("--pressure-loss", type=float, required=True)
    pipe_flow.add_argument("--regime", choices=("laminar", "turbulent"), required=True)
    compressibility = calculate_subparsers.add_parser(
        "compressibility",
        help="Screen Mach number for an incompressible flow model.",
    )
    compressibility.add_argument("--velocity", type=float, required=True)
    compressibility.add_argument("--speed-of-sound", type=float, required=True)
    compressibility.add_argument(
        "--maximum-incompressible-mach",
        type=float,
        default=0.3,
    )
    compressibility.add_argument("--json", action="store_true", dest="as_json")
    wall_resolution = calculate_subparsers.add_parser(
        "wall-resolution",
        help="Estimate turbulent-pipe wall spacing for a target y-plus.",
    )
    wall_resolution.add_argument("--density", type=float, required=True)
    wall_resolution.add_argument("--viscosity", type=float, required=True)
    wall_resolution.add_argument("--velocity", type=float, required=True)
    wall_resolution.add_argument("--diameter", type=float, required=True)
    wall_resolution.add_argument("--target-y-plus", type=float, required=True)
    wall_resolution.add_argument("--roughness", type=float, default=0.0)
    wall_resolution.add_argument("--json", action="store_true", dest="as_json")
    thermal_screen = calculate_subparsers.add_parser(
        "thermal-flow",
        help="Screen constant-property internal-flow heat transport.",
    )
    thermal_screen.add_argument("--density", type=float, required=True)
    thermal_screen.add_argument("--viscosity", type=float, required=True)
    thermal_screen.add_argument("--specific-heat", type=float, required=True)
    thermal_screen.add_argument("--thermal-conductivity", type=float, required=True)
    thermal_screen.add_argument("--velocity", type=float, required=True)
    thermal_screen.add_argument("--diameter", type=float, required=True)
    thermal_screen.add_argument("--flow-area", type=float, required=True)
    thermal_screen.add_argument("--inlet-temperature", type=float, required=True)
    thermal_screen.add_argument("--heat-rate", type=float, required=True)
    thermal_screen.add_argument(
        "--maximum-temperature-change-fraction",
        type=float,
        default=0.05,
    )
    thermal_screen.add_argument("--json", action="store_true", dest="as_json")

    property_command = subparsers.add_parser(
        "properties",
        help="Evaluate an auditable optional thermophysical-property state.",
    )
    property_subparsers = property_command.add_subparsers(
        dest="property_operation",
        required=True,
    )
    property_state = property_subparsers.add_parser(
        "state",
        help="Evaluate a CoolProp pressure-temperature state in SI units.",
    )
    property_state.add_argument("--fluid", required=True)
    property_state.add_argument("--pressure", type=float, required=True)
    property_state.add_argument("--temperature", type=float, required=True)
    property_state.add_argument("--json", action="store_true", dest="as_json")

    demo = subparsers.add_parser("demo", help="Run a bundled verified workflow.")
    demo_subparsers = demo.add_subparsers(dest="demo", required=True)
    pipe = demo_subparsers.add_parser(
        "pipe", help="Run the laminar circular-pipe reference workflow."
    )
    pipe.add_argument("--output", type=Path, default=Path("agentcfd-pipe-result.json"))

    prepare = subparsers.add_parser(
        "prepare", help="Generate a provider case without executing it."
    )
    prepare_subparsers = prepare.add_subparsers(dest="provider", required=True)
    openfoam = prepare_subparsers.add_parser(
        "openfoam-pipe",
        help="Generate the experimental OpenFOAM laminar-pipe case.",
    )
    openfoam.add_argument("case_directory", type=Path)
    openfoam.add_argument("--json", action="store_true", dest="as_json")
    openfoam.add_argument(
        "--fully-developed",
        action="store_true",
        help="Use a declared analytic laminar inlet profile.",
    )
    openfoam.add_argument(
        "--cross-section-cells",
        type=int,
        default=8,
        help="Cells along each O-grid block direction (default: 8).",
    )
    openfoam.add_argument(
        "--axial-cells",
        type=int,
        help="Cells along the pipe; defaults to a bounded geometry-based value.",
    )
    openfoam.add_argument(
        "--nominal-wall-cell-fraction",
        type=float,
        help="Wall-adjacent cell width divided by the nominal outer O-grid edge.",
    )
    turbulent_prepare = prepare_subparsers.add_parser(
        "openfoam-turbulent-pipe",
        help="Generate the experimental OpenFOAM k-omega SST smooth-pipe case.",
    )
    turbulent_prepare.add_argument("case_directory", type=Path)
    turbulent_prepare.add_argument("--velocity", type=float, default=1.0)
    turbulent_prepare.add_argument("--turbulence-intensity", type=float, default=0.05)
    turbulent_prepare.add_argument(
        "--turbulence-length-scale", type=float, default=0.007
    )
    turbulent_prepare.add_argument("--cross-section-cells", type=int, default=8)
    turbulent_prepare.add_argument("--axial-cells", type=int, default=120)
    turbulent_prepare.add_argument("--nominal-wall-cell-fraction", type=float)
    turbulent_prepare.add_argument(
        "--precursor-case",
        type=Path,
        help="Accepted periodic precursor case used for developed-field mapping.",
    )
    turbulent_prepare.add_argument("--json", action="store_true", dest="as_json")
    precursor_prepare = prepare_subparsers.add_parser(
        "openfoam-turbulent-precursor",
        help="Generate a periodic two-equation RANS circular-pipe precursor.",
    )
    precursor_prepare.add_argument("case_directory", type=Path)
    precursor_prepare.add_argument("--velocity", type=float, default=1.0)
    precursor_prepare.add_argument("--turbulence-intensity", type=float, default=0.05)
    precursor_prepare.add_argument(
        "--turbulence-length-scale", type=float, default=0.007
    )
    precursor_prepare.add_argument(
        "--turbulence-model",
        choices=("k-omega-sst", "k-epsilon"),
        default="k-omega-sst",
    )
    precursor_prepare.add_argument("--cross-section-cells", type=int, default=8)
    precursor_prepare.add_argument("--nominal-wall-cell-fraction", type=float)
    precursor_prepare.add_argument(
        "--nut-wall-function",
        choices=(
            "nutUBlendedWallFunction",
            "nutUSpaldingWallFunction",
            "nutkWallFunction",
        ),
        default=None,
        help="OpenFOAM momentum wall function; defaults by turbulence model.",
    )
    precursor_prepare.add_argument("--maximum-iterations", type=int, default=1000)
    precursor_prepare.add_argument("--json", action="store_true", dest="as_json")
    wall_prepare = prepare_subparsers.add_parser(
        "openfoam-turbulent-wall-study",
        help="Prepare a fixed-wall-cell three-grid turbulent precursor study.",
    )
    wall_prepare.add_argument("directory", type=Path)
    wall_prepare.add_argument(
        "--cross-section-cells",
        nargs=3,
        type=int,
        default=(8, 16, 32),
        metavar=("COARSE", "MEDIUM", "FINE"),
    )
    wall_prepare.add_argument(
        "--nominal-wall-cell-fraction",
        type=float,
        default=0.0625,
    )
    wall_prepare.add_argument(
        "--maximum-iterations",
        nargs=3,
        type=int,
        default=(1000, 4000, 6000),
        metavar=("COARSE", "MEDIUM", "FINE"),
    )
    wall_prepare.add_argument(
        "--nut-wall-function",
        choices=(
            "nutUBlendedWallFunction",
            "nutUSpaldingWallFunction",
            "nutkWallFunction",
        ),
        default="nutUBlendedWallFunction",
    )
    wall_prepare.add_argument("--json", action="store_true", dest="as_json")
    wall_function_prepare = prepare_subparsers.add_parser(
        "openfoam-turbulent-wall-function-study",
        help="Prepare an identical-mesh SST momentum wall-function study.",
    )
    wall_function_prepare.add_argument("directory", type=Path)
    wall_function_prepare.add_argument("--cross-section-cells", type=int, default=16)
    wall_function_prepare.add_argument(
        "--nominal-wall-cell-fraction",
        type=float,
        default=0.0625,
    )
    wall_function_prepare.add_argument("--maximum-iterations", type=int, default=4000)
    wall_function_prepare.add_argument("--json", action="store_true", dest="as_json")
    model_study_prepare = prepare_subparsers.add_parser(
        "openfoam-turbulent-model-study",
        help="Prepare an identical-mesh SST versus k-epsilon model screen.",
    )
    model_study_prepare.add_argument("directory", type=Path)
    model_study_prepare.add_argument("--velocity", type=float, default=1.0)
    model_study_prepare.add_argument("--turbulence-intensity", type=float, default=0.05)
    model_study_prepare.add_argument(
        "--turbulence-length-scale", type=float, default=0.007
    )
    model_study_prepare.add_argument("--cross-section-cells", type=int, default=16)
    wall_selection = model_study_prepare.add_mutually_exclusive_group()
    wall_selection.add_argument(
        "--nominal-wall-cell-fraction",
        type=float,
    )
    wall_selection.add_argument(
        "--target-y-plus",
        type=float,
        help="Derive the wall-cell fraction from a smooth-pipe preflight estimate.",
    )
    model_study_prepare.add_argument("--maximum-iterations", type=int, default=4000)
    model_study_prepare.add_argument("--json", action="store_true", dest="as_json")
    model_sweep_prepare = prepare_subparsers.add_parser(
        "openfoam-turbulent-model-sweep",
        help="Prepare a multi-Re SST versus k-epsilon campaign.",
    )
    model_sweep_prepare.add_argument("directory", type=Path)
    model_sweep_prepare.add_argument(
        "--velocities",
        nargs="+",
        type=float,
        default=(0.5, 1.0, 2.0, 5.0),
    )
    model_sweep_prepare.add_argument("--target-y-plus", type=float, default=40.0)
    model_sweep_prepare.add_argument("--turbulence-intensity", type=float, default=0.05)
    model_sweep_prepare.add_argument(
        "--turbulence-length-scale", type=float, default=0.007
    )
    model_sweep_prepare.add_argument("--cross-section-cells", type=int, default=16)
    model_sweep_prepare.add_argument("--maximum-iterations", type=int, default=4000)
    model_sweep_prepare.add_argument("--json", action="store_true", dest="as_json")
    grid_prepare = prepare_subparsers.add_parser(
        "openfoam-pipe-grid",
        help="Prepare a same-model three-grid fully developed pipe study.",
    )
    grid_prepare.add_argument("directory", type=Path)
    grid_prepare.add_argument(
        "--cross-section-cells",
        nargs=3,
        type=int,
        default=(8, 16, 32),
        metavar=("COARSE", "MEDIUM", "FINE"),
        help="O-grid block counts; the validated default is 8/16/32.",
    )
    grid_prepare.add_argument(
        "--base-axial-cells",
        type=int,
        default=40,
        help="Coarse-grid axial cells (validated default: 40).",
    )
    grid_prepare.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser(
        "run", help="Prepare, execute, and recover a provider result."
    )
    run_subparsers = run.add_subparsers(dest="provider", required=True)
    project_run = run_subparsers.add_parser(
        "project",
        help="Execute a project into replaceable output or an immutable campaign run.",
    )
    project_run.add_argument("project", nargs="?", type=Path, default=Path("."))
    project_run.add_argument(
        "--provider",
        choices=("reference", "openfoam"),
        dest="project_provider",
    )
    project_run.add_argument("--container-image")
    project_run.add_argument(
        "--param",
        action="append",
        type=_project_parameter,
        help="Pass NAME=JSON_SCALAR to build(); repeat for a design point.",
    )
    project_run.add_argument(
        "--param-file",
        type=Path,
        help="Load agentcfd.parameter-set/0.1; explicit --param values take precedence.",
    )
    project_run.add_argument(
        "--summary-only",
        action="store_true",
        help="Keep compact evidence but skip permanent XDMF/HDF5 fields.",
    )
    project_run.add_argument(
        "--campaign",
        action="store_true",
        help="Preserve this run under campaigns/<run-id> instead of replacing output/.",
    )
    project_run.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Retain the hidden generated OpenFOAM workspace for expert debugging.",
    )
    project_run.add_argument("--json", action="store_true", dest="as_json")
    run_openfoam = run_subparsers.add_parser(
        "openfoam-pipe",
        help="Run and recover the experimental OpenFOAM laminar-pipe case.",
    )
    run_openfoam.add_argument("case_directory", type=Path)
    run_openfoam.add_argument("--result", type=Path)
    run_openfoam.add_argument(
        "--container-image",
        help="Run OpenFOAM through Docker, for example opencfd/openfoam-run:2606.",
    )
    run_openfoam.add_argument(
        "--timeout-seconds",
        type=float,
        default=3600.0,
        help="Maximum wall time for each external command (default: 3600).",
    )
    run_openfoam.add_argument("--json", action="store_true", dest="as_json")
    run_openfoam.add_argument(
        "--prepared",
        action="store_true",
        help="Verify hashes and execute an existing AgentCFD-prepared case.",
    )
    run_openfoam.add_argument(
        "--fully-developed",
        action="store_true",
        help="Use a declared analytic laminar inlet profile.",
    )
    run_openfoam.add_argument(
        "--cross-section-cells",
        type=int,
        default=8,
        help="Cells along each O-grid block direction (default: 8).",
    )
    run_openfoam.add_argument(
        "--axial-cells",
        type=int,
        help="Cells along the pipe; defaults to a bounded geometry-based value.",
    )
    run_openfoam.add_argument("--nominal-wall-cell-fraction", type=float)
    turbulent_run = run_subparsers.add_parser(
        "openfoam-turbulent-pipe",
        help="Run the experimental OpenFOAM k-omega SST smooth-pipe case.",
    )
    turbulent_run.add_argument("case_directory", type=Path)
    turbulent_run.add_argument("--result", type=Path)
    turbulent_run.add_argument("--velocity", type=float, default=1.0)
    turbulent_run.add_argument("--turbulence-intensity", type=float, default=0.05)
    turbulent_run.add_argument("--turbulence-length-scale", type=float, default=0.007)
    turbulent_run.add_argument("--cross-section-cells", type=int, default=8)
    turbulent_run.add_argument("--axial-cells", type=int, default=120)
    turbulent_run.add_argument("--nominal-wall-cell-fraction", type=float)
    turbulent_run.add_argument(
        "--precursor-case",
        type=Path,
        help="Accepted periodic precursor case used for developed-field mapping.",
    )
    turbulent_run.add_argument("--container-image")
    turbulent_run.add_argument("--timeout-seconds", type=float, default=3600.0)
    turbulent_run.add_argument("--prepared", action="store_true")
    turbulent_run.add_argument("--json", action="store_true", dest="as_json")
    precursor_run = run_subparsers.add_parser(
        "openfoam-turbulent-precursor",
        help="Run a periodic two-equation RANS circular-pipe precursor.",
    )
    precursor_run.add_argument("case_directory", type=Path)
    precursor_run.add_argument("--result", type=Path)
    precursor_run.add_argument("--velocity", type=float, default=1.0)
    precursor_run.add_argument("--turbulence-intensity", type=float, default=0.05)
    precursor_run.add_argument("--turbulence-length-scale", type=float, default=0.007)
    precursor_run.add_argument(
        "--turbulence-model",
        choices=("k-omega-sst", "k-epsilon"),
        default="k-omega-sst",
    )
    precursor_run.add_argument("--cross-section-cells", type=int, default=8)
    precursor_run.add_argument("--nominal-wall-cell-fraction", type=float)
    precursor_run.add_argument(
        "--nut-wall-function",
        choices=(
            "nutUBlendedWallFunction",
            "nutUSpaldingWallFunction",
            "nutkWallFunction",
        ),
        default=None,
    )
    precursor_run.add_argument("--maximum-iterations", type=int, default=1000)
    precursor_run.add_argument("--container-image")
    precursor_run.add_argument("--timeout-seconds", type=float, default=3600.0)
    precursor_run.add_argument("--prepared", action="store_true")
    precursor_run.add_argument("--json", action="store_true", dest="as_json")
    wall_run = run_subparsers.add_parser(
        "openfoam-turbulent-wall-study",
        help="Execute a prepared fixed-wall-cell turbulent precursor study.",
    )
    wall_run.add_argument("directory", type=Path)
    wall_run.add_argument("--container-image")
    wall_run.add_argument("--timeout-seconds", type=float, default=3600.0)
    wall_run.add_argument("--json", action="store_true", dest="as_json")
    wall_function_run = run_subparsers.add_parser(
        "openfoam-turbulent-wall-function-study",
        help="Execute a prepared SST momentum wall-function study.",
    )
    wall_function_run.add_argument("directory", type=Path)
    wall_function_run.add_argument("--container-image")
    wall_function_run.add_argument("--timeout-seconds", type=float, default=3600.0)
    wall_function_run.add_argument("--json", action="store_true", dest="as_json")
    model_study_run = run_subparsers.add_parser(
        "openfoam-turbulent-model-study",
        help="Execute a prepared SST versus k-epsilon model screen.",
    )
    model_study_run.add_argument("directory", type=Path)
    model_study_run.add_argument("--container-image")
    model_study_run.add_argument("--timeout-seconds", type=float, default=3600.0)
    model_study_run.add_argument("--json", action="store_true", dest="as_json")
    model_sweep_run = run_subparsers.add_parser(
        "openfoam-turbulent-model-sweep",
        help="Execute or resume a prepared multi-Re turbulence-model campaign.",
    )
    model_sweep_run.add_argument("directory", type=Path)
    model_sweep_run.add_argument("--container-image")
    model_sweep_run.add_argument("--timeout-seconds", type=float, default=3600.0)
    model_sweep_run.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not reuse complete point assessments.",
    )
    model_sweep_run.set_defaults(resume=True)
    model_sweep_run.add_argument("--json", action="store_true", dest="as_json")
    run_grid = run_subparsers.add_parser(
        "openfoam-pipe-grid",
        help="Execute a prepared three-grid pipe study and compute GCI.",
    )
    run_grid.add_argument("directory", type=Path)
    run_grid.add_argument(
        "--container-image",
        help="Run all three cases through the selected Docker image.",
    )
    run_grid.add_argument(
        "--timeout-seconds",
        type=float,
        default=3600.0,
        help="Maximum wall time for each external command in each case.",
    )
    run_grid.add_argument("--json", action="store_true", dest="as_json")

    verify = subparsers.add_parser(
        "verify", help="Create numerical verification evidence."
    )
    verify_subparsers = verify.add_subparsers(dest="verification", required=True)
    grid = verify_subparsers.add_parser(
        "grid-convergence",
        help="Compute a three-result Richardson extrapolation and GCI.",
    )
    grid.add_argument("results", nargs=3, type=Path)
    grid.add_argument("--quantity", required=True)
    grid.add_argument("--json", action="store_true", dest="as_json")
    time_step = verify_subparsers.add_parser(
        "time-step-sensitivity",
        help="Compare two otherwise matched transient result records.",
    )
    time_step.add_argument("results", nargs=2, type=Path)
    time_step.add_argument("--quantity", required=True)
    time_step.add_argument(
        "--maximum-relative-change",
        type=float,
        default=0.02,
    )
    time_step.add_argument("--output", type=Path)
    time_step.add_argument("--json", action="store_true", dest="as_json")
    wall_study = verify_subparsers.add_parser(
        "turbulent-wall-study",
        help="Assess fixed-wall-cell precursor results without misusing GCI.",
    )
    wall_study.add_argument("results", nargs="+", type=Path)
    wall_study.add_argument("--output", type=Path)
    wall_study.add_argument("--json", action="store_true", dest="as_json")
    precursor_grid = verify_subparsers.add_parser(
        "turbulent-precursor-grid-study",
        help="Assess a uniform, geometrically similar precursor GCI candidate.",
    )
    precursor_grid.add_argument("results", nargs=3, type=Path)
    precursor_grid.add_argument("--output", type=Path)
    precursor_grid.add_argument("--json", action="store_true", dest="as_json")
    wall_function_study = verify_subparsers.add_parser(
        "turbulent-wall-function-study",
        help="Compare supported SST momentum wall functions on one identical mesh.",
    )
    wall_function_study.add_argument("results", nargs=3, type=Path)
    wall_function_study.add_argument("--output", type=Path)
    wall_function_study.add_argument("--json", action="store_true", dest="as_json")
    model_study = verify_subparsers.add_parser(
        "turbulent-model-study",
        help="Assess an identical-mesh SST versus k-epsilon model screen.",
    )
    model_study.add_argument("results", nargs=2, type=Path)
    model_study.add_argument("--output", type=Path)
    model_study.add_argument("--json", action="store_true", dest="as_json")
    model_sweep = verify_subparsers.add_parser(
        "turbulent-model-sweep",
        help="Aggregate at least three identical-mesh turbulence-model studies.",
    )
    model_sweep.add_argument("studies", nargs="+", type=Path)
    model_sweep.add_argument("--output", type=Path)
    model_sweep.add_argument("--json", action="store_true", dest="as_json")
    result_check = verify_subparsers.add_parser(
        "result",
        help="Verify a result's trust state and content-addressed artifacts.",
    )
    result_check.add_argument("result", type=Path)
    result_check.add_argument("--json", action="store_true", dest="as_json")
    project_check = verify_subparsers.add_parser(
        "project",
        help="Verify one published project run, result, and optional XDMF/H5 bundle.",
    )
    project_check.add_argument("project", nargs="?", type=Path, default=Path("."))
    project_check.add_argument(
        "--run-id",
        help="Select one immutable campaign run instead of the latest project result.",
    )
    project_check.add_argument("--json", action="store_true", dest="as_json")
    bundle_check = verify_subparsers.add_parser(
        "field-bundle",
        help="Verify XDMF/H5 and any selected NPZ hashes and frame identity.",
    )
    bundle_check.add_argument("directory", type=Path)
    bundle_check.add_argument("--json", action="store_true", dest="as_json")
    validation_point = verify_subparsers.add_parser(
        "validation-point",
        help="Compare one simulated observable with reference data and uncertainty.",
    )
    validation_point.add_argument("--simulation", type=float, required=True)
    validation_point.add_argument("--reference", type=float, required=True)
    validation_point.add_argument("--numerical-uncertainty", type=float, required=True)
    validation_point.add_argument("--input-uncertainty", type=float, required=True)
    validation_point.add_argument(
        "--experimental-uncertainty", type=float, required=True
    )
    validation_point.add_argument("--coverage-factor", type=float, default=2.0)
    validation_point.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    selected_argv = list(sys.argv[1:] if argv is None else argv)
    legacy_run_targets = {
        "project",
        "openfoam-pipe",
        "openfoam-turbulent-pipe",
        "openfoam-turbulent-precursor",
        "openfoam-turbulent-wall-study",
        "openfoam-turbulent-wall-function-study",
        "openfoam-turbulent-model-study",
        "openfoam-turbulent-model-sweep",
        "openfoam-pipe-grid",
    }
    if (
        selected_argv
        and selected_argv[0] == "run"
        and (
            len(selected_argv) == 1
            or (
                selected_argv[1] not in legacy_run_targets
                and selected_argv[1] not in {"-h", "--help"}
            )
        )
    ):
        selected_argv.insert(1, "project")
    args = build_parser().parse_args(selected_argv)
    if args.command == "doctor":
        report = (
            _doctor()
            if args.project is None
            else projects.Project.discover(args.project).doctor()
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        elif args.project is not None:
            print(
                f"Project doctor | {str(report['state']).upper()} | healthy "
                f"{str(report['healthy']).lower()}"
            )
            resource = report["resource_estimate"]
            print(
                f"resource proxy: {resource['estimated_mesh_cells']} cells | "
                f"{resource['nominal_solver_steps']} nominal steps | "
                f"{resource['cell_updates_proxy']} cell-updates"
            )
            for check in report["checks"]:
                if check["status"] == "failed":
                    print(
                        f"{check['severity']}: {check['code']} | "
                        f"{check['repair']}"
                    )
            print(f"next: {report['next_action']['command']}")
        else:
            print(
                f"AgentCFD {report['agentcfd']} | Python {report['python']} | healthy"
            )
            print(
                f"Reference provider: ready | OpenFOAM runtime: {'found' if report['providers']['openfoam-runtime'] else 'not found (optional)'}"
            )
            print(
                "Portable XDMF/HDF5: "
                + (
                    "ready"
                    if report["providers"]["portable-xdmf-hdf5"]
                    else "not installed"
                )
            )
            for action in report["next_actions"]:
                print(f"next: {action}")
        return 0 if report["healthy"] else 3
    if args.command == "init":
        inline_options = (
            args.template,
            args.provider,
            args.geometry,
            args.unit,
            args.roles,
            True if args.accept_name_roles else None,
            args.interior_point_m,
            args.inlet_velocity_m_s,
            args.inlet_mass_flow_kg_s,
            args.inlet_total_gauge_pressure_pa,
            args.base_size_m,
            args.maximum_cells,
        )
        request_sha256 = None
        if args.request is not None:
            if any(value is not None for value in inline_options):
                raise ProjectError(
                    "--request cannot be combined with inline project creation options."
                )
            try:
                request = strict_json_object(
                    args.request.read_text(encoding="utf-8"),
                    label="project creation request",
                )
            except OSError as error:
                raise ProjectError(
                    f"Cannot read project creation request {args.request}: {error}"
                ) from error
            project = projects.init_project_from_request(
                args.directory,
                request,
                base_directory=args.request.parent,
            )
            selected_template = str(request["template"])
            request_sha256 = content_fingerprint(request)
        else:
            selected_template = args.template or "industrial-pipe"
            project = projects.init_project(
                args.directory,
                provider=args.provider
                or (
                    "openfoam"
                    if selected_template
                    in {"heated-pipe", "baffle-channel", "imported-internal-flow"}
                    else "reference"
                ),
                template=selected_template,
                geometry_path=args.geometry,
                geometry_unit=args.unit,
                boundary_roles=_boundary_role_map(args.roles),
                accept_name_roles=args.accept_name_roles,
                interior_point_m=(
                    tuple(args.interior_point_m)
                    if args.interior_point_m is not None
                    else None
                ),
                inlet_velocity_m_s=(
                    tuple(args.inlet_velocity_m_s)
                    if args.inlet_velocity_m_s is not None
                    else None
                ),
                inlet_mass_flow_kg_s=args.inlet_mass_flow_kg_s,
                inlet_total_gauge_pressure_pa=args.inlet_total_gauge_pressure_pa,
                base_size_m=args.base_size_m,
                maximum_cells=args.maximum_cells,
            )
        report = {
            "schema": "agentcfd.project-initialization/0.1",
            "template": selected_template,
            "root": str(project.root),
            "entrypoint": str(project.entrypoint),
            "provider": project.manifest.default_provider,
            "request_sha256": request_sha256,
            "next_action": {
                "command": f"agentcfd status {shlex.quote(str(project.root))}",
                "reason": "Inspect readiness and follow the single recommended action.",
            },
        }
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Created AgentCFD {selected_template} project")
            print(project.root)
            print(f"next: {report['next_action']['command']}")
        return 0
    if args.command == "check":
        plan = projects.Project(args.project).plan(
            provider=args.provider,
            container_image=args.container_image,
            parameters=_project_parameters(args.param, args.param_file),
            portable_fields=False if args.summary_only else None,
        )
        readiness = plan["readiness"]
        valid = readiness["model_valid"] and readiness["provider_compatible"]
        report = {
            "schema": "agentcfd.project-check/0.1",
            "valid": valid,
            "readiness": readiness,
            "issues": plan["issues"],
            "model": plan["model"],
            "plan_sha256": plan["plan_sha256"],
        }
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Project {'valid' if valid else 'invalid'} | ready to run "
                f"{str(readiness['ready_to_run']).lower()}"
            )
            for issue in plan["issues"]:
                print(f"{issue['severity']}: {issue['code']} | {issue['message']}")
        return 0 if valid else 3
    if args.command == "plan":
        report = projects.Project(args.project).plan(
            provider=args.provider,
            container_image=args.container_image,
            parameters=_project_parameters(args.param, args.param_file),
            portable_fields=False if args.summary_only else None,
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Solution plan | provider {report['decisions']['provider']['name']} | "
                f"ready {str(report['readiness']['ready_to_run']).lower()}"
            )
            if args.output is not None:
                print(args.output)
        return 0 if report["readiness"]["ready_to_run"] else 3
    if args.command == "inspect":
        report = projects.Project(args.project).inspect()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"{report['model']['name']} | runs {report['run_count']} | "
                f"ready {str(report['readiness']['ready_to_run']).lower()}"
            )
            if report["latest_run"] is not None:
                print(
                    f"latest {report['latest_run']['run_id']} | "
                    f"trust {report['latest_run']['trust_level']}"
                )
        return 0
    if args.command == "geometry-check":
        report = geometry_io.inspect_geometry(
            args.path,
            unit=args.unit,
            require_watertight=not args.allow_open,
            topology_triangle_limit=args.max_topology_triangles,
            merge_tolerance=args.merge_tolerance,
            boundary_roles=_boundary_role_map(args.roles),
            internal_flow=args.internal_flow,
        )
        if args.output is not None:
            _write_json_atomic(args.output, report)
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            surface = report["surface"]
            print(
                f"Geometry {report['source']['format']} | "
                f"{surface['triangle_count']} triangles | "
                f"watertight {str(surface['watertight']).lower()}"
            )
            if surface["dimensions_m"] is not None:
                dimensions = " × ".join(
                    f"{float(value):.6g}" for value in surface["dimensions_m"]
                )
                print(f"size: {dimensions} m")
            confirmed_roles = report["boundary_roles"]["confirmed"]
            region_metrics = surface["region_metrics"]
            if isinstance(confirmed_roles, dict) and isinstance(region_metrics, dict):
                for name, role in sorted(confirmed_roles.items()):
                    metric = region_metrics.get(name)
                    if role not in {"inlet", "outlet"} or not isinstance(metric, dict):
                        continue
                    area = metric.get("area_m2")
                    normal = metric.get("mean_unit_normal")
                    if isinstance(area, (int, float)) and isinstance(normal, list):
                        direction = ", ".join(f"{float(value):.4g}" for value in normal)
                        print(
                            f"{role} {name}: area {float(area):.6g} m^2 | "
                            f"mean normal ({direction})"
                        )
            for issue in report["issues"]:
                print(f"{issue['severity']}: {issue['code']} | {issue['repair']}")
            print(f"next: {report['next_action']['message']}")
        ready = report["readiness"]["geometry_ready"] and (
            args.roles is None or report["readiness"]["boundary_roles_ready"]
        )
        return 0 if ready else 3
    if args.command == "mesh":
        project = projects.Project.discover(args.project)
        step = project.load_step(_project_parameters(args.param, args.param_file))
        domain = step.model.domain
        if not isinstance(domain, geometry.ImportedSurface):
            raise ProjectError("The mesh command requires ImportedSurface geometry.")
        source = projects._safe_project_path(
            project.root,
            domain.asset,
            label="imported surface asset",
        )
        if not source.is_file():
            raise ProjectError(f"Imported geometry asset is missing: {domain.asset}.")
        if "sha256:" + file_sha256(source) != domain.source_sha256:
            raise ProjectError(
                "Imported geometry bytes changed; reinspect and explicitly update model intent."
            )
        mesh_plan = plan_imported_mesh(step)
        if args.plan_only:
            report = mesh_plan.to_dict()
            if args.as_json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(
                    "Imported mesh plan | background "
                    f"{report['background_cell_count']} cells | hard maximum "
                    f"{report['maximum_cells']}"
                )
                print("next: rerun with --output DIRECTORY")
            return 0
        if args.output is None:
            raise ProjectError(
                "Imported mesh preparation requires an explicit new --output directory."
            )
        prepared = prepare_imported_mesh(
            step,
            source=source,
            directory=args.output,
        )
        if args.prepare_only:
            report = prepared.to_dict()
            accepted = True
        else:
            image = args.container_image or project.manifest.openfoam.get(
                "container_image"
            )
            result = execute_imported_mesh(
                prepared,
                container_image=str(image) if image else None,
                timeout_seconds=args.timeout,
            )
            report = result.to_dict()
            accepted = result.accepted
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            mode = "prepared" if args.prepare_only else "accepted" if accepted else "failed"
            print(f"Imported mesh {mode} | {prepared.directory}")
            if not args.prepare_only:
                print(
                    f"checks {len(report['checks'])} | "
                    f"accepted {str(accepted).lower()}"
                )
        return 0 if accepted else 3
    if args.command == "watch":
        if not math.isfinite(args.interval) or args.interval < 0.2:
            raise ValueError(
                "Watch interval must be a finite value of at least 0.2 seconds."
            )
        project = projects.Project.discover(args.project)
        try:
            while True:
                report = project.status(include_storage=args.storage)
                if args.as_json:
                    print(json.dumps(report, sort_keys=True), flush=True)
                else:
                    print(_watch_summary(report), flush=True)
                if report["state"] != "running":
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 130
        return 0 if report["state"] not in {"blocked", "failed"} else 3
    if args.command == "logs":
        report = projects.Project.discover(args.project).logs(
            command=args.solver_command,
            lines=args.lines,
            run_id=args.run_id,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"{report['command']} | {report['source']} | "
                f"last {report['returned_lines']} lines"
            )
            print(report["path"])
            if report["tail"]:
                print(report["tail"], end="")
            print(f"next: {report['next_action']['command']}")
        return 0
    if args.command == "diagnose":
        report = projects.Project.discover(args.project).diagnose(
            command=args.solver_command,
            run_id=args.run_id,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            primary = report["primary_finding"]
            if primary is None:
                print("No known deterministic failure signature found.")
            else:
                print(
                    f"{primary['code']} | {primary['confidence']} confidence | "
                    f"{primary['title']}"
                )
                evidence = primary["evidence"]
                print(
                    f"evidence: {evidence['command']}:{evidence['line']} | "
                    f"{evidence['excerpt']}"
                )
                print(f"repair: {primary['repair']}")
            recovery = report["recovery"]
            if recovery["available"]:
                coordinate = recovery["coordinate"]
                print(
                    f"checkpoint: {coordinate['value']:g} {coordinate['unit']} | "
                    f"{recovery['source']}"
                )
                print(
                    "after repair: "
                    f"{report['resume_after_repair']['command']}"
                )
            print(f"next: {report['next_action']['command']}")
        return 0 if report["primary_finding"] is not None else 2
    if args.command == "resume":
        completed = projects.Project.discover(args.project).resume(
            container_image=args.container_image,
            keep_workspace=args.keep_workspace,
        )
        report = completed.to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            resumed_from = completed.result.quantities.get(
                "restart.resumed_from_time"
            )
            detail = (
                ""
                if resumed_from is None
                else f" | resumed from {resumed_from.value:g} {resumed_from.unit}"
            )
            print(
                f"Project resume {completed.result.status} | trust "
                f"{completed.result.trust_level} | accepted "
                f"{str(completed.result.accepted).lower()}{detail}"
            )
            print(completed.directory)
        if completed.result.accepted:
            return 0
        return 1 if completed.result.status != "completed" else 3
    if args.command == "status":
        report = projects.Project(args.project).status(include_storage=args.storage)
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            latest = report["latest_run"]
            decision = ""
            if isinstance(latest, dict) and latest.get("accepted") is not None:
                decision = f" | accepted {str(latest['accepted']).lower()}"
            print(
                f"{report['model']['name']} | {str(report['state']).upper()}{decision}"
            )
            adjustable = [
                item
                for item in report["parameters"]
                if isinstance(item, dict) and item.get("overrideable") is True
            ]
            if adjustable:
                visible = adjustable[:6]
                rendered_items = []
                for item in visible:
                    metadata = item.get("metadata")
                    unit = (
                        metadata.get("unit")
                        if isinstance(metadata, dict) and item["current"] is not None
                        else None
                    )
                    rendered_items.append(
                        f"{item['name']}={json.dumps(item['current'], sort_keys=True)}"
                        + (f" {unit}" if unit not in {None, "1"} else "")
                    )
                rendered = ", ".join(rendered_items)
                remaining = len(adjustable) - len(visible)
                if remaining:
                    rendered += f", +{remaining} more"
                print(f"adjustable: {rendered}")
                print("change with: agentcfd run . --param NAME=JSON")
            if isinstance(latest, dict) and report["state"] in {
                "running",
                "interrupted",
                "failed",
            }:
                print(f"phase: {latest.get('phase', latest.get('status', 'unknown'))}")
            if isinstance(latest, dict) and latest.get("parameters"):
                parameters = latest["parameters"]
                assert isinstance(parameters, dict)
                rendered = ", ".join(
                    f"{name}={json.dumps(value, sort_keys=True)}"
                    for name, value in sorted(parameters.items())
                )
                suffix = (
                    " | current case.py defaults differ"
                    if report["inputs_changed"]
                    else ""
                )
                print(f"latest parameters: {rendered}{suffix}")
            if isinstance(latest, dict) and latest.get("mesh_acquisition"):
                print(f"mesh: {latest['mesh_acquisition']}")
            progress = report["progress"]
            if isinstance(progress, dict):
                command = progress.get("current_command")
                coordinate = progress["coordinate"]
                parts = []
                if command:
                    parts.append(str(command))
                current = coordinate.get("current")
                target = coordinate.get("target")
                if current is not None and target is not None:
                    unit = " s" if coordinate.get("unit") == "s" else ""
                    fraction = coordinate.get("fraction")
                    percent = (
                        "" if fraction is None else f" ({100.0 * float(fraction):.1f}%)"
                    )
                    parts.append(f"{current:g}{unit} / {target:g}{unit}{percent}")
                if progress.get("elapsed_display"):
                    parts.append(f"elapsed {progress['elapsed_display']}")
                if parts:
                    print("progress: " + " | ".join(parts))
                residuals = progress.get("latest_residuals", {})
                if isinstance(residuals, dict) and residuals:
                    worst_name, worst = max(
                        residuals.items(),
                        key=lambda item: float(item[1]["initial"]),
                    )
                    print(
                        f"residual: {worst_name} initial {worst['initial']:.3g} | "
                        f"final {worst['final']:.3g}"
                    )
                monitors = progress.get("monitors", {})
                if (
                    isinstance(monitors, dict)
                    and monitors.get("relative_mass_imbalance") is not None
                ):
                    monitor_line = (
                        "monitor: mass imbalance "
                        f"{float(monitors['relative_mass_imbalance']):.3g}"
                    )
                    if monitors.get("pressure_drop") is not None:
                        monitor_line += f" | pressure drop {float(monitors['pressure_drop']):.6g} Pa"
                    print(monitor_line)
            recovery = report["recovery"]
            if recovery["available"]:
                coordinate = recovery["coordinate"]
                print(
                    f"checkpoint: resumable from {coordinate['value']:g} "
                    f"{coordinate['unit']} | {recovery['source']}"
                )
            print(
                f"next: {report['next_action']['command']} | "
                f"{report['next_action']['reason']}"
            )
            postprocess = report["postprocess"]
            if postprocess["primary"] is not None:
                print(f"result: {postprocess['primary']}")
            if args.storage:
                storage = report["storage"]
                print(
                    f"managed: {storage['managed_display']} | reclaimable: "
                    f"{storage['reclaimable_display']}"
                )
        return 0 if report["state"] not in {"blocked", "failed"} else 3
    if args.command == "project":
        report = projects.open_project(args.project).snapshot(
            include_result=args.include_result,
            include_storage=args.storage,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            model = report["model"]
            result = report["result"]
            decision = (
                ""
                if result is None
                else f" | accepted {str(result['accepted']).lower()} | trust {result['trust_level']}"
            )
            print(f"{model['name']} | {str(report['state']).upper()}{decision}")
            output = report["output"]
            if output is None:
                print("output: not published")
            else:
                print(f"output: {output['directory']}")
                fields = output["fields"]
                if fields is not None and fields["xdmf"] is not None:
                    print(f"fields: {fields['xdmf']}")
                workspace = output["expert_workspace"]
                if report["project"]["provider"] == "openfoam":
                    print(
                        "OpenFOAM workspace: "
                        + (
                            str(workspace["path"])
                            if workspace["retained"]
                            else "cleaned"
                        )
                    )
            if report["storage"] is not None:
                storage = report["storage"]
                print(
                    f"storage: {storage['managed_display']} managed | "
                    f"{storage['reclaimable_display']} reclaimable"
                )
            action = report["next_action"]
            print(
                f"next: {action['operation']} | {action['command']} | "
                f"{action['reason']}"
            )
        return 0 if report["state"] not in {"blocked", "failed"} else 3
    if args.command == "params":
        project = projects.Project(args.project)
        selected = _project_parameters(args.param, args.param_file)
        report = project.parameter_set(selected)
        if args.output is not None:
            if args.output.exists():
                raise FileExistsError(
                    f"Parameter-set output already exists: {args.output}"
                )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(args.output, report)
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("Validated operating point")
            contract = project.parameter_contract(selected)
            for item in contract:
                if item["overrideable"] is not True:
                    continue
                metadata = item.get("metadata")
                unit = metadata.get("unit") if isinstance(metadata, dict) else None
                suffix = f" {unit}" if unit not in {None, "1"} else ""
                print(
                    f"  {item['name']}={json.dumps(item['current'], sort_keys=True)}"
                    f"{suffix}"
                )
            if args.output is not None:
                print(args.output)
        return 0
    if args.command == "result":
        report = projects.Project.discover(args.project).result_summary(
            run_id=args.run_id,
            quantities=args.quantity,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Result {report['run_id']} | {str(report['status']).upper()} | "
                f"accepted {str(report['accepted']).lower()} | "
                f"trust {report['trust_level']}"
            )
            grouped: dict[str, list[tuple[str, dict[str, object]]]] = {}
            for name, quantity in report["quantities"].items():
                group = _result_quantity_group(name, quantity)
                grouped.setdefault(group, []).append((name, quantity))
            for group in (
                "Flow results",
                "Thermal results",
                "Inputs",
                "Mesh quality",
                "Verification",
                "Runtime",
                "Other results",
            ):
                items = grouped.get(group, [])
                if not items:
                    continue
                print(f"{group}:")
                for name, quantity in items:
                    raw_unit = quantity["unit"]
                    unit = (
                        ""
                        if raw_unit is None
                        else " [-]"
                        if raw_unit == "1"
                        else f" {raw_unit}"
                    )
                    print(f"  {name}: {float(quantity['value']):.8g}{unit}")
            available = report["available"]
            print(
                f"data: {len(available['histories'])} histories | "
                f"{len(available['fields'])} fields | HDF5 not opened"
            )
            if report["failed_checks"]:
                print(
                    "failed checks: "
                    + ", ".join(check["name"] for check in report["failed_checks"])
                )
            print(f"verify artifacts: {report['artifact_integrity']['command']}")
        return 0 if report["accepted"] else 3
    if args.command == "storage":
        report = projects.Project(args.project).storage()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Managed data {report['managed_display']} | reclaimable "
                f"{report['reclaimable_display']}"
            )
            print(f"filesystem free: {report['filesystem']['free_display']}")
            for name, item in report["categories"].items():
                print(f"{name}: {item['display']} | {item['file_count']} files")
            if report["next_action"] is not None:
                print(f"next: {report['next_action']['command']}")
        return 0
    if args.command == "performance":
        report = projects.open_project(args.project).performance()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            history = report["history"]
            current = report["current"]
            calibration = current["calibration"]
            print(
                f"Runtime history {history['sample_count']}/{history['maximum_samples']} "
                f"samples | {history['status']}"
            )
            if calibration is None:
                print("current setup: no comparable completed run")
            else:
                print(
                    f"current setup: {calibration['sample_count']} comparable | "
                    f"median {calibration['median_display']} | "
                    f"range {calibration['minimum_display']}–"
                    f"{calibration['maximum_display']}"
                )
            print(f"next: {report['next_action']['command']}")
        return 0
    if args.command == "campaigns":
        project = projects.Project.discover(args.project)
        if args.export_csv is None:
            report = project.campaign_index(include_storage=args.storage)
        else:
            _, report = project.export_campaign_csv(
                args.export_csv,
                include_storage=args.storage,
            )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Campaigns {report['run_count']} | accepted "
                f"{report['accepted_count']} | failed {report['failed_count']}"
            )
            for row in report["runs"]:
                duration = (
                    "unknown time"
                    if row["duration_seconds"] is None
                    else f"{float(row['duration_seconds']):.3g}s"
                )
                print(
                    f"{row['run_id']} | {row['status']} | "
                    f"accepted {str(row['accepted']).lower()} | {duration}"
                )
            if report["exported_csv"] is not None:
                print(f"CSV: {report['exported_csv']}")
            if not report["include_storage"]:
                print("storage not scanned; add --storage when needed")
        return 0
    if args.command == "sweep":
        project = projects.Project.discover(args.project)
        points = _campaign_request(args.request)
        if args.plan_only:
            report = project.plan_campaign(
                points,
                provider=args.provider,
                container_image=args.container_image,
                summary_only=args.summary_only,
            )
        else:
            report = project.run_campaign(
                points,
                provider=args.provider,
                container_image=args.container_image,
                fail_fast=args.fail_fast,
                maximum_solver_runs=args.max_runs,
                summary_only=args.summary_only,
            )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        elif args.plan_only:
            print(
                f"Sweep plan {report['ready_count']}/{report['point_count']} ready | "
                f"would execute {report['would_execute_count']} | reusable "
                f"{report['reusable_count']}"
            )
            print(f"result profile: {report['result_profile']}")
            for point in report["points"]:
                print(
                    f"{point['name']} | ready {str(point['ready']).lower()} | "
                    f"reusable {str(point['reusable']).lower()}"
                )
        else:
            print(
                f"Sweep {report['processed_count']}/{report['requested_count']} | "
                f"executed {report['executed_count']} | reused "
                f"{report['reused_count']} | deduplicated "
                f"{report['deduplicated_count']} | accepted "
                f"{report['accepted_count']}"
            )
            print(f"result profile: {report['result_profile']}")
            for point in report["points"]:
                print(
                    f"{point['name']} | {point['execution']} | {point['outcome']}"
                )
                if point["diagnose_command"] is not None:
                    print(f"  diagnose: {point['diagnose_command']}")
            print(f"progress: {report['progress']}")
        if args.plan_only:
            return 0 if report["all_ready"] else 3
        return 0 if report["successful"] else 3
    if args.command == "promote":
        report = projects.Project.discover(args.project).promote_campaign_run(
            args.run_id,
            container_image=args.container_image,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Promotion {report['execution']} | source "
                f"{report['source']['run_id']} | target "
                f"{report['target']['run_id']} | accepted "
                f"{str(report['target']['accepted']).lower()}"
            )
            print(f"fields: {report['target']['directory']}")
        return 0 if report["successful"] else 3
    if args.command == "compact":
        report = projects.Project.discover(args.project).compact_campaign_run(
            args.run_id,
            apply=args.apply,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            action = "Reclaimed" if report["applied"] else "Would reclaim"
            print(
                f"{action} {report['candidate_display']} from {report['run_id']} "
                f"({report['candidate_file_count']} files)"
            )
            if not report["applied"]:
                print("preview only; add --apply to compact this accepted run")
        return 0
    if args.command == "clean":
        report = projects.Project(args.project).clean(
            apply=args.apply,
            include_retained=args.include_retained,
            include_cache=args.include_cache,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            action = "Reclaimed" if report["applied"] else "Would reclaim"
            amount = (
                report["reclaimed_display"]
                if report["applied"]
                else report["candidate_display"]
            )
            source = (
                "temporary solver workspaces and mesh cache"
                if report["include_cache"]
                else "temporary solver workspaces"
            )
            print(f"{action} {amount} from {source}")
            print(
                "Preserved output/ and campaigns/"
                + ("" if report["include_cache"] else "; mesh cache preserved")
            )
            if report["protected_active_run_ids"]:
                print(
                    "Protected active runs: "
                    + ", ".join(report["protected_active_run_ids"])
                )
            if report["protected_recovery_run_ids"]:
                print(
                    "Protected recovery checkpoints: "
                    + ", ".join(report["protected_recovery_run_ids"])
                )
            if report["protected_retained_run_ids"]:
                print(
                    "Protected explicitly retained workspaces: "
                    + ", ".join(report["protected_retained_run_ids"])
                )
            if not report["applied"] and report["candidate_bytes"]:
                suffix = " --include-cache" if report["include_cache"] else ""
                print(f"preview only; apply with: agentcfd clean . --apply{suffix}")
        return 0
    if args.command == "view":
        status = projects.Project(args.project).status()
        selected_recipe = None
        selected_layout = None
        if args.recipe is not None:
            selected_recipe = next(
                (
                    recipe
                    for recipe in status["postprocess"]["recipes"]
                    if recipe.get("name") == args.recipe
                ),
                None,
            )
            if selected_recipe is None:
                available = ", ".join(
                    recipe["name"] for recipe in status["postprocess"]["recipes"]
                )
                raise ProjectError(
                    f"Unknown post-processing recipe {args.recipe!r}. Available: "
                    + (available or "none; declare output views in case.py and rerun")
                    + "."
                )
        elif args.layout is not None:
            selected_layout = next(
                (
                    layout
                    for layout in status["postprocess"]["layouts"]
                    if layout.get("name") == args.layout
                ),
                None,
            )
            if selected_layout is None:
                available = ", ".join(
                    layout["name"] for layout in status["postprocess"]["layouts"]
                )
                raise ProjectError(
                    f"Unknown post-processing layout {args.layout!r}. Available: "
                    + (available or "none; declare output layouts in case.py and rerun")
                    + "."
                )
        selected_item = selected_recipe or selected_layout
        target = (
            selected_item["script"]
            if selected_item is not None
            else status["postprocess"]["primary"]
        )
        if target is None:
            raise ProjectError(
                "No completed result is available. Follow `agentcfd status .` first."
            )
        launched = False
        batch_completed = False
        produced = []
        viewer = None
        if args.launch:
            if str(target).endswith((".xdmf", ".py")):
                viewer = _paraview_executable()
                if viewer is None:
                    raise ProjectError(
                        "ParaView is not on PATH. Open the reported fields.xdmf file "
                        "manually or install the ParaView command-line launcher."
                    )
                command = (
                    [viewer, "--script", str(target)]
                    if str(target).endswith(".py")
                    else [viewer, str(target)]
                )
                subprocess.Popen(command)
                launched = True
            else:
                raise ProjectError(
                    "The latest result has no XDMF field bundle; inspect result.json instead."
                )
        if args.batch:
            if selected_item is None or not str(target).endswith(".py"):
                raise ProjectError(
                    "Batch post-processing requires `--recipe NAME` or `--layout NAME` "
                    "for a published post-processing script."
                )
            viewer = _paraview_batch_executable()
            if viewer is None:
                raise ProjectError(
                    "ParaView pvbatch is not on PATH. Install ParaView or use "
                    "`agentcfd view . --recipe NAME --launch` on a desktop."
                )
            command = [viewer, str(target)]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-1000:]
                raise ProjectError(
                    "ParaView batch recipe failed"
                    + (f": {detail}" if detail else ".")
                )
            batch_completed = True
            expected = [
                *selected_item.get("render_outputs_after_launch", []),
                *selected_item.get("data_outputs_after_launch", []),
            ]
            recipe_directory = Path(str(target)).parent
            produced = [
                str(recipe_directory / name)
                for name in expected
                if "%" not in name and (recipe_directory / name).is_file()
            ]
        report = {
            "schema": "agentcfd.project-view/0.1",
            "project_state": status["state"],
            "target": str(target),
            "kind": (
                "paraview-script"
                if str(target).endswith(".py")
                else "xdmf"
                if str(target).endswith(".xdmf")
                else "result-json"
            ),
            "summary": (
                selected_item
                if selected_item is not None
                else status["postprocess"]["field_summary"]
            ),
            "launched": launched,
            "batch_completed": batch_completed,
            "produced": produced,
            "viewer": viewer,
        }
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            prefix = (
                "Generated"
                if batch_completed
                else "Opened"
                if launched
                else "Latest post-processing target"
            )
            print(f"{prefix}: {target}")
            if batch_completed:
                print(
                    "produced: "
                    + (", ".join(produced) if produced else "recipe completed")
                )
            if report["kind"] == "paraview-script":
                summary = report["summary"]
                if summary["type"] == "render-layout":
                    print(
                        f"layout: {summary['name']} | {summary['columns']}x"
                        f"{summary['rows']} | views " + ", ".join(summary["views"])
                    )
                else:
                    print(
                        f"recipe: {summary['name']} | {summary['type']} | "
                        f"field {summary['field']}"
                    )
                print("shares fields/fields.h5; no volume data was duplicated")
            elif report["summary"] is not None:
                summary = report["summary"]
                axis = summary["axis"]
                print(
                    f"{summary['frame_count']} frames | {axis['name']} "
                    f"{axis['first']}..{axis['last']} {axis['unit']} | "
                    f"{summary['portable_display']}"
                )
                print(
                    "fields: "
                    + ", ".join(
                        f"{field['name']} [{field['association']}]"
                        for field in summary["fields"]
                    )
                )
                recipes = status["postprocess"]["recipes"]
                if recipes:
                    print(
                        "recipes: "
                        + ", ".join(recipe["name"] for recipe in recipes)
                    )
                layouts = status["postprocess"]["layouts"]
                if layouts:
                    print(
                        "layouts: "
                        + ", ".join(layout["name"] for layout in layouts)
                    )
            if not launched and report["kind"] in {"xdmf", "paraview-script"}:
                selection_option = (
                    f" --recipe {args.recipe}" if args.recipe is not None else ""
                )
                if args.layout is not None:
                    selection_option = f" --layout {args.layout}"
                print(f"launch with: agentcfd view .{selection_option} --launch")
        return 0
    if args.command == "capabilities":
        report = capabilities.as_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for item in capabilities.all():
                print(f"{item.name}: {item.maturity}")
        return 0
    if args.command == "benchmarks":
        report = benchmarks.as_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for case in benchmarks.all():
                print(f"{case.id}: {case.status} | next: {case.next_gate}")
        return 0
    if args.command == "contracts":
        report = contracts.catalog()
        compatibility = None
        if args.check_agentcae:
            compatibility = contracts.agentcae_compatibility()
            report["agentcae_compatibility"] = compatibility
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for contract in report["contracts"]:
                print(f"{contract['name']}: {contract['id']}")
            if compatibility is not None:
                print(f"AgentCAE compatibility: {compatibility['status']}")
                next_action = compatibility.get("next_action")
                if isinstance(next_action, dict):
                    print(f"next: {next_action['command']}")
        return 0 if compatibility is None or compatibility["compatible"] is True else 3
    if args.command == "licenses":
        report = licensing.as_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for component in licensing.all():
                required = "required" if component.mandatory_runtime else "optional"
                print(
                    f"{component.name}: {component.license_expression} | "
                    f"{component.relationship} | {required}"
                )
        return 0
    if args.command == "export" and args.export_format == "openfoam":
        bundle = data_exchange.export_openfoam_case(
            args.case_directory,
            args.output_directory,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
            convert=not args.skip_conversion,
            density=args.density,
            profile=args.profile,
            fields=args.fields,
            formats=("xdmf", "npz") if args.with_npz else ("xdmf",),
            compression=args.compression,
            maximum_bytes=args.storage_budget,
        )
        report = bundle.to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            formats = "XDMF/H5 + safe NPZ" if bundle.npz is not None else "XDMF/H5"
            print(f"Exported {bundle.frame_count} frames | {formats}")
            print(bundle.xdmf)
        return 0
    if args.command == "export" and args.export_format == "field-sample":
        output = data_exchange.export_agentfem_field_sample(
            args.bundle_directory,
            args.output,
            field=args.field,
            association=args.association,
            frame=args.frame,
            cell_block=args.cell_block,
        )
        report = {
            "schema": "agentcfd.field-sample-export/0.1",
            "output": str(output),
            "field": args.field,
            "association": args.association,
            "frame": args.frame,
        }
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Exported tensor-ready field sample | {args.field}:{args.association}"
            )
            print(output)
        return 0
    if args.command == "calculate" and args.calculation == "pipe-loss":
        report = engineering.pipe_pressure_loss(
            density=args.density,
            dynamic_viscosity=args.viscosity,
            mean_velocity=args.velocity,
            length=args.length,
            hydraulic_diameter=args.diameter,
            roughness=args.roughness,
            loss_coefficient=args.loss_coefficient,
        ).to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"{report['regime']} pipe | Re {report['reynolds_number']:.6g} | "
                f"pressure loss {report['total_pressure_loss']:.6g} Pa"
            )
        return 0
    if args.command == "calculate" and args.calculation == "pipe-flow":
        report = engineering.circular_pipe_operating_point(
            pressure_loss=args.pressure_loss,
            density=args.density,
            dynamic_viscosity=args.viscosity,
            length=args.length,
            diameter=args.diameter,
            regime=args.regime,
            roughness=args.roughness,
            loss_coefficient=args.loss_coefficient,
        ).to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"{report['regime']} pipe | velocity {report['mean_velocity']:.6g} m/s | "
                f"flow {report['volume_flow_rate']:.6g} m^3/s"
            )
        return 0
    if args.command == "calculate" and args.calculation == "compressibility":
        report = engineering.screen_incompressible_flow(
            velocity=args.velocity,
            speed_of_sound=args.speed_of_sound,
            maximum_incompressible_mach=args.maximum_incompressible_mach,
        ).to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            decision = (
                "appropriate"
                if report["incompressible_model_appropriate"]
                else "not appropriate"
            )
            print(
                f"Mach {report['mach_number']:.6g} | incompressible model "
                f"{decision} under threshold "
                f"{report['maximum_incompressible_mach']:.6g}"
            )
        return 0
    if args.command == "calculate" and args.calculation == "wall-resolution":
        report = engineering.turbulent_pipe_wall_resolution(
            density=args.density,
            dynamic_viscosity=args.viscosity,
            mean_velocity=args.velocity,
            hydraulic_diameter=args.diameter,
            target_y_plus=args.target_y_plus,
            roughness=args.roughness,
        ).to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Re {report['reynolds_number']:.6g} | target y+ "
                f"{report['target_y_plus']:.6g} | nominal first-cell thickness "
                f"{report['nominal_first_cell_thickness']:.6g} m"
            )
        return 0
    if args.command == "calculate" and args.calculation == "thermal-flow":
        report = engineering.screen_thermal_internal_flow(
            density=args.density,
            dynamic_viscosity=args.viscosity,
            specific_heat=args.specific_heat,
            thermal_conductivity=args.thermal_conductivity,
            mean_velocity=args.velocity,
            hydraulic_diameter=args.diameter,
            flow_area=args.flow_area,
            inlet_bulk_temperature=args.inlet_temperature,
            heat_rate_into_fluid=args.heat_rate,
            maximum_temperature_change_fraction=(
                args.maximum_temperature_change_fraction
            ),
        ).to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            decision = (
                "within" if report["within_declared_temperature_change_limit"] else "above"
            )
            print(
                f"Re {report['reynolds_number']:.6g} | Pr "
                f"{report['prandtl_number']:.6g} | Pe {report['peclet_number']:.6g}"
            )
            print(
                f"estimated outlet {report['estimated_outlet_bulk_temperature']:.6g} K | "
                f"temperature change is {decision} declared limit"
            )
        return 0
    if args.command == "properties" and args.property_operation == "state":
        state = (
            properties.CoolPropPropertyProvider()
            .at_pressure_temperature(
                args.fluid,
                pressure=args.pressure,
                temperature=args.temperature,
            )
            .to_dict()
        )
        if args.as_json:
            print(json.dumps(state, indent=2, sort_keys=True))
        else:
            print(
                f"{state['fluid']} | {state['phase']} | density "
                f"{state['density']:.6g} kg/m^3 | provider "
                f"{state['provider']} {state['provider_version']}"
            )
        return 0
    if args.command == "demo" and args.demo == "pipe":
        result = _pipe_demo(args.output)
        pressure_drop = result["quantities"]["flow.pressure_drop"]
        print(
            f"Accepted laminar pipe result | pressure drop {pressure_drop['value']:.6g} {pressure_drop['unit']}"
        )
        print(args.output)
        return 0
    if args.command == "prepare" and args.provider == "openfoam-pipe":
        manifest = _prepare_openfoam_pipe(
            args.case_directory,
            fully_developed=args.fully_developed,
            cross_section_cells=args.cross_section_cells,
            axial_cells=args.axial_cells,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
        )
        if args.as_json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print("Prepared experimental OpenFOAM circular-pipe case")
            print(args.case_directory)
            print(f"case sha256: {manifest['case_sha256']}")
        return 0
    if args.command == "prepare" and args.provider == "openfoam-turbulent-pipe":
        step = _turbulent_pipe_step(
            velocity=args.velocity,
            turbulence_intensity=args.turbulence_intensity,
            turbulence_length_scale=args.turbulence_length_scale,
        )
        manifest = (
            _turbulent_openfoam_provider(
                args.case_directory,
                cross_section_cells=args.cross_section_cells,
                axial_cells=args.axial_cells,
                nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
                precursor_case=args.precursor_case,
            )
            .prepare(step)
            .to_dict()
        )
        if args.as_json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print("Prepared experimental OpenFOAM k-omega SST pipe case")
            print(args.case_directory)
            print(f"case sha256: {manifest['case_sha256']}")
        return 0
    if args.command == "prepare" and args.provider == "openfoam-turbulent-precursor":
        step = _turbulent_pipe_step(
            turbulence_model=args.turbulence_model,
            velocity=args.velocity,
            turbulence_intensity=args.turbulence_intensity,
            turbulence_length_scale=args.turbulence_length_scale,
        )
        manifest = (
            _turbulent_precursor_provider(
                args.case_directory,
                cross_section_cells=args.cross_section_cells,
                maximum_iterations=args.maximum_iterations,
                nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
                nut_wall_function=(
                    args.nut_wall_function
                    or (
                        "nutkWallFunction"
                        if args.turbulence_model == "k-epsilon"
                        else "nutUBlendedWallFunction"
                    )
                ),
            )
            .prepare(step)
            .to_dict()
        )
        if args.as_json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print(f"Prepared periodic OpenFOAM {args.turbulence_model} inlet precursor")
            print(args.case_directory)
            print(f"case sha256: {manifest['case_sha256']}")
        return 0
    if args.command == "prepare" and args.provider == "openfoam-turbulent-wall-study":
        plan = _prepare_openfoam_turbulent_wall_study(
            args.directory,
            cross_section_cells=tuple(args.cross_section_cells),
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            nut_wall_function=args.nut_wall_function,
            maximum_iterations=tuple(args.maximum_iterations),
        )
        if args.as_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print("Prepared fixed-wall-cell OpenFOAM turbulent study")
            print(args.directory / "agentcfd-turbulent-wall-study.json")
        return 0
    if (
        args.command == "prepare"
        and args.provider == "openfoam-turbulent-wall-function-study"
    ):
        plan = _prepare_openfoam_turbulent_wall_function_study(
            args.directory,
            cross_section_cells=args.cross_section_cells,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            maximum_iterations=args.maximum_iterations,
        )
        if args.as_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print("Prepared identical-mesh OpenFOAM wall-function study")
            print(args.directory / "agentcfd-turbulent-wall-function-study.json")
        return 0
    if args.command == "prepare" and args.provider == "openfoam-turbulent-model-study":
        plan = _prepare_openfoam_turbulent_model_study(
            args.directory,
            velocity=args.velocity,
            turbulence_intensity=args.turbulence_intensity,
            turbulence_length_scale=args.turbulence_length_scale,
            cross_section_cells=args.cross_section_cells,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            target_y_plus=args.target_y_plus,
            maximum_iterations=args.maximum_iterations,
        )
        if args.as_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print("Prepared identical-mesh OpenFOAM turbulence-model study")
            print(args.directory / "agentcfd-turbulent-model-study.json")
            screen = plan["wall_resolution_screen"]
            assert isinstance(screen, dict)
            print(
                f"wall preflight: predicted y+ "
                f"{float(screen['predicted_nominal_y_plus']):.6g} | "
                "runtime verification required"
            )
            if screen["predicted_high_re_wall_function_applicable"] is not True:
                print(
                    "warning: predicted y+ is outside the high-Re wall-function range"
                )
        return 0
    if args.command == "prepare" and args.provider == "openfoam-turbulent-model-sweep":
        plan = _prepare_openfoam_turbulent_model_sweep(
            args.directory,
            velocities=tuple(args.velocities),
            target_y_plus=args.target_y_plus,
            turbulence_intensity=args.turbulence_intensity,
            turbulence_length_scale=args.turbulence_length_scale,
            cross_section_cells=args.cross_section_cells,
            maximum_iterations=args.maximum_iterations,
        )
        if args.as_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(
                f"Prepared OpenFOAM turbulence-model sweep | "
                f"{len(plan['points'])} points | target y+ {plan['target_y_plus']}"
            )
            print(args.directory / "agentcfd-turbulent-model-sweep.json")
        return 0
    if args.command == "prepare" and args.provider == "openfoam-pipe-grid":
        plan = _prepare_openfoam_pipe_grid(
            args.directory,
            cross_section_cells=tuple(args.cross_section_cells),
            base_axial_cells=args.base_axial_cells,
        )
        if args.as_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print("Prepared same-model three-grid OpenFOAM pipe study")
            print(args.directory / "agentcfd-grid-study.json")
        return 0
    if args.command == "run" and args.provider == "openfoam-pipe":
        result, target = _run_openfoam_pipe(
            args.case_directory,
            result_path=args.result,
            fully_developed=args.fully_developed,
            container_image=args.container_image,
            cross_section_cells=args.cross_section_cells,
            axial_cells=args.axial_cells,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            prepared=args.prepared,
            timeout_seconds=args.timeout_seconds,
        )
        if args.as_json:
            print(json.dumps(_result_cli_payload(result), indent=2, sort_keys=True))
        else:
            print(
                f"OpenFOAM run {result.status} | trust {result.trust_level} | "
                f"accepted {str(result.accepted).lower()}"
            )
            failed = [check.name for check in result.checks if not check.passed]
            if failed:
                print("blocked by: " + ", ".join(failed))
            print(target)
        if result.accepted:
            return 0
        return 1 if result.status != "completed" else 3
    if args.command == "run" and args.provider == "project":
        completed = projects.Project(args.project).run(
            provider=args.project_provider,
            container_image=args.container_image,
            campaign=args.campaign,
            keep_workspace=args.keep_workspace,
            parameters=_project_parameters(args.param, args.param_file),
            portable_fields=False if args.summary_only else None,
        )
        report = completed.to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Project run {completed.result.status} | trust "
                f"{completed.result.trust_level} | accepted "
                f"{str(completed.result.accepted).lower()}"
            )
            print(completed.directory)
        if completed.result.accepted:
            return 0
        return 1 if completed.result.status != "completed" else 3
    if args.command == "run" and args.provider == "openfoam-turbulent-pipe":
        step = _turbulent_pipe_step(
            velocity=args.velocity,
            turbulence_intensity=args.turbulence_intensity,
            turbulence_length_scale=args.turbulence_length_scale,
        )
        provider = _turbulent_openfoam_provider(
            args.case_directory,
            cross_section_cells=args.cross_section_cells,
            axial_cells=args.axial_cells,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            precursor_case=args.precursor_case,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
        )
        result = provider.run_prepared(step) if args.prepared else provider.run(step)
        target = args.result or args.case_directory / "agentcfd-result.json"
        result.write(target)
        if args.as_json:
            print(json.dumps(_result_cli_payload(result), indent=2, sort_keys=True))
        else:
            friction = result.quantities.get("flow.darcy_friction_factor")
            detail = ""
            if friction is not None:
                detail = f" | Darcy f {friction.value:.6g}"
            print(
                f"OpenFOAM turbulent pipe {result.status} | trust "
                f"{result.trust_level} | accepted {str(result.accepted).lower()}{detail}"
            )
            failed = [check.name for check in result.checks if not check.passed]
            if failed:
                print("blocked by: " + ", ".join(failed))
            print(target)
        if result.accepted:
            return 0
        return 1 if result.status != "completed" else 3
    if args.command == "run" and args.provider == "openfoam-turbulent-precursor":
        step = _turbulent_pipe_step(
            turbulence_model=args.turbulence_model,
            velocity=args.velocity,
            turbulence_intensity=args.turbulence_intensity,
            turbulence_length_scale=args.turbulence_length_scale,
        )
        provider = _turbulent_precursor_provider(
            args.case_directory,
            cross_section_cells=args.cross_section_cells,
            maximum_iterations=args.maximum_iterations,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            nut_wall_function=(
                args.nut_wall_function
                or (
                    "nutkWallFunction"
                    if args.turbulence_model == "k-epsilon"
                    else "nutUBlendedWallFunction"
                )
            ),
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
        )
        result = provider.run_prepared(step) if args.prepared else provider.run(step)
        target = args.result or args.case_directory / "agentcfd-result.json"
        result.write(target)
        if args.as_json:
            print(json.dumps(_result_cli_payload(result), indent=2, sort_keys=True))
        else:
            friction = result.quantities.get("flow.darcy_friction_factor")
            detail = f" | Darcy f {friction.value:.6g}" if friction is not None else ""
            print(
                f"OpenFOAM turbulent precursor {result.status} | trust "
                f"{result.trust_level} | accepted {str(result.accepted).lower()}{detail}"
            )
            failed = [check.name for check in result.checks if not check.passed]
            if failed:
                print("blocked by: " + ", ".join(failed))
            print(target)
        if result.accepted:
            return 0
        return 1 if result.status != "completed" else 3
    if args.command == "run" and args.provider == "openfoam-turbulent-wall-study":
        payload, target = _run_openfoam_turbulent_wall_study(
            args.directory,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
        )
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "Completed turbulent wall study | wall strategy "
                f"{str(payload['acceptance']['wall_strategy_accepted']).lower()} | "
                f"GCI applicable {str(payload['gci']['applicable']).lower()}"
            )
            print(target)
        return 0 if payload["acceptance"]["wall_strategy_accepted"] else 3
    if (
        args.command == "run"
        and args.provider == "openfoam-turbulent-wall-function-study"
    ):
        payload, target = _run_openfoam_turbulent_wall_function_study(
            args.directory,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
        )
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            candidate = payload["recommendation"]["candidate"]
            accepted = payload["acceptance"]["screening_accepted"]
            print(
                f"Completed turbulent wall-function study | candidate {candidate} | "
                f"screening accepted {str(accepted).lower()}"
            )
            print(target)
        return 0 if payload["acceptance"]["screening_accepted"] else 3
    if args.command == "run" and args.provider == "openfoam-turbulent-model-study":
        payload, target = _run_openfoam_turbulent_model_study(
            args.directory,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
        )
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            recommendation = payload["recommendation"]
            accepted = payload["acceptance"]["screening_accepted"]
            print(
                "Completed turbulent model study | candidate "
                f"{recommendation['candidate_turbulence_model']} + "
                f"{recommendation['candidate_nut_wall_function']} | "
                f"screening accepted {str(accepted).lower()}"
            )
            print(target)
        return 0 if payload["acceptance"]["screening_accepted"] else 3
    if args.command == "run" and args.provider == "openfoam-turbulent-model-sweep":
        payload, target = _run_openfoam_turbulent_model_sweep(
            args.directory,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
        )
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            accepted = payload["acceptance"]["range_candidate_accepted"]
            print(
                f"Completed turbulence-model sweep | range accepted "
                f"{str(accepted).lower()} | default promotion false"
            )
            print(target)
        return 0 if payload["acceptance"]["range_candidate_accepted"] else 3
    if args.command == "run" and args.provider == "openfoam-pipe-grid":
        payload, target = _run_openfoam_pipe_grid(
            args.directory,
            container_image=args.container_image,
            timeout_seconds=args.timeout_seconds,
        )
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                f"Completed three-grid study | observed order "
                f"{payload['observed_order']:.6g}"
            )
            print(target)
        return 0 if payload["acceptance"]["accepted"] else 3
    if args.command == "verify" and args.verification == "grid-convergence":
        payload = _grid_convergence_payload(args.results, quantity=args.quantity)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            relative = payload["fine_grid_relative_gci"]
            relative_text = "undefined" if relative is None else f"{relative:.6g}"
            print(
                f"GCI {args.quantity} | observed order {payload['observed_order']:.6g} | "
                f"fine relative GCI {relative_text}"
            )
        return 0 if payload["acceptance"]["accepted"] else 3
    if args.command == "verify" and args.verification == "time-step-sensitivity":
        payload = _time_step_sensitivity_payload(
            args.results,
            quantity=args.quantity,
            maximum_relative_change=args.maximum_relative_change,
        )
        if args.output is not None:
            _write_json_atomic(args.output, payload)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            relative = payload["relative_change"]
            relative_text = "undefined" if relative is None else f"{relative:.6g}"
            print(
                f"Time-step sensitivity {args.quantity} | relative change "
                f"{relative_text} | accepted {str(payload['accepted']).lower()}"
            )
            if args.output is not None:
                print(args.output)
        return 0 if payload["accepted"] else 3
    if args.command == "verify" and args.verification == "turbulent-wall-study":
        payload = _turbulent_wall_study_payload(args.results)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(args.output)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            metrics = payload["metrics"]
            acceptance = payload["acceptance"]
            print(
                "Turbulent wall study | wall strategy "
                f"{str(acceptance['wall_strategy_accepted']).lower()} | "
                f"fine change {metrics['fine_pair_pressure_gradient_relative_change']:.6g} | "
                f"GCI applicable {str(payload['gci']['applicable']).lower()}"
            )
            if args.output is not None:
                print(args.output)
        return 0 if payload["acceptance"]["wall_strategy_accepted"] else 3
    if (
        args.command == "verify"
        and args.verification == "turbulent-precursor-grid-study"
    ):
        payload = _turbulent_precursor_grid_study_payload(args.results)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(args.output)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "Turbulent precursor grid study | "
                f"monotonic {str(payload['metrics']['monotonic']).lower()} | "
                f"GCI applicable {str(payload['gci']['applicable']).lower()} | "
                "uncertainty promotion "
                f"{str(payload['acceptance']['uncertainty_promotion_accepted']).lower()}"
            )
            if args.output is not None:
                print(args.output)
        return 0 if payload["acceptance"]["uncertainty_promotion_accepted"] else 3
    if (
        args.command == "verify"
        and args.verification == "turbulent-wall-function-study"
    ):
        payload = _turbulent_wall_function_study_payload(args.results)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(args.output)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            candidate = payload["recommendation"]["candidate"]
            accepted = payload["acceptance"]["screening_accepted"]
            print(
                f"Turbulent wall-function study | candidate {candidate} | "
                f"screening accepted {str(accepted).lower()} | default promotion false"
            )
            if args.output is not None:
                print(args.output)
        return 0 if payload["acceptance"]["screening_accepted"] else 3
    if args.command == "verify" and args.verification == "turbulent-model-study":
        payload = _turbulent_model_study_payload(args.results)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(args.output)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            recommendation = payload["recommendation"]
            accepted = payload["acceptance"]["screening_accepted"]
            print(
                "Turbulent model study | candidate "
                f"{recommendation['candidate_turbulence_model']} + "
                f"{recommendation['candidate_nut_wall_function']} | "
                f"screening accepted {str(accepted).lower()} | "
                "default promotion false"
            )
            if args.output is not None:
                print(args.output)
        return 0 if payload["acceptance"]["screening_accepted"] else 3
    if args.command == "verify" and args.verification == "turbulent-model-sweep":
        payload = _turbulent_model_sweep_payload(args.studies)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(args.output)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            recommendation = payload["recommendation"]
            acceptance = payload["acceptance"]
            reynolds_range = payload["reynolds_range"]
            print(
                "Turbulent model sweep | candidate "
                f"{recommendation['candidate_turbulence_model']} + "
                f"{recommendation['candidate_nut_wall_function']} | "
                f"Re {reynolds_range['minimum']:.6g}.."
                f"{reynolds_range['maximum']:.6g} | range accepted "
                f"{str(acceptance['range_candidate_accepted']).lower()} | "
                "default promotion false"
            )
            if args.output is not None:
                print(args.output)
        return 0 if payload["acceptance"]["range_candidate_accepted"] else 3
    if args.command == "verify" and args.verification == "result":
        record = read_result_record(args.result)
        report = {
            "schema": "agentcfd.result-verification/0.1",
            "path": str(args.result),
            "accepted": record["accepted"],
            "trust_level": record["trust_level"],
            "artifact_count": len(record["artifact_records"]),
            "verified": True,
        }
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Verified result | trust {report['trust_level']} | "
                f"artifacts {report['artifact_count']}"
            )
        return 0
    if args.command == "verify" and args.verification == "project":
        report = projects.open_project(args.project).verify(run_id=args.run_id)
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Project integrity verified {str(report['verified']).lower()} | "
                f"accepted {str(report['accepted']).lower()} | "
                f"trust {report['trust_level']}"
            )
            for check in report["checks"]:
                state = "PASS" if check["passed"] else "FAIL"
                print(f"{state} {check['code']}: {check['message']}")
            print(f"next: {report['next_action']['command']}")
        return 0 if report["verified"] else 3
    if args.command == "verify" and args.verification == "field-bundle":
        report = data_exchange.verify_field_bundle(args.directory)
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Verified portable field bundle | {report['frame_count']} frames | "
                f"{report['point_count']} points"
            )
        return 0
    if args.command == "verify" and args.verification == "validation-point":
        assessment = assess_validation_point(
            args.simulation,
            args.reference,
            numerical_standard_uncertainty=args.numerical_uncertainty,
            input_standard_uncertainty=args.input_uncertainty,
            experimental_standard_uncertainty=args.experimental_uncertainty,
            coverage_factor=args.coverage_factor,
        )
        payload = {
            "schema": "agentcfd.validation-point/0.1",
            **assessment.to_dict(),
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                f"Validation point accepted {str(assessment.accepted).lower()} | "
                f"error {assessment.absolute_error:.6g} | expanded uncertainty "
                f"{assessment.expanded_validation_uncertainty:.6g}"
            )
        return 0 if assessment.accepted else 3
    return 2


def entrypoint(argv: list[str] | None = None) -> int:
    """Run the console interface with concise expected-failure reporting."""

    try:
        return main(argv)
    except (AgentCFDError, FileExistsError, FileNotFoundError, ValueError) as error:
        selected = sys.argv[1:] if argv is None else argv
        if "--json" in selected:
            print(json.dumps(_error_cli_payload(error), indent=2, sort_keys=True))
        else:
            print(f"agentcfd: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(entrypoint())
