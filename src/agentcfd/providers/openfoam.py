"""OpenFOAM case lowering and external-process execution.

AgentCFD does not import, link, vendor, or modify OpenFOAM.  This provider
creates ordinary OpenFOAM case files and, when explicitly asked to run, calls
an installation already present on the user's machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import boundaries
from .._version import __version__
from .._validation import integer_at_least, positive_float
from ..errors import ProviderUnavailableError, UnsupportedCaseError
from ..results import Artifact, Check, FieldRecord, History, Quantity, SimulationResult
from .base import ProviderDescriptor


_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _solver_converged(log: str) -> bool:
    """Return true only for an explicit OpenFOAM convergence statement."""

    return bool(re.search(r"(?mi)^.*solution converged in \d+ iterations\s*$", log))


def _read_scalar_series(case_directory: Path, function_name: str) -> dict[float, float]:
    """Read the scalar history written by an OpenFOAM field-value object."""

    root = case_directory / "postProcessing" / function_name
    samples: dict[float, float] = {}
    if not root.is_dir():
        return samples

    def restart_key(path: Path) -> tuple[float, str]:
        try:
            start_time = float(path.relative_to(root).parts[0])
        except (ValueError, IndexError):
            start_time = -math.inf
        return start_time, str(path)

    for path in sorted(root.rglob("*.dat"), key=restart_key):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            columns = stripped.replace("(", " ").replace(")", " ").split()
            if len(columns) < 2:
                continue
            try:
                time = float(columns[0])
                value = float(columns[1])
            except ValueError:
                continue
            if math.isfinite(time) and math.isfinite(value):
                samples[time] = value
    return samples


def _mesh_quality_quantities(log: str) -> dict[str, Quantity]:
    """Recover stable checkMesh observables without depending on line order."""

    patterns = {
        "mesh.cell_count": (r"(?m)^\s*cells:\s+(\d+)\s*$", "1"),
        "mesh.maximum_aspect_ratio": (r"Max aspect ratio =\s*([0-9.eE+-]+)", "1"),
        "mesh.maximum_non_orthogonality": (
            r"Mesh non-orthogonality Max:\s*([0-9.eE+-]+)",
            "deg",
        ),
        "mesh.average_non_orthogonality": (
            r"Mesh non-orthogonality Max:\s*[0-9.eE+-]+\s+average:\s*([0-9.eE+-]+)",
            "deg",
        ),
        "mesh.maximum_skewness": (r"Max skewness =\s*([0-9.eE+-]+)", "1"),
    }
    quantities: dict[str, Quantity] = {}
    for name, (pattern, unit) in patterns.items():
        match = re.search(pattern, log)
        if match is not None:
            quantities[name] = Quantity(float(match.group(1)), unit)
    return quantities


def _runtime_version(logs: dict[str, str], fallback: str) -> str:
    for log in logs.values():
        match = re.search(r"(?m)^\|.*Version:\s*([^ ]+)\s+", log)
        if match is not None:
            return match.group(1)
        match = re.search(r"\bversion=([^\s]+)", log)
        if match is not None:
            return match.group(1)
    return fallback


def _latest_time_directory(case_directory: Path) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for path in case_directory.iterdir():
        if not path.is_dir():
            continue
        try:
            time = float(path.name)
        except ValueError:
            continue
        if time > 0.0:
            candidates.append((time, path))
    return max(candidates, default=(0.0, None), key=lambda item: item[0])[1]


def _recover_patch_data(
    case_directory: Path,
    *,
    density: float,
    reference_pressure_drop: float,
    solver_tolerance: float,
    reference_pressure_drop_per_flow: float | None = None,
    pressure_error_limit: float = 0.02,
    mass_balance_limit: float = 1.0e-6,
    validation_message: str = "Pressure drop is compared with Hagen--Poiseuille at recovered flow.",
) -> tuple[dict[str, Quantity], dict[str, History], tuple[Check, ...], str]:
    series = {
        name: _read_scalar_series(case_directory, name)
        for name in (
            "agentcfd_inlet_flow",
            "agentcfd_outlet_flow",
            "agentcfd_inlet_pressure",
            "agentcfd_outlet_pressure",
        )
    }
    quantities = {
        "reference.flow.pressure_drop": Quantity(reference_pressure_drop, "Pa"),
        "reference.flow.pressure_drop_requested": Quantity(reference_pressure_drop, "Pa"),
    }
    common_times = sorted(set.intersection(*(set(values) for values in series.values())))
    if not common_times:
        return (
            quantities,
            {},
            (
                Check(
                    name="field-output-recovery",
                    passed=False,
                    value="missing",
                    limit="all four patch histories are required",
                    message="OpenFOAM did not write a complete common patch-history sample.",
                    kind="verification",
                    observable="flow.mass_balance_and_pressure_drop",
                ),
            ),
            "OpenFOAM patch result recovery is incomplete.",
        )

    inlet_flow = tuple(-series["agentcfd_inlet_flow"][time] for time in common_times)
    outlet_flow = tuple(series["agentcfd_outlet_flow"][time] for time in common_times)
    pressure_drop = tuple(
        density
        * (
            series["agentcfd_inlet_pressure"][time]
            - series["agentcfd_outlet_pressure"][time]
        )
        for time in common_times
    )
    mass_imbalance = tuple(
        abs(inflow - outflow) / max(abs(inflow), abs(outflow), 1.0e-300)
        for inflow, outflow in zip(inlet_flow, outlet_flow)
    )
    final_inlet_flow = inlet_flow[-1]
    final_outlet_flow = outlet_flow[-1]
    final_pressure_drop = pressure_drop[-1]
    final_mass_imbalance = mass_imbalance[-1]
    reference_from_recovered_flow = reference_pressure_drop
    if reference_pressure_drop_per_flow is not None:
        reference_from_recovered_flow = reference_pressure_drop_per_flow * 0.5 * (
            final_inlet_flow + final_outlet_flow
        )
        quantities["reference.flow.pressure_drop"] = Quantity(
            reference_from_recovered_flow,
            "Pa",
            description="Hagen-Poiseuille pressure drop at the recovered volume flow.",
        )
    reference_is_positive = reference_from_recovered_flow > 0.0
    relative_pressure_error = (
        abs(final_pressure_drop - reference_from_recovered_flow)
        / abs(reference_from_recovered_flow)
        if reference_is_positive
        else 1.0
    )
    pressure_error_limit = positive_float(
        pressure_error_limit,
        name="pressure_error_limit",
    )
    mass_balance_limit = max(
        positive_float(mass_balance_limit, name="mass_balance_limit"),
        10.0 * solver_tolerance,
    )
    quantities.update(
        {
            "flow.inlet_volume_flow_rate": Quantity(final_inlet_flow, "m^3/s"),
            "flow.outlet_volume_flow_rate": Quantity(final_outlet_flow, "m^3/s"),
            "flow.mass_flow_rate": Quantity(
                0.5 * density * (final_inlet_flow + final_outlet_flow),
                "kg/s",
            ),
            "flow.pressure_drop": Quantity(final_pressure_drop, "Pa"),
            "flow.pressure_drop_relative_error": Quantity(relative_pressure_error, "1"),
        }
    )
    iteration = tuple(common_times)
    histories = {
        "flow.inlet_volume_flow_rate": History(
            iteration,
            inlet_flow,
            unit="m^3/s",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
        "flow.outlet_volume_flow_rate": History(
            iteration,
            outlet_flow,
            unit="m^3/s",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
        "flow.pressure_drop": History(
            iteration,
            pressure_drop,
            unit="Pa",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
        "flow.relative_mass_imbalance": History(
            iteration,
            mass_imbalance,
            unit="1",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
    }
    checks = (
        Check(
            name="field-output-recovery",
            passed=True,
            value=float(len(common_times)),
            limit="at least one common patch sample",
            kind="runtime",
            observable="flow.patch_histories",
        ),
        Check(
            name="mass-balance",
            passed=final_mass_imbalance <= mass_balance_limit,
            value=final_mass_imbalance,
            limit=mass_balance_limit,
            kind="verification",
            observable="flow.relative_mass_imbalance",
        ),
        Check(
            name="positive-through-flow",
            passed=(
                final_inlet_flow > 0.0
                and final_outlet_flow > 0.0
                and reference_is_positive
            ),
            value=min(final_inlet_flow, final_outlet_flow),
            limit="> 0 m^3/s at both patches",
            kind="verification",
            observable="flow.volume_flow_rate",
        ),
        Check(
            name="pressure-drop-reference",
            passed=relative_pressure_error <= pressure_error_limit,
            value=relative_pressure_error,
            limit=pressure_error_limit,
            message=validation_message,
            kind="validation",
            observable="flow.pressure_drop_relative_error",
        ),
    )
    return (
        quantities,
        histories,
        checks,
        "OpenFOAM patch flow and pressure histories were recovered automatically.",
    )


@dataclass(frozen=True, slots=True)
class OpenFOAMMeshControls:
    """Provider-specific controls for the first circular-pipe mesh."""

    cross_section_cells: int = 8
    axial_cells: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cross_section_cells",
            integer_at_least(
                self.cross_section_cells,
                name="cross_section_cells",
                minimum=2,
            ),
        )
        if self.axial_cells is not None:
            object.__setattr__(
                self,
                "axial_cells",
                integer_at_least(
                    self.axial_cells,
                    name="axial_cells",
                    minimum=2,
                ),
            )


@dataclass(frozen=True, slots=True)
class OpenFOAMValidationPolicy:
    """Explicit scientific acceptance thresholds for the pipe provider."""

    maximum_relative_mass_imbalance: float = 1.0e-6
    maximum_relative_pressure_error: float = 0.02

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_relative_mass_imbalance",
            positive_float(
                self.maximum_relative_mass_imbalance,
                name="maximum_relative_mass_imbalance",
            ),
        )
        object.__setattr__(
            self,
            "maximum_relative_pressure_error",
            positive_float(
                self.maximum_relative_pressure_error,
                name="maximum_relative_pressure_error",
            ),
        )


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMCase:
    """Content-addressed record of a generated OpenFOAM case."""

    directory: Path
    model_sha256: str
    case_sha256: str
    files: dict[str, str]
    capability: str = "openfoam.steady-laminar-circular-pipe"
    schema: str = "agentcfd.openfoam-case/0.1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "capability": self.capability,
            "model_sha256": self.model_sha256,
            "case_sha256": self.case_sha256,
            "files": dict(sorted(self.files.items())),
            "provider": {
                "name": "openfoam",
                "execution_boundary": "filesystem-and-subprocess",
                "license": "GPL-3.0-or-later (external program)",
            },
        }


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMGridStudy:
    """A geometrically similar three-case OpenFOAM refinement plan."""

    directory: Path
    model_sha256: str
    cases: tuple[dict[str, object], ...]
    refinement_ratio: float
    scientific_inputs: dict[str, object]
    schema: str = "agentcfd.openfoam-grid-study/0.1"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "model_sha256": self.model_sha256,
            "quantity": "flow.pressure_drop",
            "refinement_ratio": self.refinement_ratio,
            "scientific_inputs": self.scientific_inputs,
            "cases": list(self.cases),
        }

    def write(self) -> Path:
        target = self.directory / "agentcfd-grid-study.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


def prepare_pipe_grid_study(
    step,
    directory: str | Path,
    *,
    cross_section_cells: tuple[int, int, int] = (4, 8, 16),
    base_axial_cells: int = 20,
) -> PreparedOpenFOAMGridStudy:
    """Prepare three same-model, geometrically similar circular-pipe cases."""

    selected = tuple(
        integer_at_least(value, name="cross_section_cells", minimum=2)
        for value in cross_section_cells
    )
    if len(selected) != 3:
        raise ValueError("Exactly three cross-section cell counts are required.")
    if not (selected[0] < selected[1] < selected[2]):
        raise ValueError("Cross-section cell counts must be strictly increasing.")
    ratio_21 = selected[1] / selected[0]
    ratio_32 = selected[2] / selected[1]
    if not math.isclose(ratio_21, ratio_32, rel_tol=1.0e-12):
        raise ValueError("Cross-section cell counts must use one refinement ratio.")
    base_axial = integer_at_least(
        base_axial_cells,
        name="base_axial_cells",
        minimum=2,
    )
    axial_counts: list[int] = []
    for cross_cells in selected:
        scaled = base_axial * cross_cells / selected[0]
        if not scaled.is_integer():
            raise ValueError("Axial cell counts must scale integrally with cross-section cells.")
        axial_counts.append(int(scaled))

    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"OpenFOAM grid-study directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    model_sha256 = step.model.fingerprint()
    cases: list[dict[str, object]] = []
    for index, (cross_cells, axial_cells) in enumerate(
        zip(selected, axial_counts),
        start=1,
    ):
        relative = Path(f"grid-{index}-c{cross_cells}-a{axial_cells}")
        prepared = OpenFOAMProvider(
            case_directory=root / relative,
            mesh=OpenFOAMMeshControls(
                cross_section_cells=cross_cells,
                axial_cells=axial_cells,
            ),
        ).prepare(step)
        if prepared.model_sha256 != model_sha256:
            raise RuntimeError("Prepared grid cases do not share one model identity.")
        cases.append(
            {
                "index": index,
                "label": ("coarse", "medium", "fine")[index - 1],
                "directory": str(relative),
                "cross_section_cells": cross_cells,
                "axial_cells": axial_cells,
                "expected_cell_count": 5 * cross_cells**2 * axial_cells,
                "case_sha256": prepared.case_sha256,
                "result": str(relative / "agentcfd-result.json"),
            }
        )
    study = PreparedOpenFOAMGridStudy(
        directory=root,
        model_sha256=model_sha256,
        cases=tuple(cases),
        refinement_ratio=ratio_21,
        scientific_inputs={
            "model": step.model.to_dict(),
            "procedure": step.procedure.to_dict(),
            "output_request": step.output.to_dict(),
        },
    )
    study.write()
    return study


class OpenFOAMProvider:
    """Lower a bounded model to an external ``simpleFoam`` workflow.

    The supported case is intentionally narrow: steady, incompressible,
    isothermal, Newtonian, laminar flow through a smooth circular pipe.  Case
    generation is deterministic and testable without an OpenFOAM installation.
    Execution additionally requires ``blockMesh``, ``checkMesh``, and
    ``simpleFoam`` on PATH.
    """

    def __init__(
        self,
        *,
        case_directory: str | Path | None = None,
        mesh: OpenFOAMMeshControls | None = None,
        validation: OpenFOAMValidationPolicy | None = None,
        timeout_seconds: float = 3600.0,
        container_image: str | None = None,
    ) -> None:
        self.case_directory = Path(case_directory) if case_directory is not None else None
        self.mesh = mesh or OpenFOAMMeshControls()
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

    def _execution_argv(self, name: str, command: str, case_directory: Path) -> list[str]:
        if self.container_image is None:
            return [command, "-case", str(case_directory)]
        return [
            command,
            "run",
            "--rm",
            "-v",
            f"{case_directory.resolve()}:/case",
            "-w",
            "/case",
            self.container_image,
            name,
            "-case",
            "/case",
        ]

    def descriptor(self) -> ProviderDescriptor:
        commands = self._commands()
        available = all(commands.values())
        version = self.container_image or os.environ.get(
            "WM_PROJECT_VERSION", "externally-managed"
        )
        return ProviderDescriptor(
            name="openfoam",
            version=version,
            license="GPL-3.0-or-later (external program)",
            available=available,
            execution_boundary=(
                "filesystem-and-container-subprocess"
                if self.container_image
                else "filesystem-and-subprocess"
            ),
            capabilities=("openfoam.steady-laminar-circular-pipe",),
        )

    def prepare(self, step, directory: str | Path | None = None) -> PreparedOpenFOAMCase:
        """Validate and write a deterministic OpenFOAM case.

        The destination must not already contain files.  AgentCFD never deletes
        or silently overwrites an existing CFD case.
        """

        step.model.validate()
        self._validate_supported(step)
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM case_directory is required for prepare or run.")
        if target.exists() and any(target.iterdir()):
            raise FileExistsError(f"OpenFOAM case directory is not empty: {target}")
        rendered = self._render_files(step)
        target.mkdir(parents=True, exist_ok=True)

        hashes: dict[str, str] = {}
        for relative, content in sorted(rendered.items()):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            hashes[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        case_identity = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prepared = PreparedOpenFOAMCase(
            directory=target,
            model_sha256=step.model.fingerprint(),
            case_sha256=case_identity,
            files=hashes,
        )
        manifest = target / "agentcfd-case.json"
        manifest.write_text(
            json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return prepared

    def run(self, step) -> SimulationResult:
        """Prepare and execute the case through external OpenFOAM commands.

        A successful process is reported as completed execution evidence.
        Numerical convergence additionally requires OpenFOAM's explicit
        convergence marker. Patch flow and pressure histories are recovered
        independently so scientific checks never rely on log wording alone.
        """

        prepared = self.prepare(step)
        commands = self._commands()
        missing = [name for name, path in commands.items() if path is None]
        if missing:
            if self.container_image is not None:
                raise ProviderUnavailableError(
                    "OpenFOAM container execution requires docker on PATH."
                )
            raise ProviderUnavailableError(
                "OpenFOAM execution requires commands on PATH: " + ", ".join(missing)
            )

        logs: dict[str, str] = {}
        return_codes: dict[str, int] = {}
        for name in ("blockMesh", "checkMesh", "simpleFoam"):
            try:
                completed = subprocess.run(
                    self._execution_argv(
                        name,
                        str(commands[name]),
                        prepared.directory,
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
                return_codes[name] = -124
            logs[name] = combined
            (prepared.directory / f"log.{name}").write_text(combined, encoding="utf-8")
            if return_codes[name] != 0:
                break

        process_ok = all(return_codes.get(name) == 0 for name in commands)
        solver_log = logs.get("simpleFoam", "")
        reached_end = process_ok and bool(re.search(r"(?m)^End\s*$", solver_log))
        solver_converged = process_ok and _solver_converged(solver_log)
        reference_drop = self._reference_pressure_drop(step)
        quantities, histories, recovery_checks, recovery_message = _recover_patch_data(
            prepared.directory,
            density=step.model.fluid.density,
            reference_pressure_drop=reference_drop,
            solver_tolerance=step.procedure.relative_tolerance,
            reference_pressure_drop_per_flow=(
                128.0
                * step.model.fluid.dynamic_viscosity
                * step.model.domain.length
                / (math.pi * step.model.domain.diameter**4)
            ),
            pressure_error_limit=self.validation.maximum_relative_pressure_error,
            mass_balance_limit=self.validation.maximum_relative_mass_imbalance,
            validation_message=(
                "The fully developed profile isolates spatial and outlet-boundary error."
                if any(
                    isinstance(value, boundaries.FullyDevelopedVelocityInlet)
                    for value in step.model.boundary_conditions.values()
                )
                else "The uniform inlet includes developing-flow effects in the total pressure drop."
            ),
        )
        quantities.update(_mesh_quality_quantities(logs.get("checkMesh", "")))
        artifact_paths = {
            "case_manifest": prepared.directory / "agentcfd-case.json",
            **{
                f"log_{name}": prepared.directory / f"log.{name}"
                for name in logs
            },
        }
        fields: dict[str, FieldRecord] = {}
        latest_time = _latest_time_directory(prepared.directory)
        if latest_time is not None:
            for name, unit in (("U", "m/s"), ("p", "m^2/s^2")):
                path = latest_time / name
                if path.is_file():
                    fields[name] = FieldRecord(
                        location="cell",
                        unit=unit,
                        artifact=str(path),
                        components=("x", "y", "z") if name == "U" else (),
                    )
                    artifact_paths[f"field_{name}"] = path
        descriptor = self.descriptor()
        return SimulationResult(
            status="completed" if process_ok else "failed",
            converged=solver_converged,
            provider="openfoam",
            quantities=quantities,
            histories=histories,
            fields=fields,
            checks=(
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
                    limit="OpenFOAM log ends with End",
                    kind="runtime",
                    observable="provider.convergence_marker",
                ),
                Check(
                    name="solver-convergence-marker",
                    passed=solver_converged,
                    value="found" if solver_converged else "missing",
                    limit="OpenFOAM reports solution converged",
                    message=(
                        "A normal End marker proves process completion, not numerical "
                        "convergence; reaching the configured iteration limit remains "
                        "unconverged."
                    ),
                    kind="verification",
                    observable="provider.numerical_convergence",
                ),
                Check(
                    name="mesh-quality",
                    passed=(
                        return_codes.get("checkMesh") == 0
                        and "Mesh OK" in logs.get("checkMesh", "")
                    ),
                    value=(
                        "Mesh OK"
                        if "Mesh OK" in logs.get("checkMesh", "")
                        else "not confirmed"
                    ),
                    limit="checkMesh succeeds and reports Mesh OK",
                    kind="verification",
                    observable="mesh.quality",
                ),
                *recovery_checks,
            ),
            artifacts={
                name: Artifact.from_path(path, role="execution-evidence", media_type="text/plain")
                for name, path in artifact_paths.items()
            },
            scientific_inputs={
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
                "mesh_controls": asdict(self.mesh),
                "validation_policy": asdict(self.validation),
                "lowered_case_sha256": prepared.case_sha256,
            },
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": step.model.fingerprint(),
                "case_sha256": prepared.case_sha256,
                "provider": "openfoam",
                "provider_version": _runtime_version(
                    logs,
                    descriptor.version,
                ),
                "execution_boundary": descriptor.execution_boundary,
                "container_image": self.container_image,
                "case_manifest": str(prepared.directory / "agentcfd-case.json"),
            },
            messages=(recovery_message,),
        )

    def _validate_supported(self, step) -> None:
        model = step.model
        study = model.study
        if not study.steady or study.compressible or study.energy or study.reacting or not study.laminar:
            raise UnsupportedCaseError(
                "The initial OpenFOAM provider supports steady, incompressible, "
                "isothermal, Newtonian, laminar internal flow only."
            )
        if model.domain.roughness != 0.0:
            raise UnsupportedCaseError("The initial OpenFOAM provider requires a smooth pipe.")
        wall_conditions = [
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.NoSlipWall)
        ]
        if any(condition.roughness not in (None, 0.0) for condition in wall_conditions):
            raise UnsupportedCaseError("Rough-wall lowering is not implemented.")
        if len(wall_conditions) != 1:
            raise UnsupportedCaseError("The initial OpenFOAM pipe mesh requires exactly one wall boundary.")
        reynolds = (
            model.fluid.density
            * self._mean_velocity(model)
            * model.domain.diameter
            / model.fluid.dynamic_viscosity
        )
        if reynolds >= 2300.0:
            raise UnsupportedCaseError(
                f"Re={reynolds:.6g} is outside the declared laminar provider range Re < 2300."
            )
        for name in model.boundary_conditions:
            if not _FOAM_WORD.fullmatch(name):
                raise UnsupportedCaseError(
                    f"Boundary name {name!r} is not a valid OpenFOAM word; use letters, digits, and underscores."
                )

    def _render_files(self, step) -> dict[str, str]:
        model = step.model
        inlet_name, inlet = next(
            (name, value)
            for name, value in model.boundary_conditions.items()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                ),
            )
        )
        outlet_name, outlet = next(
            (name, value)
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.PressureOutlet)
        )
        wall_names = tuple(
            name
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.NoSlipWall)
        )
        rho = model.fluid.density
        mean_velocity = self._mean_velocity(model)
        outlet_kinematic_pressure = outlet.gauge_pressure / rho

        axial_cells = self.mesh.axial_cells
        if axial_cells is None:
            axial_cells = max(20, min(800, math.ceil(2.0 * model.domain.length / model.domain.diameter)))

        return {
            "0/U": _velocity_field(
                inlet_name,
                outlet_name,
                wall_names,
                mean_velocity,
                radius=model.domain.diameter / 2.0,
                fully_developed=isinstance(inlet, boundaries.FullyDevelopedVelocityInlet),
            ),
            "0/p": _pressure_field(inlet_name, outlet_name, wall_names, outlet_kinematic_pressure),
            "constant/transportProperties": _transport_properties(model.fluid.kinematic_viscosity),
            "constant/turbulenceProperties": _turbulence_properties(),
            "system/blockMeshDict": _block_mesh_dict(
                length=model.domain.length,
                radius=model.domain.diameter / 2.0,
                cross_cells=self.mesh.cross_section_cells,
                axial_cells=axial_cells,
                inlet=inlet_name,
                outlet=outlet_name,
                walls=wall_names,
            ),
            "system/controlDict": _control_dict(
                step.procedure.maximum_iterations,
                inlet=inlet_name,
                outlet=outlet_name,
            ),
            "system/fvSchemes": _fv_schemes(),
            "system/fvSolution": _fv_solution(step.procedure.relative_tolerance),
        }

    @staticmethod
    def _reference_pressure_drop(step) -> float:
        model = step.model
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                ),
            )
        )
        if isinstance(inlet, boundaries.MassFlowInlet):
            velocity = inlet.mass_flow_rate / (model.fluid.density * model.domain.area)
        else:
            velocity = inlet.velocity
        return (
            32.0
            * model.fluid.dynamic_viscosity
            * model.domain.length
            * velocity
            / model.domain.diameter**2
        )

    @staticmethod
    def _mean_velocity(model) -> float:
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                ),
            )
        )
        if isinstance(inlet, boundaries.MassFlowInlet):
            return inlet.mass_flow_rate / (model.fluid.density * model.domain.area)
        return inlet.velocity


def _header(*, object_name: str, class_name: str, location: str | None = None) -> str:
    location_line = f'    location    "{location}";\n' if location is not None else ""
    return (
        "/* Generated by AgentCFD. OpenFOAM is an external GPL program. */\n"
        "FoamFile\n{\n"
        "    version     2.0;\n"
        "    format      ascii;\n"
        f"    class       {class_name};\n"
        f"{location_line}"
        f"    object      {object_name};\n"
        "}\n\n"
    )


def _velocity_field(
    inlet: str,
    outlet: str,
    walls: tuple[str, ...],
    velocity: float,
    *,
    radius: float,
    fully_developed: bool,
) -> str:
    wall_blocks = "\n".join(
        f"    {name}\n    {{\n        type noSlip;\n    }}" for name in walls
    )
    if fully_developed:
        inlet_block = f"""        type uniformFixedValue;
        value uniform (0 0 {velocity:.17g});
        uniformValue
        {{
            type expression;
            expression "vector(0, 0, ({2.0 * velocity:.17g})*(1 - (sqr(pos().x()) + sqr(pos().y()))/{radius**2:.17g}))";
        }}"""
    else:
        inlet_block = f"""        type fixedValue;
        value uniform (0 0 {velocity:.17g});"""
    return _header(object_name="U", class_name="volVectorField", location="0") + f"""dimensions      [0 1 -1 0 0 0 0];
internalField   uniform (0 0 {velocity:.17g});
boundaryField
{{
    {inlet}
    {{
{inlet_block}
    }}
    {outlet}
    {{
        type zeroGradient;
    }}
{wall_blocks}
}}
"""


def _pressure_field(inlet: str, outlet: str, walls: tuple[str, ...], pressure: float) -> str:
    wall_blocks = "\n".join(
        f"    {name}\n    {{\n        type zeroGradient;\n    }}" for name in walls
    )
    return _header(object_name="p", class_name="volScalarField", location="0") + f"""dimensions      [0 2 -2 0 0 0 0];
internalField   uniform {pressure:.17g};
boundaryField
{{
    {inlet}
    {{
        type zeroGradient;
    }}
    {outlet}
    {{
        type fixedValue;
        value uniform {pressure:.17g};
    }}
{wall_blocks}
}}
"""


def _transport_properties(nu: float) -> str:
    return _header(object_name="transportProperties", class_name="dictionary", location="constant") + f"""transportModel Newtonian;
nu              [0 2 -1 0 0 0 0] {nu:.17g};
"""


def _turbulence_properties() -> str:
    return _header(object_name="turbulenceProperties", class_name="dictionary", location="constant") + """simulationType laminar;
"""


def _block_mesh_dict(
    *,
    length: float,
    radius: float,
    cross_cells: int,
    axial_cells: int,
    inlet: str,
    outlet: str,
    walls: tuple[str, ...],
) -> str:
    inner = radius / 3.0
    wall_name = walls[0]
    if len(walls) != 1:
        raise UnsupportedCaseError("The initial OpenFOAM pipe mesh requires exactly one wall boundary.")
    return _header(object_name="blockMeshDict", class_name="dictionary", location="system") + f"""convertToMeters 1;

geometry
{{
    pipeCylinder
    {{
        type cylinder;
        point1 (0 0 0);
        point2 (0 0 {length:.17g});
        radius {radius:.17g};
    }}
}}

vertices
(
    ({-inner:.17g} {-inner:.17g} 0)
    ({inner:.17g} {-inner:.17g} 0)
    ({-inner:.17g} {inner:.17g} 0)
    ({inner:.17g} {inner:.17g} 0)
    project ({-radius:.17g} {-radius:.17g} 0) (pipeCylinder)
    project ({radius:.17g} {-radius:.17g} 0) (pipeCylinder)
    project ({-radius:.17g} {radius:.17g} 0) (pipeCylinder)
    project ({radius:.17g} {radius:.17g} 0) (pipeCylinder)
    ({-inner:.17g} {-inner:.17g} {length:.17g})
    ({inner:.17g} {-inner:.17g} {length:.17g})
    ({-inner:.17g} {inner:.17g} {length:.17g})
    ({inner:.17g} {inner:.17g} {length:.17g})
    project ({-radius:.17g} {-radius:.17g} {length:.17g}) (pipeCylinder)
    project ({radius:.17g} {-radius:.17g} {length:.17g}) (pipeCylinder)
    project ({-radius:.17g} {radius:.17g} {length:.17g}) (pipeCylinder)
    project ({radius:.17g} {radius:.17g} {length:.17g}) (pipeCylinder)
);

blocks
(
    hex (4 5 1 0 12 13 9 8) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (4 0 2 6 12 8 10 14) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (1 5 7 3 9 13 15 11) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (2 3 7 6 10 11 15 14) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (0 1 3 2 8 9 11 10) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
);

edges
(
    arc 4 5 (0 {-radius:.17g} 0)
    arc 7 5 ({radius:.17g} 0 0)
    arc 6 7 (0 {radius:.17g} 0)
    arc 4 6 ({-radius:.17g} 0 0)
    arc 12 13 (0 {-radius:.17g} {length:.17g})
    arc 13 15 ({radius:.17g} 0 {length:.17g})
    arc 12 14 ({-radius:.17g} 0 {length:.17g})
    arc 14 15 (0 {radius:.17g} {length:.17g})
);

boundary
(
    {inlet}
    {{
        type patch;
        faces
        (
            (4 0 1 5)
            (4 6 2 0)
            (1 3 7 5)
            (2 6 7 3)
            (0 2 3 1)
        );
    }}
    {outlet}
    {{
        type patch;
        faces
        (
            (12 13 9 8)
            (12 8 10 14)
            (9 13 15 11)
            (10 11 15 14)
            (8 9 11 10)
        );
    }}
    {wall_name}
    {{
        type wall;
        faces
        (
            (4 5 13 12)
            (4 12 14 6)
            (5 7 15 13)
            (6 14 15 7)
        );
    }}
);
"""


def _control_dict(maximum_iterations: int, *, inlet: str, outlet: str) -> str:
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
    agentcfd_inlet_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {inlet};
        operation sum;
        fields (phi);
    }}
    agentcfd_outlet_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {outlet};
        operation sum;
        fields (phi);
    }}
    agentcfd_inlet_pressure
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {inlet};
        operation areaAverage;
        fields (p);
    }}
    agentcfd_outlet_pressure
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {outlet};
        operation areaAverage;
        fields (p);
    }}
}}
"""


def _fv_schemes() -> str:
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


def _fv_solution(relative_tolerance: float) -> str:
    return _header(object_name="fvSolution", class_name="dictionary", location="system") + f"""solvers
{{
    p
    {{
        solver GAMG;
        tolerance {relative_tolerance:.17g};
        relTol 0.01;
        smoother GaussSeidel;
    }}
    U
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance {relative_tolerance:.17g};
        relTol 0.1;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors 0;
    consistent yes;
    residualControl
    {{
        p {relative_tolerance:.17g};
        U {relative_tolerance:.17g};
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
        U 0.7;
    }}
}}
"""
