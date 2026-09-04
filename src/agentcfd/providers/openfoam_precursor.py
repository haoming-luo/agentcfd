"""Fully developed turbulent inlet precursor backed by periodic OpenFOAM flow.

The precursor is a separate, content-addressed numerical problem.  It solves a
single periodic axial layer of the same circular O-grid used by the downstream
pipe provider and retains the developed ``U``, ``k``, ``omega``, and ``nut``
fields as auditable artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import boundaries, engineering
from .._version import __version__
from .._validation import integer_at_least, positive_float
from ..errors import CaseIntegrityError, ProviderUnavailableError, UnsupportedCaseError
from ..jsonio import strict_json_object
from ..results import Artifact, Check, FieldRecord, History, Quantity, SimulationResult
from .base import ProviderDescriptor
from .openfoam import (
    OpenFOAMValidationPolicy,
    PreparedOpenFOAMCase,
    _analysis_sha256,
    _block_mesh_dict,
    _container_image_identity,
    _header,
    _is_positive_time_name,
    _latest_time_directory,
    _mesh_metric_checks,
    _mesh_quality_quantities,
    _nominal_wall_cell_height,
    _nominal_wall_fraction_from_block_mesh,
    _read_y_plus_series,
    _runtime_version,
    _runtime_version_key,
    _solver_residual_evidence,
    _stop_timed_out_container,
    _turbulence_properties,
    _unexpected_case_entries,
    _wall_normal_expansion_ratio,
    _write_mesh_manifest,
)


_CAPABILITY = "openfoam.periodic-k-omega-sst-circular-pipe-precursor"

_SUPPORTED_NUT_WALL_FUNCTIONS = (
    "nutUBlendedWallFunction",
    "nutUSpaldingWallFunction",
    "nutkWallFunction",
)


def _validated_nut_wall_function(value: str) -> str:
    selected = str(value).strip()
    if selected not in _SUPPORTED_NUT_WALL_FUNCTIONS:
        choices = ", ".join(_SUPPORTED_NUT_WALL_FUNCTIONS)
        raise ValueError(f"nut_wall_function must be one of: {choices}.")
    return selected


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMTurbulentWallStudy:
    """Content-addressed plan for a fixed-wall-cell precursor family."""

    directory: Path
    model_sha256: str
    nominal_wall_cell_fraction: float
    nut_wall_function: str
    cases: tuple[dict[str, object], ...]
    scientific_inputs: dict[str, object]
    schema: str = "agentcfd.openfoam-turbulent-wall-study/0.1"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "model_sha256": self.model_sha256,
            "quantity": "flow.pressure_gradient",
            "nominal_wall_cell_fraction": self.nominal_wall_cell_fraction,
            "nut_wall_function": self.nut_wall_function,
            "scientific_inputs": self.scientific_inputs,
            "cases": list(self.cases),
        }

    def write(self) -> Path:
        target = self.directory / "agentcfd-turbulent-wall-study.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMTurbulentWallFunctionStudy:
    """Content-addressed plan for an identical-mesh wall-function screen."""

    directory: Path
    model_sha256: str
    cross_section_cells: int
    nominal_wall_cell_fraction: float
    maximum_iterations: int
    cases: tuple[dict[str, object], ...]
    scientific_inputs: dict[str, object]
    schema: str = "agentcfd.openfoam-turbulent-wall-function-study/0.1"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "model_sha256": self.model_sha256,
            "quantity": "flow.darcy_friction_factor",
            "cross_section_cells": self.cross_section_cells,
            "nominal_wall_cell_fraction": self.nominal_wall_cell_fraction,
            "maximum_iterations": self.maximum_iterations,
            "scientific_inputs": self.scientific_inputs,
            "cases": list(self.cases),
        }

    def write(self) -> Path:
        target = self.directory / "agentcfd-turbulent-wall-function-study.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


def prepare_turbulent_wall_function_study(
    step,
    directory: str | Path,
    *,
    cross_section_cells: int = 16,
    nominal_wall_cell_fraction: float = 0.0625,
    maximum_iterations: int = 4000,
) -> PreparedOpenFOAMTurbulentWallFunctionStudy:
    """Prepare all supported SST momentum wall functions on one mesh."""

    cross_cells = integer_at_least(
        cross_section_cells,
        name="cross_section_cells",
        minimum=2,
    )
    iteration_limit = integer_at_least(
        maximum_iterations,
        name="maximum_iterations",
        minimum=5,
    )
    fraction = positive_float(
        nominal_wall_cell_fraction,
        name="nominal_wall_cell_fraction",
    )
    if fraction >= 1.0:
        raise ValueError("nominal_wall_cell_fraction must be below one.")
    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"OpenFOAM wall-function study directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    model_sha256 = step.model.fingerprint()
    cases: list[dict[str, object]] = []
    for index, wall_function in enumerate(_SUPPORTED_NUT_WALL_FUNCTIONS, start=1):
        relative = Path(f"wall-function-{index}-{wall_function}")
        prepared = OpenFOAMTurbulentPrecursorProvider(
            case_directory=root / relative,
            cross_section_cells=cross_cells,
            nominal_wall_cell_fraction=fraction,
            nut_wall_function=wall_function,
            maximum_iterations=iteration_limit,
        ).prepare(step)
        cases.append(
            {
                "index": index,
                "directory": str(relative),
                "nut_wall_function": wall_function,
                "expected_cell_count": 5 * cross_cells**2,
                "case_sha256": prepared.case_sha256,
                "result": str(relative / "agentcfd-result.json"),
            }
        )
    study = PreparedOpenFOAMTurbulentWallFunctionStudy(
        directory=root,
        model_sha256=model_sha256,
        cross_section_cells=cross_cells,
        nominal_wall_cell_fraction=fraction,
        maximum_iterations=iteration_limit,
        cases=tuple(cases),
        scientific_inputs={
            "model": step.model.to_dict(),
            "procedure": step.procedure.to_dict(),
            "output_request": step.output.to_dict(),
        },
    )
    study.write()
    return study


def prepare_turbulent_wall_study(
    step,
    directory: str | Path,
    *,
    cross_section_cells: tuple[int, int, int] = (8, 16, 32),
    nominal_wall_cell_fraction: float = 0.0625,
    nut_wall_function: str = "nutUBlendedWallFunction",
    maximum_iterations: tuple[int, int, int] = (1000, 4000, 6000),
) -> PreparedOpenFOAMTurbulentWallStudy:
    """Prepare three precursor cases with one fixed nominal wall-cell height."""

    selected = tuple(
        integer_at_least(value, name="cross_section_cells", minimum=2)
        for value in cross_section_cells
    )
    iterations = tuple(
        integer_at_least(value, name="maximum_iterations", minimum=5)
        for value in maximum_iterations
    )
    if len(selected) != 3 or len(iterations) != 3:
        raise ValueError("Exactly three grid counts and iteration limits are required.")
    if not (selected[0] < selected[1] < selected[2]):
        raise ValueError("Cross-section cell counts must be strictly increasing.")
    fraction = positive_float(
        nominal_wall_cell_fraction,
        name="nominal_wall_cell_fraction",
    )
    if fraction >= 1.0:
        raise ValueError("nominal_wall_cell_fraction must be below one.")
    wall_function = _validated_nut_wall_function(nut_wall_function)
    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"OpenFOAM wall-study directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    model_sha256 = step.model.fingerprint()
    cases: list[dict[str, object]] = []
    for index, (cross_cells, iteration_limit) in enumerate(
        zip(selected, iterations),
        start=1,
    ):
        relative = Path(f"wall-{index}-c{cross_cells}")
        prepared = OpenFOAMTurbulentPrecursorProvider(
            case_directory=root / relative,
            cross_section_cells=cross_cells,
            nominal_wall_cell_fraction=fraction,
            nut_wall_function=wall_function,
            maximum_iterations=iteration_limit,
        ).prepare(step)
        if prepared.model_sha256 != model_sha256:
            raise RuntimeError("Prepared wall-study cases do not share one model identity.")
        cases.append(
            {
                "index": index,
                "label": ("coarse", "medium", "fine")[index - 1],
                "directory": str(relative),
                "cross_section_cells": cross_cells,
                "maximum_iterations": iteration_limit,
                "expected_cell_count": 5 * cross_cells**2,
                "case_sha256": prepared.case_sha256,
                "result": str(relative / "agentcfd-result.json"),
            }
        )
    study = PreparedOpenFOAMTurbulentWallStudy(
        directory=root,
        model_sha256=model_sha256,
        nominal_wall_cell_fraction=fraction,
        nut_wall_function=wall_function,
        cases=tuple(cases),
        scientific_inputs={
            "model": step.model.to_dict(),
            "procedure": step.procedure.to_dict(),
            "output_request": step.output.to_dict(),
        },
    )
    study.write()
    return study


def _precursor_series(log: str) -> tuple[dict[float, float], dict[float, float]]:
    """Recover target-direction bulk velocity and kinematic pressure gradient."""

    current_iteration: float | None = None
    bulk_velocity: dict[float, float] = {}
    pressure_gradient: dict[float, float] = {}
    time_pattern = re.compile(r"^\s*Time\s*=\s*([0-9.eE+-]+)\s*$")
    sample_pattern = re.compile(
        r"Pressure gradient source:\s*uncorrected Ubar\s*=\s*([0-9.eE+-]+),\s*"
        r"pressure gradient\s*=\s*([0-9.eE+-]+)"
    )
    for line in log.splitlines():
        time_match = time_pattern.match(line)
        if time_match is not None:
            try:
                value = float(time_match.group(1))
            except ValueError:
                current_iteration = None
            else:
                current_iteration = value if math.isfinite(value) else None
            continue
        sample = sample_pattern.search(line)
        if sample is None or current_iteration is None:
            continue
        try:
            velocity = float(sample.group(1))
            gradient = float(sample.group(2))
        except ValueError:
            continue
        if math.isfinite(velocity) and math.isfinite(gradient):
            bulk_velocity[current_iteration] = velocity
            pressure_gradient[current_iteration] = gradient
    return bulk_velocity, pressure_gradient


def _tail_relative_range(values: tuple[float, ...], sample_count: int) -> float | None:
    if len(values) < sample_count:
        return None
    tail = values[-sample_count:]
    scale = max(abs(tail[-1]), 1.0e-300)
    return (max(tail) - min(tail)) / scale


def _precursor_residual_check(
    quantities: dict[str, Quantity],
    *,
    tolerance: float,
) -> Check:
    initial_prefix = "solver.initial_residual."
    final_prefix = "solver.final_residual."
    initial = {
        name.removeprefix(initial_prefix): quantity.value
        for name, quantity in quantities.items()
        if name.startswith(initial_prefix)
    }
    final = {
        name.removeprefix(final_prefix): quantity.value
        for name, quantity in quantities.items()
        if name.startswith(final_prefix)
    }
    axial_names = tuple(name for name in ("Uz", "U") if name in initial)
    missing = [name for name in ("k", "omega") if name not in initial]
    has_velocity = bool(axial_names)
    if not has_velocity:
        missing.insert(0, "axial velocity")
    if "p" not in final:
        missing.insert(0, "pressure")
    selected = [initial[name] for name in (*axial_names, "k", "omega") if name in initial]
    selected.extend(
        value
        for name, value in final.items()
        if name == "p" or (name.startswith("U") and name not in axial_names)
    )
    maximum = max(selected, default=None)
    passed = bool(not missing and maximum is not None and maximum <= tolerance)
    return Check(
        name="precursor-residual-target",
        passed=passed,
        value=maximum,
        limit=tolerance,
        message=(
            "Axial/turbulence outer residuals and pressure/transverse linear "
            "residuals meet the periodic-flow target."
            if passed
            else "Missing equations: " + ", ".join(missing)
            if missing
            else "A developed velocity or turbulence residual exceeds the target."
        ),
        kind="verification",
        observable="solver.residual",
    )


class OpenFOAMTurbulentPrecursorProvider:
    """Generate and execute a periodic, fully developed circular-pipe precursor."""

    def __init__(
        self,
        *,
        case_directory: str | Path | None = None,
        cross_section_cells: int = 16,
        nominal_wall_cell_fraction: float | None = None,
        nut_wall_function: str = "nutUBlendedWallFunction",
        maximum_iterations: int = 1000,
        validation: OpenFOAMValidationPolicy | None = None,
        timeout_seconds: float = 3600.0,
        container_image: str | None = None,
    ) -> None:
        self.case_directory = Path(case_directory) if case_directory is not None else None
        self.cross_section_cells = integer_at_least(
            cross_section_cells,
            name="cross_section_cells",
            minimum=2,
        )
        if nominal_wall_cell_fraction is None:
            self.nominal_wall_cell_fraction = None
        else:
            fraction = positive_float(
                nominal_wall_cell_fraction,
                name="nominal_wall_cell_fraction",
            )
            if fraction >= 1.0:
                raise ValueError("nominal_wall_cell_fraction must be below one.")
            self.nominal_wall_cell_fraction = fraction
        self.nut_wall_function = _validated_nut_wall_function(nut_wall_function)
        self.maximum_iterations = integer_at_least(
            maximum_iterations,
            name="maximum_iterations",
            minimum=5,
        )
        self.validation = validation or OpenFOAMValidationPolicy()
        self.timeout_seconds = positive_float(timeout_seconds, name="timeout_seconds")
        self.container_image = str(container_image).strip() if container_image else None

    def _commands(self) -> dict[str, str | None]:
        if self.container_image is not None:
            docker = shutil.which("docker")
            return {name: docker for name in ("blockMesh", "checkMesh", "simpleFoam")}
        return {
            "blockMesh": shutil.which("blockMesh"),
            "checkMesh": shutil.which("checkMesh"),
            "simpleFoam": shutil.which("simpleFoam"),
        }

    def _execution_argv(
        self,
        name: str,
        command: str,
        case_directory: Path,
        *,
        cidfile: Path | None = None,
    ) -> list[str]:
        if self.container_image is None:
            return [command, "-case", str(case_directory)]
        argv = [command, "run", "--rm"]
        if cidfile is not None:
            argv.extend(("--cidfile", str(cidfile)))
        argv.extend(
            (
                "-v",
                f"{case_directory.resolve()}:/case",
                "-w",
                "/case",
                self.container_image,
                name,
                "-case",
                "/case",
            )
        )
        return argv

    def descriptor(self) -> ProviderDescriptor:
        commands = self._commands()
        return ProviderDescriptor(
            name="openfoam-periodic-precursor",
            version=self.container_image or os.environ.get("WM_PROJECT_VERSION", "externally-managed"),
            license="GPL-3.0-or-later (external program)",
            available=all(commands.values()),
            execution_boundary=(
                "filesystem-and-container-subprocess"
                if self.container_image
                else "filesystem-and-subprocess"
            ),
            capabilities=(_CAPABILITY,),
        )

    def prepare(self, step, directory: str | Path | None = None) -> PreparedOpenFOAMCase:
        step.model.validate()
        self._validate_supported(step)
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM precursor case_directory is required.")
        if target.exists() and any(target.iterdir()):
            raise FileExistsError(f"OpenFOAM precursor directory is not empty: {target}")
        target.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for relative, content in sorted(self._render_files(step).items()):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            path.write_bytes(data)
            hashes[relative] = hashlib.sha256(data).hexdigest()
        case_sha256 = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prepared = PreparedOpenFOAMCase(
            directory=target,
            model_sha256=step.model.fingerprint(),
            analysis_sha256=_analysis_sha256(step),
            case_sha256=case_sha256,
            files=hashes,
            capability=_CAPABILITY,
        )
        (target / "agentcfd-case.json").write_text(
            json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return prepared

    def run(self, step) -> SimulationResult:
        return self._execute(step, self.prepare(step))

    def run_prepared(self, step, directory: str | Path | None = None) -> SimulationResult:
        prepared = self._load_prepared(step, directory)
        execution_outputs = [
            path
            for path in prepared.directory.iterdir()
            if path.name.startswith("log.")
            or path.name == "postProcessing"
            or (path.is_dir() and _is_positive_time_name(path.name))
        ]
        if (prepared.directory / "constant/polyMesh").exists():
            execution_outputs.append(prepared.directory / "constant/polyMesh")
        if execution_outputs:
            raise CaseIntegrityError(
                "Prepared OpenFOAM precursor already contains execution output: "
                + ", ".join(sorted(path.name for path in execution_outputs))
            )
        unexpected = _unexpected_case_entries(prepared)
        if unexpected:
            raise CaseIntegrityError(
                "Prepared OpenFOAM precursor contains unrecorded entries: "
                + ", ".join(unexpected)
            )
        self._verify_prepared_controls(prepared)
        return self._execute(step, prepared)

    def _verify_prepared_controls(self, prepared: PreparedOpenFOAMCase) -> None:
        """Bind runtime expectations to the controls in the verified case files."""

        block_mesh = (prepared.directory / "system/blockMeshDict").read_text(
            encoding="utf-8"
        )
        matches = re.findall(
            r"hex\s+\([^)]*\)\s+\((\d+)\s+(\d+)\s+(\d+)\)",
            block_mesh,
        )
        expected_resolution = (
            self.cross_section_cells,
            self.cross_section_cells,
            1,
        )
        if len(matches) != 5 or any(
            tuple(int(value) for value in match) != expected_resolution
            for match in matches
        ):
            raise CaseIntegrityError(
                "Prepared precursor mesh resolution differs from runtime controls."
            )
        fraction = _nominal_wall_fraction_from_block_mesh(block_mesh)
        if fraction != self.nominal_wall_cell_fraction:
            raise CaseIntegrityError(
                "Prepared precursor near-wall grading differs from runtime controls."
            )
        control = (prepared.directory / "system/controlDict").read_text(
            encoding="utf-8"
        )
        end_time = re.search(r"(?m)^endTime\s+(\d+)\s*;\s*$", control)
        if end_time is None or int(end_time.group(1)) != self.maximum_iterations:
            raise CaseIntegrityError(
                "Prepared precursor iteration limit differs from runtime controls."
            )
        nut = (prepared.directory / "0/nut").read_text(encoding="utf-8")
        wall_type = re.search(r"(?m)^\s*type\s+(nut\w+WallFunction)\s*;\s*$", nut)
        if wall_type is None or wall_type.group(1) != self.nut_wall_function:
            raise CaseIntegrityError(
                "Prepared precursor momentum wall function differs from runtime controls."
            )

    def _load_prepared(
        self,
        step,
        directory: str | Path | None,
    ) -> PreparedOpenFOAMCase:
        step.model.validate()
        self._validate_supported(step)
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM precursor case_directory is required.")
        manifest_path = target / "agentcfd-case.json"
        try:
            manifest = strict_json_object(
                manifest_path.read_text(encoding="utf-8"),
                label=f"OpenFOAM precursor manifest {manifest_path}",
            )
        except (FileNotFoundError, ValueError) as error:
            raise CaseIntegrityError("Prepared OpenFOAM precursor manifest is missing or invalid.") from error
        expected = {
            "schema": "agentcfd.openfoam-case/0.3",
            "capability": _CAPABILITY,
            "model_sha256": step.model.fingerprint(),
            "analysis_sha256": _analysis_sha256(step),
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise CaseIntegrityError(f"Prepared OpenFOAM precursor has a different {key}.")
        recorded = manifest.get("files")
        if not isinstance(recorded, dict) or not recorded:
            raise CaseIntegrityError("Prepared OpenFOAM precursor has no recorded files.")
        verified: dict[str, str] = {}
        root = target.resolve()
        for relative, digest in recorded.items():
            if not isinstance(relative, str) or not isinstance(digest, str):
                raise CaseIntegrityError("Prepared precursor file identities are malformed.")
            path = (target / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise CaseIntegrityError(f"Prepared precursor file escapes its root: {relative!r}") from error
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise CaseIntegrityError(f"Prepared precursor file is missing or changed: {relative}")
            verified[relative] = digest
        case_sha256 = hashlib.sha256(
            json.dumps(verified, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if manifest.get("case_sha256") != case_sha256:
            raise CaseIntegrityError("Prepared OpenFOAM precursor identity has changed.")
        return PreparedOpenFOAMCase(
            directory=target,
            model_sha256=expected["model_sha256"],
            analysis_sha256=expected["analysis_sha256"],
            case_sha256=case_sha256,
            files=verified,
            capability=_CAPABILITY,
        )

    def _validate_supported(self, step) -> None:
        model = step.model
        study = model.study
        if (
            not study.steady
            or study.compressible
            or study.energy
            or study.reacting
            or study.turbulence != "k-omega-sst"
            or study.wall_treatment != "blended-wall-functions"
        ):
            raise UnsupportedCaseError(
                "The periodic pipe precursor supports steady incompressible isothermal "
                "k-omega SST flow with blended wall functions only."
            )
        if model.domain.roughness != 0.0:
            raise UnsupportedCaseError("The periodic pipe precursor requires a smooth pipe.")
        inlets = [
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.TurbulentMeanVelocityInlet)
        ]
        walls = [
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.NoSlipWall)
        ]
        if len(inlets) != 1 or len(walls) != 1:
            raise UnsupportedCaseError(
                "The periodic pipe precursor requires one turbulent inlet and one no-slip wall."
            )
        reynolds = (
            model.fluid.density
            * inlets[0].velocity
            * model.domain.diameter
            / model.fluid.dynamic_viscosity
        )
        if reynolds < 4000.0:
            raise UnsupportedCaseError(
                f"Re={reynolds:.6g} is outside the turbulent precursor range Re >= 4000."
            )
        estimated_aspect = self._estimated_axial_to_radial_ratio(model.domain.diameter)
        if estimated_aspect > self.validation.maximum_mesh_aspect_ratio:
            raise UnsupportedCaseError(
                "The requested precursor grading has an estimated axial-to-smallest-"
                f"radial cell ratio {estimated_aspect:.6g}, above the declared "
                f"{self.validation.maximum_mesh_aspect_ratio:g} mesh-aspect limit."
            )

    def _estimated_axial_to_radial_ratio(self, diameter: float) -> float:
        radius = diameter / 2.0
        outer_edge = radius * (1.0 - math.sqrt(2.0) / 3.0)
        fraction = (
            1.0 / self.cross_section_cells
            if self.nominal_wall_cell_fraction is None
            else self.nominal_wall_cell_fraction
        )
        wall_cell = outer_edge * fraction
        wall_to_core = _wall_normal_expansion_ratio(
            self.cross_section_cells,
            self.nominal_wall_cell_fraction,
        )
        smallest_radial = wall_cell * min(1.0, wall_to_core)
        axial_cell = diameter / self.cross_section_cells
        return axial_cell / smallest_radial

    def _render_files(self, step) -> dict[str, str]:
        model = step.model
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.TurbulentMeanVelocityInlet)
        )
        wall_name = next(
            name
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.NoSlipWall)
        )
        estimate = engineering.turbulence_inlet_from_intensity(
            mean_velocity=inlet.velocity,
            intensity=inlet.turbulence_intensity,
            length_scale=inlet.turbulence_length_scale,
        )
        thickness = model.domain.diameter / max(self.cross_section_cells, 2)
        return {
            "0/U": _precursor_velocity_field(wall_name, inlet.velocity),
            **_precursor_turbulence_fields(
                wall_name,
                kinetic_energy=estimate.turbulent_kinetic_energy,
                specific_dissipation_rate=estimate.specific_dissipation_rate,
                nut_wall_function=self.nut_wall_function,
            ),
            "0/p": _precursor_pressure_field(wall_name),
            "constant/fvOptions": _precursor_fv_options(inlet.velocity),
            "constant/transportProperties": _precursor_transport_properties(
                model.fluid.kinematic_viscosity
            ),
            "constant/turbulenceProperties": _turbulence_properties(turbulent=True),
            "system/blockMeshDict": _block_mesh_dict(
                length=thickness,
                radius=model.domain.diameter / 2.0,
                cross_cells=self.cross_section_cells,
                axial_cells=1,
                inlet="periodic_in",
                outlet="periodic_out",
                walls=(wall_name,),
                cyclic_end_planes=True,
                nominal_wall_cell_fraction=self.nominal_wall_cell_fraction,
            ),
            "system/controlDict": _precursor_control_dict(self.maximum_iterations),
            "system/fvSchemes": _precursor_fv_schemes(),
            "system/fvSolution": _precursor_fv_solution(step.procedure.relative_tolerance),
        }

    def _execute(self, step, prepared: PreparedOpenFOAMCase) -> SimulationResult:
        commands = self._commands()
        missing = [name for name, command in commands.items() if command is None]
        if missing:
            if self.container_image is not None:
                raise ProviderUnavailableError("OpenFOAM precursor container execution requires docker.")
            raise ProviderUnavailableError(
                "OpenFOAM precursor execution requires commands on PATH: " + ", ".join(missing)
            )
        logs: dict[str, str] = {}
        return_codes: dict[str, int] = {}
        durations: dict[str, float] = {}
        for name in ("blockMesh", "checkMesh", "simpleFoam"):
            cidfile = (
                prepared.directory / f".agentcfd-{name}.cid"
                if self.container_image is not None
                else None
            )
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    self._execution_argv(
                        name,
                        str(commands[name]),
                        prepared.directory,
                        cidfile=cidfile,
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                combined = completed.stdout + completed.stderr
                return_codes[name] = completed.returncode
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
                combined = stdout + stderr + f"\nAgentCFD timeout after {self.timeout_seconds:g} seconds.\n"
                if cidfile is not None:
                    combined += _stop_timed_out_container(str(commands[name]), cidfile) + "\n"
                return_codes[name] = -124
            except KeyboardInterrupt:
                if cidfile is not None:
                    _stop_timed_out_container(str(commands[name]), cidfile)
                raise
            finally:
                durations[name] = time.monotonic() - started
                if cidfile is not None:
                    cidfile.unlink(missing_ok=True)
            logs[name] = combined
            (prepared.directory / f"log.{name}").write_text(combined, encoding="utf-8")
            if return_codes[name] != 0:
                break
        return self._result(step, prepared, logs, return_codes, durations, commands)

    def _result(
        self,
        step,
        prepared: PreparedOpenFOAMCase,
        logs: dict[str, str],
        return_codes: dict[str, int],
        durations: dict[str, float],
        commands: dict[str, str | None],
    ) -> SimulationResult:
        process_ok = all(return_codes.get(name) == 0 for name in ("blockMesh", "checkMesh", "simpleFoam"))
        solver_log = logs.get("simpleFoam", "")
        reached_end = process_ok and bool(re.search(r"(?m)^End\s*$", solver_log))
        inlet = next(
            value
            for value in step.model.boundary_conditions.values()
            if isinstance(value, boundaries.TurbulentMeanVelocityInlet)
        )
        velocity_series, gradient_series = _precursor_series(solver_log)
        ordered_times = tuple(sorted(set(velocity_series) & set(gradient_series)))
        velocities = tuple(velocity_series[value] for value in ordered_times)
        gradients = tuple(gradient_series[value] for value in ordered_times)
        quantities, histories = _solver_residual_evidence(solver_log)
        if ordered_times:
            histories["flow.bulk_velocity"] = History(
                ordered_times,
                velocities,
                unit="m/s",
                abscissa_name="iteration",
                abscissa_unit="1",
            )
            histories["flow.kinematic_pressure_gradient"] = History(
                ordered_times,
                gradients,
                unit="m/s^2",
                abscissa_name="iteration",
                abscissa_unit="1",
            )
            final_velocity = velocities[-1]
            final_gradient = gradients[-1]
            friction = 2.0 * step.model.domain.diameter * abs(final_gradient) / final_velocity**2
            reynolds = engineering.reynolds_number(
                density=step.model.fluid.density,
                mean_velocity=final_velocity,
                hydraulic_diameter=step.model.domain.diameter,
                dynamic_viscosity=step.model.fluid.dynamic_viscosity,
            )
            reference_friction = engineering.darcy_friction_factor(reynolds)
            friction_error = abs(friction - reference_friction) / reference_friction
            quantities.update(
                {
                    "flow.mean_velocity": Quantity(final_velocity, "m/s"),
                    "flow.volume_flow_rate": Quantity(final_velocity * step.model.domain.area, "m^3/s"),
                    "flow.reynolds_number": Quantity(reynolds, "1"),
                    "flow.kinematic_pressure_gradient": Quantity(final_gradient, "m/s^2"),
                    "flow.pressure_gradient": Quantity(
                        step.model.fluid.density * final_gradient,
                        "Pa/m",
                    ),
                    "flow.darcy_friction_factor": Quantity(friction, "1"),
                    "reference.flow.darcy_friction_factor": Quantity(reference_friction, "1"),
                    "flow.darcy_friction_factor_relative_error": Quantity(friction_error, "1"),
                }
            )
        else:
            final_velocity = None
            friction_error = None
        for name, duration in durations.items():
            quantities[f"runtime.{name}.wall_seconds"] = Quantity(duration, "s", kind="runtime_metric")
        quantities["runtime.total_wall_seconds"] = Quantity(
            sum(durations.values()),
            "s",
            kind="runtime_metric",
        )
        mesh_quantities = _mesh_quality_quantities(logs.get("checkMesh", ""))
        quantities.update(mesh_quantities)
        quantities["mesh.estimated_axial_to_smallest_radial_cell_ratio"] = Quantity(
            self._estimated_axial_to_radial_ratio(step.model.domain.diameter),
            "1",
            kind="scientific_input",
        )
        quantities["mesh.nominal_wall_cell_height"] = Quantity(
            _nominal_wall_cell_height(
                radius=step.model.domain.diameter / 2.0,
                cross_cells=self.cross_section_cells,
                nominal_wall_cell_fraction=self.nominal_wall_cell_fraction,
            ),
            "m",
            kind="scientific_input",
            description=(
                "Design height on the O-grid corner-to-core radial edge; curved-face "
                "cell-centre wall distance is recovered separately through y+."
            ),
        )
        residual_check = _precursor_residual_check(
            quantities,
            tolerance=self.validation.maximum_turbulent_outer_residual,
        )
        gradient_drift = _tail_relative_range(
            gradients,
            self.validation.minimum_precursor_steady_samples,
        )
        gradient_check = Check(
            name="pressure-gradient-tail-stability",
            passed=bool(
                gradient_drift is not None
                and gradient_drift <= self.validation.maximum_relative_turbulent_pressure_drop_drift
            ),
            value=gradient_drift,
            limit=self.validation.maximum_relative_turbulent_pressure_drop_drift,
            kind="verification",
            observable="flow.pressure_gradient",
        )
        velocity_error = (
            abs(final_velocity - inlet.velocity) / inlet.velocity
            if final_velocity is not None
            else None
        )
        velocity_check = Check(
            name="target-bulk-velocity",
            passed=bool(
                velocity_error is not None
                and velocity_error <= self.validation.maximum_relative_inlet_flow_error
            ),
            value=velocity_error,
            limit=self.validation.maximum_relative_inlet_flow_error,
            kind="verification",
            observable="flow.mean_velocity",
        )
        y_series = _read_y_plus_series(prepared.directory)
        common_y = sorted(set(y_series["minimum"]) & set(y_series["maximum"]) & set(y_series["average"]))
        if common_y:
            for label in ("minimum", "maximum", "average"):
                values = tuple(y_series[label][value] for value in common_y)
                histories[f"wall.y_plus.{label}"] = History(
                    tuple(common_y),
                    values,
                    unit="1",
                    abscissa_name="iteration",
                    abscissa_unit="1",
                )
                quantities[f"wall.y_plus.{label}"] = Quantity(values[-1], "1")
            minimum_y = quantities["wall.y_plus.minimum"].value
            maximum_y = quantities["wall.y_plus.maximum"].value
        else:
            minimum_y = maximum_y = None
        y_check = Check(
            name="wall-y-plus-range",
            passed=bool(
                minimum_y is not None
                and maximum_y is not None
                and minimum_y >= self.validation.minimum_wall_y_plus
                and maximum_y <= self.validation.maximum_wall_y_plus
            ),
            value=(
                f"{minimum_y:.6g}..{maximum_y:.6g}"
                if minimum_y is not None and maximum_y is not None
                else None
            ),
            limit=(
                f"[{self.validation.minimum_wall_y_plus:g}, "
                f"{self.validation.maximum_wall_y_plus:g}]"
            ),
            kind="verification",
            observable="wall.y_plus",
        )
        friction_check = Check(
            name="smooth-pipe-friction-correlation",
            passed=bool(
                friction_error is not None
                and friction_error <= self.validation.maximum_relative_turbulent_friction_error
            ),
            value=friction_error,
            limit=self.validation.maximum_relative_turbulent_friction_error,
            message="Correlation agreement is diagnostic until a precursor grid study passes.",
            kind="verification",
            observable="flow.darcy_friction_factor",
        )
        expected_cells = 5 * self.cross_section_cells**2
        actual_cells = quantities.get("mesh.cell_count")
        mesh_sha256, mesh_manifest = _write_mesh_manifest(prepared.directory)
        latest = _latest_time_directory(prepared.directory)
        field_units = {
            "U": "m/s",
            "p": "m^2/s^2",
            "k": "m^2/s^2",
            "omega": "1/s",
            "nut": "m^2/s",
        }
        fields: dict[str, FieldRecord] = {}
        artifact_paths: dict[str, Path] = {
            "case_manifest": prepared.directory / "agentcfd-case.json",
            **{f"log_{name}": prepared.directory / f"log.{name}" for name in logs},
        }
        if mesh_manifest is not None:
            artifact_paths["mesh_manifest"] = mesh_manifest
        if latest is not None:
            for name, unit in field_units.items():
                path = latest / name
                if path.is_file():
                    fields[name] = FieldRecord(
                        location="cell",
                        unit=unit,
                        artifact=str(path),
                        components=("x", "y", "z") if name == "U" else (),
                        mesh_sha256=mesh_sha256,
                        description="Fully developed periodic precursor field.",
                    )
                    artifact_paths[f"field_{name}"] = path
        descriptor = self.descriptor()
        runtime_version = _runtime_version(logs, descriptor.version)
        version_ok = _runtime_version_key(runtime_version) in {
            _runtime_version_key(value)
            for value in self.validation.validated_runtime_versions
        }
        container_identity: dict[str, Any] | None = None
        container_checks: tuple[Check, ...] = ()
        if self.container_image is not None:
            docker_command = next(str(value) for value in commands.values() if value is not None)
            container_identity = _container_image_identity(
                docker_command,
                self.container_image,
                timeout_seconds=self.timeout_seconds,
            )
            container_checks = (
                Check(
                    name="container-image-identity",
                    passed=bool(container_identity["identity_verified"]),
                    value=container_identity.get("image_id") or container_identity.get("inspection_error", "missing"),
                    limit="immutable local image SHA-256 is recorded",
                    kind="runtime",
                    observable="provider.container_identity",
                ),
            )
        converged = bool(
            process_ok
            and reached_end
            and residual_check.passed
            and gradient_check.passed
            and velocity_check.passed
            and y_check.passed
            and len(fields) == len(field_units)
        )
        checks = (
            Check(
                name="openfoam-process",
                passed=process_ok,
                value=json.dumps(return_codes, sort_keys=True),
                limit="all return codes equal zero",
                kind="runtime",
                observable="provider.process",
            ),
            Check(
                name="solver-completion-marker",
                passed=reached_end,
                value="found" if reached_end else "missing",
                limit="periodic simpleFoam log ends with End",
                kind="runtime",
                observable="provider.convergence_marker",
            ),
            residual_check,
            gradient_check,
            velocity_check,
            Check(
                name="mesh-quality",
                passed=return_codes.get("checkMesh") == 0 and "Mesh OK" in logs.get("checkMesh", ""),
                value="Mesh OK" if "Mesh OK" in logs.get("checkMesh", "") else "not confirmed",
                limit="checkMesh succeeds and reports Mesh OK",
                kind="verification",
                observable="mesh.quality",
            ),
            *_mesh_metric_checks(mesh_quantities, policy=self.validation),
            Check(
                name="mesh-cell-count",
                passed=bool(actual_cells is not None and actual_cells.value == expected_cells),
                value=actual_cells.value if actual_cells is not None else None,
                limit=float(expected_cells),
                kind="verification",
                observable="mesh.cell_count",
            ),
            Check(
                name="mesh-identity",
                passed=mesh_sha256 is not None,
                value=mesh_sha256 or "missing",
                limit="content-addressed polyMesh manifest exists",
                kind="verification",
                observable="mesh.identity",
            ),
            Check(
                name="openfoam-runtime-version",
                passed=version_ok,
                value=runtime_version,
                limit="validated versions: " + ", ".join(self.validation.validated_runtime_versions),
                kind="runtime",
                observable="provider.version",
            ),
            Check(
                name="developed-field-completeness",
                passed=len(fields) == len(field_units),
                value=", ".join(sorted(fields)),
                limit="U, p, k, omega, and nut are recovered",
                kind="runtime",
                observable="result.outputs",
            ),
            y_check,
            friction_check,
            *container_checks,
        )
        return SimulationResult(
            name="turbulent-circular-pipe-precursor",
            status="completed" if process_ok else "failed",
            converged=converged,
            provider="openfoam-periodic-precursor",
            quantities=quantities,
            histories=histories,
            fields=fields,
            checks=checks,
            artifacts={
                name: Artifact.from_path(
                    path,
                    role="precursor-evidence",
                    media_type="application/json" if path.suffix == ".json" else "text/plain",
                )
                for name, path in artifact_paths.items()
            },
            scientific_inputs={
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
                "precursor": {
                    "solver": "simpleFoam",
                    "driving_source": "meanVelocityForce",
                    "cross_section_cells": self.cross_section_cells,
                    "axial_cells": 1,
                    "nominal_wall_cell_fraction": self.nominal_wall_cell_fraction,
                    "nut_wall_function": self.nut_wall_function,
                    "maximum_iterations": self.maximum_iterations,
                    "periodic_end_planes": True,
                },
                "validation_policy": asdict(self.validation),
                "lowered_case_sha256": prepared.case_sha256,
            },
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": step.model.fingerprint(),
                "analysis_sha256": prepared.analysis_sha256,
                "case_sha256": prepared.case_sha256,
                "mesh_sha256": mesh_sha256,
                "provider": "openfoam-periodic-precursor",
                "provider_version": runtime_version,
                "execution_boundary": descriptor.execution_boundary,
                "container_image": self.container_image,
                "container_identity": container_identity,
                "command_return_codes": return_codes,
                "command_wall_seconds": durations,
                "case_manifest": str(prepared.directory / "agentcfd-case.json"),
            },
            messages=(
                "The periodic meanVelocityForce fields are a developed-inlet precursor; "
                "downstream mapping and a three-grid certificate remain separate gates.",
            ),
        )


def _precursor_velocity_field(wall: str, velocity: float) -> str:
    return _header(object_name="U", class_name="volVectorField", location="0") + f"""dimensions      [0 1 -1 0 0 0 0];
internalField   uniform (0 0 {velocity:.17g});
boundaryField
{{
    periodic_in
    {{
        type cyclic;
    }}
    periodic_out
    {{
        type cyclic;
    }}
    {wall}
    {{
        type noSlip;
    }}
}}
"""


def _precursor_turbulence_fields(
    wall: str,
    *,
    kinetic_energy: float,
    specific_dissipation_rate: float,
    nut_wall_function: str = "nutUBlendedWallFunction",
) -> dict[str, str]:
    nut_wall_function = _validated_nut_wall_function(nut_wall_function)
    specifications = {
        "k": ("[0 2 -2 0 0 0 0]", kinetic_energy, "kqRWallFunction", ""),
        "omega": (
            "[0 0 -1 0 0 0 0]",
            specific_dissipation_rate,
            "omegaWallFunction",
            "        blending binomial;\n",
        ),
        "nut": ("[0 2 -1 0 0 0 0]", 0.0, nut_wall_function, ""),
    }
    files: dict[str, str] = {}
    for name, (dimensions, value, wall_type, extra) in specifications.items():
        files[f"0/{name}"] = _header(
            object_name=name,
            class_name="volScalarField",
            location="0",
        ) + f"""dimensions      {dimensions};
internalField   uniform {value:.17g};
boundaryField
{{
    periodic_in
    {{
        type cyclic;
    }}
    periodic_out
    {{
        type cyclic;
    }}
    {wall}
    {{
        type {wall_type};
{extra}        value uniform {value:.17g};
    }}
}}
"""
    return files


def _precursor_pressure_field(wall: str) -> str:
    return _header(object_name="p", class_name="volScalarField", location="0") + f"""dimensions      [0 2 -2 0 0 0 0];
internalField   uniform 0;
boundaryField
{{
    periodic_in
    {{
        type cyclic;
    }}
    periodic_out
    {{
        type cyclic;
    }}
    {wall}
    {{
        type zeroGradient;
    }}
}}
"""


def _precursor_fv_options(velocity: float) -> str:
    return _header(
        object_name="fvOptions",
        class_name="dictionary",
        location="constant",
    ) + f"""momentumSource
{{
    type meanVelocityForce;
    selectionMode all;
    fields (U);
    Ubar (0 0 {velocity:.17g});
    relaxation 1;
}}
"""


def _precursor_transport_properties(nu: float) -> str:
    return _header(
        object_name="transportProperties",
        class_name="dictionary",
        location="constant",
    ) + f"""transportModel  Newtonian;
nu              [0 2 -1 0 0 0 0] {nu:.17g};
"""


def _precursor_control_dict(maximum_iterations: int) -> str:
    return _header(object_name="controlDict", class_name="dictionary", location="system") + f"""application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {maximum_iterations};
deltaT          1;
writeControl    timeStep;
writeInterval   {maximum_iterations};
purgeWrite      1;
writeFormat     ascii;
writePrecision  10;
runTimeModifiable true;

functions
{{
    agentcfd_y_plus
    {{
        type yPlus;
        libs (fieldFunctionObjects);
        executeControl timeStep;
        executeInterval 1;
        writeControl timeStep;
        writeInterval 1;
        writeToFile true;
        writeFields false;
        log true;
    }}
}}
"""


def _precursor_fv_schemes() -> str:
    return _header(object_name="fvSchemes", class_name="dictionary", location="system") + """ddtSchemes
{
    default steadyState;
}
gradSchemes
{
    default Gauss linear;
}
divSchemes
{
    default none;
    div(phi,U) bounded Gauss linearUpwind grad(U);
    turbulence bounded Gauss linear;
    div(phi,k) $turbulence;
    div(phi,omega) $turbulence;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}
laplacianSchemes
{
    default Gauss linear corrected;
}
interpolationSchemes
{
    default linear;
}
snGradSchemes
{
    default corrected;
}
wallDist
{
    method meshWave;
}
"""


def _precursor_fv_solution(tolerance: float) -> str:
    return _header(object_name="fvSolution", class_name="dictionary", location="system") + f"""solvers
{{
    p
    {{
        solver GAMG;
        tolerance {tolerance:.17g};
        relTol 0;
        smoother GaussSeidel;
    }}
    U
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance {tolerance:.17g};
        relTol 0;
    }}
    \"(k|omega)\"
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance {tolerance:.17g};
        relTol 0;
    }}
}}
SIMPLE
{{
    nNonOrthogonalCorrectors 0;
    consistent yes;
    pRefCell 0;
    pRefValue 0;
    residualControl
    {{
        p {tolerance:.17g};
        U {tolerance:.17g};
        k {tolerance:.17g};
        omega {tolerance:.17g};
    }}
}}
relaxationFactors
{{
    fields
    {{
        p 0.3;
    }}
    equations
    {{
        U 0.5;
        k 0.7;
        omega 0.7;
    }}
}}
"""
