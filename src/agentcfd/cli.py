"""Small, stable CLI for people, agents, CI, and future GUIs."""

from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import benchmarks, boundaries, capabilities, contracts, data_exchange, engineering, fluids, geometry, licensing, outputs, procedures, projects, properties, studies
from ._version import __version__
from .errors import AgentCFDError
from .jsonio import strict_json_object
from .model import Model
from .provenance import content_fingerprint, file_sha256
from .providers import (
    OpenFOAMMeshControls,
    OpenFOAMProvider,
    OpenFOAMTurbulentPrecursorProvider,
    prepare_pipe_grid_study,
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
)


def _doctor() -> dict[str, object]:
    openfoam = OpenFOAMProvider().descriptor()
    coolprop = properties.CoolPropPropertyProvider().descriptor()
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
        },
        "providers": {
            "reference-pipe": True,
            "openfoam-runtime": openfoam.available,
            "coolprop-properties": coolprop.available,
        },
    }


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
    result = _pipe_model().step(procedure=procedures.steady(), output=outputs.standard()).run()
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
    return OpenFOAMProvider(case_directory=case_directory, mesh=mesh).prepare(step).to_dict()


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
    fraction = 0.0625 if nominal_wall_cell_fraction is None else nominal_wall_cell_fraction
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
        raise ValueError("Turbulent model sweep velocities must be finite and positive.")
    if len(set(selected)) != len(selected):
        raise ValueError("Turbulent model sweep velocities must be distinct.")
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"OpenFOAM model-sweep directory is not empty: {directory}")
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
        raise ValueError("OpenFOAM turbulent model sweep requires at least three points.")
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
            raise ValueError("OpenFOAM model-sweep point escapes its directory.") from error
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


def _turbulent_wall_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_wall_study(records)
    return {
        **assessment,
        "sources": [
            {"path": str(path), "sha256": file_sha256(path)} for path in paths
        ],
    }


def _turbulent_precursor_grid_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_precursor_grid_study(records)
    return {
        **assessment,
        "sources": [
            {"path": str(path), "sha256": file_sha256(path)} for path in paths
        ],
    }


def _turbulent_wall_function_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_wall_function_study(records)
    return {
        **assessment,
        "sources": [
            {"path": str(path), "sha256": file_sha256(path)} for path in paths
        ],
    }


def _turbulent_model_study_payload(paths: list[Path]) -> dict[str, object]:
    records = [read_result_record(path) for path in paths]
    assessment = assess_turbulent_model_study(records)
    return {
        **assessment,
        "sources": [
            {"path": str(path), "sha256": file_sha256(path)} for path in paths
        ],
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
        "sources": [
            {"path": str(path), "sha256": file_sha256(path)} for path in paths
        ],
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
        raise ValueError("OpenFOAM grid-study plan belongs to a different benchmark model.")
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
            raise ValueError("OpenFOAM grid-study case escapes the study directory.") from error
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
        raise ValueError("OpenFOAM turbulent wall-study inputs changed after preparation.")
    fraction = plan.get("nominal_wall_cell_fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ValueError("OpenFOAM turbulent wall-study has an invalid wall-cell fraction.")
    nut_wall_function = plan.get("nut_wall_function")
    if not isinstance(nut_wall_function, str):
        raise ValueError("OpenFOAM turbulent wall-study has no momentum wall function.")
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError("OpenFOAM turbulent wall-study must contain exactly three cases.")

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
            raise ValueError("OpenFOAM turbulent wall-study iteration limit is invalid.")
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
        raise ValueError("OpenFOAM turbulent wall-function study uses a different model.")
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
        raise ValueError("OpenFOAM wall-function study must contain exactly three cases.")

    root = directory.resolve()
    result_paths: list[Path] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("OpenFOAM wall-function case record is invalid.")
        case_directory = (directory / str(case.get("directory", ""))).resolve()
        try:
            case_directory.relative_to(root)
        except ValueError as error:
            raise ValueError("OpenFOAM wall-function case escapes its study directory.") from error
        manifest = strict_json_object(
            (case_directory / "agentcfd-case.json").read_text(encoding="utf-8"),
            label=f"OpenFOAM case manifest {case_directory}",
        )
        if manifest.get("case_sha256") != case.get("case_sha256"):
            raise ValueError("OpenFOAM wall-function case identity changed.")
        wall_function = case.get("nut_wall_function")
        if not isinstance(wall_function, str):
            raise ValueError("OpenFOAM wall-function case has no implementation identity.")
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
    planned_inlets = [
        value
        for value in planned_boundaries.values()
        if isinstance(value, dict)
        and value.get("type") == "turbulent-mean-velocity-inlet"
    ] if isinstance(planned_boundaries, dict) else []
    if len(planned_inlets) != 1:
        raise ValueError("OpenFOAM model study has no unique turbulent inlet input.")
    planned_inlet = planned_inlets[0]
    common = {
        "velocity": planned_inlet.get("velocity"),
        "turbulence_intensity": planned_inlet.get("turbulence_intensity"),
        "turbulence_length_scale": planned_inlet.get("turbulence_length_scale"),
    }
    steps = {
        "k-omega-sst": _turbulent_pipe_step(
            turbulence_model="k-omega-sst", **common
        ),
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
        raise ValueError("OpenFOAM turbulent model-study has no wall-resolution screen.")
    expected_screen = turbulent_pipe_wall_mesh_screen(
        steps["k-omega-sst"],
        nominal_wall_cell_fraction=float(fraction),
        target_y_plus=planned_screen.get("target_y_plus"),
    )
    if plan.get("wall_resolution_screen") != expected_screen:
        raise ValueError("OpenFOAM turbulent model-study wall-resolution screen changed.")
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
            raise ValueError("OpenFOAM model-study case escapes its directory.") from error
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
    parser = argparse.ArgumentParser(prog="agentcfd", description="AI-native CFD for humans and agents.")
    parser.add_argument("--version", action="version", version=f"AgentCFD {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect the installed runtime.")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    init = subparsers.add_parser(
        "init",
        help="Create a readable AgentCFD engineering project.",
    )
    init.add_argument("directory", nargs="?", type=Path, default=Path("."))
    init.add_argument(
        "--template",
        choices=("industrial-pipe",),
        default="industrial-pipe",
    )
    init.add_argument(
        "--provider",
        choices=("reference", "openfoam"),
        default="reference",
    )
    init.add_argument("--json", action="store_true", dest="as_json")

    check = subparsers.add_parser(
        "check",
        help="Validate project intent and provider compatibility without running.",
    )
    check.add_argument("project", nargs="?", type=Path, default=Path("."))
    check.add_argument("--provider", choices=("reference", "openfoam"))
    check.add_argument("--container-image")
    check.add_argument("--json", action="store_true", dest="as_json")

    plan = subparsers.add_parser(
        "plan",
        help="Resolve a deterministic, inspectable solution plan.",
    )
    plan.add_argument("project", nargs="?", type=Path, default=Path("."))
    plan.add_argument("--provider", choices=("reference", "openfoam"))
    plan.add_argument("--container-image")
    plan.add_argument("--output", type=Path)
    plan.add_argument("--json", action="store_true", dest="as_json")

    inspect = subparsers.add_parser(
        "inspect",
        help="Inspect project readiness and its latest structured run.",
    )
    inspect.add_argument("project", nargs="?", type=Path, default=Path("."))
    inspect.add_argument("--json", action="store_true", dest="as_json")

    catalog = subparsers.add_parser("capabilities", help="Show truthful capability boundaries.")
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
        help="Export every OpenFOAM field frame as XDMF/H5 and safe NPZ.",
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
    pipe = demo_subparsers.add_parser("pipe", help="Run the laminar circular-pipe reference workflow.")
    pipe.add_argument("--output", type=Path, default=Path("agentcfd-pipe-result.json"))

    prepare = subparsers.add_parser("prepare", help="Generate a provider case without executing it.")
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
    turbulent_prepare.add_argument("--turbulence-length-scale", type=float, default=0.007)
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
    precursor_prepare.add_argument("--turbulence-length-scale", type=float, default=0.007)
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
    model_study_prepare.add_argument("--turbulence-length-scale", type=float, default=0.007)
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

    run = subparsers.add_parser("run", help="Prepare, execute, and recover a provider result.")
    run_subparsers = run.add_subparsers(dest="provider", required=True)
    project_run = run_subparsers.add_parser(
        "project",
        help="Execute the declared project and publish one structured run directory.",
    )
    project_run.add_argument("project", nargs="?", type=Path, default=Path("."))
    project_run.add_argument(
        "--provider",
        choices=("reference", "openfoam"),
        dest="project_provider",
    )
    project_run.add_argument("--container-image")
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

    verify = subparsers.add_parser("verify", help="Create numerical verification evidence.")
    verify_subparsers = verify.add_subparsers(dest="verification", required=True)
    grid = verify_subparsers.add_parser(
        "grid-convergence",
        help="Compute a three-result Richardson extrapolation and GCI.",
    )
    grid.add_argument("results", nargs=3, type=Path)
    grid.add_argument("--quantity", required=True)
    grid.add_argument("--json", action="store_true", dest="as_json")
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
    bundle_check = verify_subparsers.add_parser(
        "field-bundle",
        help="Verify XDMF/H5/NPZ hashes and cross-format frame identity.",
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
    validation_point.add_argument("--experimental-uncertainty", type=float, required=True)
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
    if selected_argv and selected_argv[0] == "run" and (
        len(selected_argv) == 1
        or (
            selected_argv[1] not in legacy_run_targets
            and selected_argv[1] not in {"-h", "--help"}
        )
    ):
        selected_argv.insert(1, "project")
    args = build_parser().parse_args(selected_argv)
    if args.command == "doctor":
        report = _doctor()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"AgentCFD {report['agentcfd']} | Python {report['python']} | healthy")
            print(f"Reference provider: ready | OpenFOAM runtime: {'found' if report['providers']['openfoam-runtime'] else 'not found (optional)'}")
        return 0
    if args.command == "init":
        project = projects.init_project(args.directory, provider=args.provider)
        report = {
            "schema": "agentcfd.project-initialization/0.1",
            "template": args.template,
            "root": str(project.root),
            "entrypoint": str(project.entrypoint),
            "provider": project.manifest.default_provider,
        }
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Created AgentCFD {args.template} project")
            print(project.root)
        return 0
    if args.command == "check":
        plan = projects.Project(args.project).plan(
            provider=args.provider,
            container_image=args.container_image,
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
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for contract in report["contracts"]:
                print(f"{contract['name']}: {contract['id']}")
        return 0
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
        )
        report = bundle.to_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"Exported {bundle.frame_count} frames | XDMF/H5 + safe NPZ"
            )
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
            print(f"Exported tensor-ready field sample | {args.field}:{args.association}")
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
    if args.command == "properties" and args.property_operation == "state":
        state = properties.CoolPropPropertyProvider().at_pressure_temperature(
            args.fluid,
            pressure=args.pressure,
            temperature=args.temperature,
        ).to_dict()
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
        print(f"Accepted laminar pipe result | pressure drop {pressure_drop['value']:.6g} {pressure_drop['unit']}")
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
        manifest = _turbulent_openfoam_provider(
            args.case_directory,
            cross_section_cells=args.cross_section_cells,
            axial_cells=args.axial_cells,
            nominal_wall_cell_fraction=args.nominal_wall_cell_fraction,
            precursor_case=args.precursor_case,
        ).prepare(step).to_dict()
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
        manifest = _turbulent_precursor_provider(
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
        ).prepare(step).to_dict()
        if args.as_json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print(
                "Prepared periodic OpenFOAM "
                f"{args.turbulence_model} inlet precursor"
            )
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
    if (
        args.command == "prepare"
        and args.provider == "openfoam-turbulent-model-study"
    ):
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
                print("warning: predicted y+ is outside the high-Re wall-function range")
        return 0
    if (
        args.command == "prepare"
        and args.provider == "openfoam-turbulent-model-sweep"
    ):
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
    if (
        args.command == "run"
        and args.provider == "openfoam-turbulent-model-study"
    ):
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
    if (
        args.command == "run"
        and args.provider == "openfoam-turbulent-model-sweep"
    ):
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
        return (
            0
            if payload["acceptance"]["uncertainty_promotion_accepted"]
            else 3
        )
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
        print(f"agentcfd: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(entrypoint())
