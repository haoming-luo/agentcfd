"""One readable project lifecycle shared by people, agents, CLIs, and GUIs."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import sys
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Mapping

from . import boundaries, data_exchange, engineering
from .errors import ModelValidationError, ProjectError, UnsupportedCaseError
from .geometry import CircularPipe, RectangularChannel
from .model import Step
from .provenance import content_fingerprint, file_sha256
from .providers import (
    OpenFOAMChannelProvider,
    OpenFOAMMeshControls,
    OpenFOAMProvider,
    ReferencePipeProvider,
)
from .results import Artifact, FieldRecord, SimulationResult


@dataclass(frozen=True, slots=True)
class ProjectIssue:
    code: str
    severity: str
    message: str
    path: str
    repair: str

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning", "info"}:
            raise ValueError(f"Unknown project issue severity {self.severity!r}.")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    entrypoint: str
    factory: str
    default_provider: str
    run_directory: str
    run_mode: str
    openfoam: Mapping[str, object]

    @classmethod
    def read(cls, path: Path) -> "ProjectManifest":
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ProjectError(f"Cannot read AgentCFD project manifest {path}: {error}") from error
        allowed = {
            "schema",
            "entrypoint",
            "factory",
            "default_provider",
            "run_directory",
            "run_mode",
            "openfoam",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ProjectError(f"Unknown agentcfd.toml keys: {', '.join(unknown)}")
        if payload.get("schema") != "agentcfd.project/0.1":
            raise ProjectError("Unsupported AgentCFD project schema.")
        strings = {
            name: payload.get(name)
            for name in ("entrypoint", "factory", "default_provider", "run_directory")
        }
        if any(not isinstance(value, str) or not value.strip() for value in strings.values()):
            raise ProjectError("Project entrypoint, factory, provider, and run directory are required strings.")
        provider = str(strings["default_provider"]).strip()
        if provider not in {"reference", "openfoam"}:
            raise ProjectError("Project default_provider must be 'reference' or 'openfoam'.")
        openfoam = payload.get("openfoam", {})
        if not isinstance(openfoam, dict):
            raise ProjectError("Project [openfoam] settings must be a table.")
        run_mode = payload.get("run_mode", "campaign")
        if run_mode not in {"replace", "campaign"}:
            raise ProjectError("Project run_mode must be 'replace' or 'campaign'.")
        return cls(
            entrypoint=str(strings["entrypoint"]).strip(),
            factory=str(strings["factory"]).strip(),
            default_provider=provider,
            run_directory=str(strings["run_directory"]).strip(),
            run_mode=str(run_mode),
            openfoam=dict(openfoam),
        )


@dataclass(frozen=True, slots=True)
class ProjectRun:
    run_id: str
    directory: Path
    result: SimulationResult
    result_path: Path
    plan_path: Path
    field_bundle: data_exchange.FieldBundle | None = None
    mode: str = "replace"
    solver_workspace: Path | None = None

    def to_dict(self) -> dict[str, object]:
        failed_checks = [check.name for check in self.result.checks if not check.passed]
        return {
            "schema": "agentcfd.project-run/0.1",
            "run_id": self.run_id,
            "mode": self.mode,
            "directory": str(self.directory),
            "result": str(self.result_path),
            "plan": str(self.plan_path),
            "status": self.result.status,
            "converged": self.result.converged,
            "accepted": self.result.accepted,
            "trust_level": self.result.trust_level,
            "provider": self.result.provider,
            "failed_check_count": len(failed_checks),
            "failed_checks": failed_checks,
            "field_bundle": (
                self.field_bundle.to_dict() if self.field_bundle is not None else None
            ),
            "solver_workspace": (
                None if self.solver_workspace is None else str(self.solver_workspace)
            ),
        }


def _safe_project_path(root: Path, relative: str, *, label: str) -> Path:
    selected = Path(relative)
    if selected.is_absolute():
        raise ProjectError(f"Project {label} must be relative to the project root.")
    target = (root / selected).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as error:
        raise ProjectError(f"Project {label} escapes the project root.") from error
    return target


def _load_module(path: Path, root: Path) -> ModuleType:
    name = f"_agentcfd_case_{file_sha256(path)[:16]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ProjectError(f"Cannot load project entrypoint {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))
    previous_bytecode_policy = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise ProjectError(f"Project entrypoint failed to import: {error}") from error
    finally:
        sys.dont_write_bytecode = previous_bytecode_policy
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
    return module


def _inlet_reynolds(step: Step) -> float | None:
    inlet = next(
        (
            condition
            for condition in step.model.boundary_conditions.values()
            if isinstance(
                condition,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                    boundaries.TurbulentMeanVelocityInlet,
                ),
            )
        ),
        None,
    )
    if inlet is None:
        return None
    if isinstance(inlet, boundaries.MassFlowInlet):
        velocity = inlet.mass_flow_rate / (step.model.fluid.density * step.model.domain.area)
    else:
        velocity = inlet.velocity
    return engineering.reynolds_number(
        density=step.model.fluid.density,
        mean_velocity=velocity,
        hydraulic_diameter=step.model.domain.hydraulic_diameter,
        dynamic_viscosity=step.model.fluid.dynamic_viscosity,
    )


def _human_bytes(value: int) -> str:
    for unit, divisor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= divisor:
            return f"{value / divisor:.2f} {unit}"
    return f"{value} B"


def _tree_usage(path: Path) -> tuple[int, int]:
    """Return logical file bytes and count without following symlinks."""

    if not path.exists():
        return 0, 0
    if path.is_symlink():
        return path.lstat().st_size, 1
    if path.is_file():
        return path.stat().st_size, 1
    total = 0
    count = 0
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    total += entry.stat(follow_symlinks=False).st_size
                    count += 1
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    count += 1
            except OSError:
                continue
    return total, count


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


_OPENFOAM_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def _human_duration(seconds: float) -> str:
    """Format an approximate elapsed time without pretending to millisecond precision."""

    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _read_text_tail(path: Path, *, maximum_bytes: int = 256 * 1024) -> tuple[str, int]:
    """Read a bounded, race-tolerant log tail while an external solver writes it."""

    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - maximum_bytes)
            stream.seek(start)
            payload = stream.read(maximum_bytes)
    except OSError:
        return "", 0
    if start:
        newline = payload.find(b"\n")
        if newline >= 0:
            payload = payload[newline + 1 :]
    return payload.decode("utf-8", errors="replace"), len(payload)


def _latest_numeric_row(path: Path) -> tuple[list[float] | None, int]:
    """Read the newest complete row from a compact OpenFOAM monitor table."""

    tail, bytes_read = _read_text_tail(path, maximum_bytes=64 * 1024)
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            values = [float(value) for value in stripped.replace("(", " ").replace(")", " ").split()]
        except ValueError:
            continue
        if len(values) >= 2 and all(math.isfinite(value) for value in values):
            return values, bytes_read
    return None, bytes_read


def _run_progress_snapshot(
    root: Path,
    record: Mapping[str, object] | None,
    plan: Mapping[str, object],
    *,
    include_storage: bool,
) -> dict[str, object] | None:
    """Observe an active or recoverable run without opening solver field files."""

    if record is None or record.get("status") not in {
        "preparing",
        "running",
        "exporting",
        "failed",
    }:
        return None
    run_id = record.get("run_id")
    workspace = (
        root / ".agentcfd" / "work" / str(run_id)
        if isinstance(run_id, str) and run_id
        else None
    )
    case_directory = None if workspace is None else workspace / "openfoam"
    log_paths = (
        sorted(
            case_directory.glob("log.*"),
            key=lambda path: path.stat().st_mtime,
        )
        if case_directory is not None and case_directory.is_dir()
        else []
    )
    current_log = log_paths[-1] if log_paths else None
    current_command = None if current_log is None else current_log.name.removeprefix("log.")
    if record.get("status") == "exporting":
        current_command = "portable-field-export"
    tail, tail_bytes = (
        _read_text_tail(current_log) if current_log is not None else ("", 0)
    )

    native_coordinates: list[float] = []
    updated_timestamps: list[float] = []
    if case_directory is not None and case_directory.is_dir():
        try:
            entries = tuple(case_directory.iterdir())
        except OSError:
            entries = ()
        for path in entries:
            if not path.is_dir():
                continue
            try:
                coordinate = float(path.name)
            except ValueError:
                continue
            if coordinate >= 0.0 and math.isfinite(coordinate):
                native_coordinates.append(coordinate)
                try:
                    updated_timestamps.append(path.stat().st_mtime)
                except OSError:
                    pass
    for path in log_paths:
        try:
            updated_timestamps.append(path.stat().st_mtime)
        except OSError:
            pass

    log_coordinates = [
        float(match.group(1))
        for match in re.finditer(
            rf"(?m)^Time\s*=\s*({_OPENFOAM_NUMBER})\s*$",
            tail,
        )
    ]
    current_coordinate = (
        log_coordinates[-1]
        if log_coordinates
        else max(native_coordinates)
        if native_coordinates
        else None
    )
    decisions = plan.get("decisions", {})
    procedure = decisions.get("procedure", {}) if isinstance(decisions, dict) else {}
    procedure_type = procedure.get("type") if isinstance(procedure, dict) else None
    if procedure_type == "transient":
        coordinate_name = "physical_time"
        coordinate_unit = "s"
        target_coordinate = procedure.get("end_time")
    elif procedure_type == "steady":
        coordinate_name = "solver_iteration"
        coordinate_unit = "1"
        target_coordinate = procedure.get("maximum_iterations")
    else:
        coordinate_name = None
        coordinate_unit = None
        target_coordinate = None
    fraction = None
    if (
        isinstance(current_coordinate, (int, float))
        and isinstance(target_coordinate, (int, float))
        and target_coordinate > 0
    ):
        fraction = min(1.0, max(0.0, current_coordinate / target_coordinate))

    residuals: dict[str, dict[str, float | int]] = {}
    residual_pattern = re.compile(
        rf"Solving for\s+([^,]+),\s+Initial residual\s*=\s*({_OPENFOAM_NUMBER}),\s+"
        rf"Final residual\s*=\s*({_OPENFOAM_NUMBER}),\s+No Iterations\s+(\d+)"
    )
    for match in residual_pattern.finditer(tail):
        residuals[match.group(1).strip()] = {
            "initial": float(match.group(2)),
            "final": float(match.group(3)),
            "linear_iterations": int(match.group(4)),
        }

    courant_matches = list(
        re.finditer(
            rf"Courant Number mean:\s*({_OPENFOAM_NUMBER})\s+max:\s*({_OPENFOAM_NUMBER})",
            tail,
        )
    )
    courant = (
        {
            "mean": float(courant_matches[-1].group(1)),
            "maximum": float(courant_matches[-1].group(2)),
        }
        if courant_matches
        else None
    )

    monitor_bytes = 0
    monitor_values: dict[str, float] = {}
    if case_directory is not None:
        for name in (
            "agentcfd_inlet_flow",
            "agentcfd_outlet_flow",
            "agentcfd_inlet_pressure",
            "agentcfd_outlet_pressure",
        ):
            candidates = sorted(
                (case_directory / "postProcessing" / name).glob("*/*.dat"),
                key=lambda path: path.stat().st_mtime,
            )
            if not candidates:
                continue
            row, bytes_read = _latest_numeric_row(candidates[-1])
            monitor_bytes += bytes_read
            if row is not None:
                monitor_values[name] = row[-1]
                try:
                    updated_timestamps.append(candidates[-1].stat().st_mtime)
                except OSError:
                    pass
    inlet_flow = monitor_values.get("agentcfd_inlet_flow")
    outlet_flow = monitor_values.get("agentcfd_outlet_flow")
    relative_mass_imbalance = None
    if inlet_flow is not None and outlet_flow is not None:
        scale = max(abs(inlet_flow), abs(outlet_flow))
        relative_mass_imbalance = (
            0.0 if scale == 0.0 else abs(inlet_flow + outlet_flow) / scale
        )
    inlet_pressure = monitor_values.get("agentcfd_inlet_pressure")
    outlet_pressure = monitor_values.get("agentcfd_outlet_pressure")
    pressure_drop = None
    model = plan.get("model", {})
    summary = model.get("summary", {}) if isinstance(model, dict) else {}
    fluid = summary.get("fluid", {}) if isinstance(summary, dict) else {}
    density = fluid.get("density") if isinstance(fluid, dict) else None
    if (
        inlet_pressure is not None
        and outlet_pressure is not None
        and isinstance(density, (int, float))
    ):
        pressure_drop = (inlet_pressure - outlet_pressure) * density

    now = datetime.now(UTC)
    started_at = record.get("started_at")
    completed_at = record.get("completed_at")
    try:
        start = datetime.fromisoformat(str(started_at))
        end = datetime.fromisoformat(str(completed_at)) if completed_at else now
        elapsed_seconds = max(0.0, (end - start).total_seconds())
    except (TypeError, ValueError):
        elapsed_seconds = None

    remaining_range = None
    if (
        procedure_type == "transient"
        and isinstance(fraction, float)
        and 0.01 <= fraction < 1.0
        and isinstance(elapsed_seconds, float)
    ):
        linear_remaining = elapsed_seconds * (1.0 - fraction) / fraction
        remaining_range = {
            "minimum_seconds": round(linear_remaining * 0.5, 1),
            "maximum_seconds": round(linear_remaining * 2.0, 1),
            "basis": "wide linear extrapolation; meshing and export excluded",
        }

    workspace_bytes = None
    workspace_files = None
    if include_storage and workspace is not None:
        workspace_bytes, workspace_files = _tree_usage(workspace)
    observed_at = now.isoformat()
    updated_at = (
        datetime.fromtimestamp(max(updated_timestamps), tz=UTC).isoformat()
        if updated_timestamps
        else None
    )
    return {
        "schema": "agentcfd.run-progress/0.1",
        "run_id": run_id,
        "status": record.get("status"),
        "phase": record.get("phase", record.get("status")),
        "current_command": current_command,
        "observed_at": observed_at,
        "updated_at": updated_at,
        "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 3),
        "elapsed_display": (
            None if elapsed_seconds is None else _human_duration(elapsed_seconds)
        ),
        "coordinate": {
            "name": coordinate_name,
            "unit": coordinate_unit,
            "current": current_coordinate,
            "target": target_coordinate,
            "fraction": fraction,
        },
        "latest_residuals": residuals,
        "courant_number": courant,
        "monitors": {
            "inlet_volume_flow": inlet_flow,
            "outlet_volume_flow": outlet_flow,
            "relative_mass_imbalance": relative_mass_imbalance,
            "pressure_drop": pressure_drop,
            "units": {
                "volume_flow": "m^3/s",
                "relative_mass_imbalance": "1",
                "pressure_drop": "Pa",
            },
        },
        "estimated_remaining": remaining_range,
        "workspace": {
            "path": None if workspace is None else str(workspace),
            "exists": workspace is not None and workspace.exists(),
            "native_time_directory_count": len(native_coordinates),
            "bytes": workspace_bytes,
            "display": None if workspace_bytes is None else _human_bytes(workspace_bytes),
            "file_count": workspace_files,
        },
        "observation_cost": {
            "field_payloads_opened": 0,
            "log_tail_bytes_read": tail_bytes,
            "monitor_bytes_read": monitor_bytes,
            "recursive_storage_scan": include_storage,
        },
    }


def _write_output_guide(run: ProjectRun, *, model_name: str) -> Path:
    """Make the result directory understandable without OpenFOAM knowledge."""

    result = run.result
    lines = [
        f"# {model_name} result",
        "",
        f"- Status: `{result.status}`",
        f"- Accepted: `{str(result.accepted).lower()}`",
        f"- Trust level: `{result.trust_level}`",
        f"- Provider: `{result.provider}`",
        "",
        "## Start here",
        "",
    ]
    if run.field_bundle is not None:
        lines.extend(
            (
                "Open `fields/fields.xdmf` in ParaView for mesh and field animation.",
                "The adjacent compressed HDF5 file is its payload; keep both together.",
                "",
            )
        )
    lines.extend(
        (
            "Read `result.json` for quantities, checks, histories, provenance, and artifacts.",
            "Read `plan.json` for the resolved modeling and output decisions.",
            "Read `run.json` for lifecycle state and machine automation.",
            "",
            "Generated OpenFOAM dictionaries are intentionally not retained here. "
            "Edit the project's `case.py` and rerun instead.",
            "",
        )
    )
    if not result.accepted:
        failed = [check.name for check in result.checks if not check.passed]
        lines.extend(
            (
                "## Review required",
                "",
                "This result is not accepted. Failed checks: "
                + (", ".join(failed) if failed else "see result.json"),
                "",
            )
        )
    target = run.directory / "README.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _field_bundle_summary(xdmf_path: Path) -> dict[str, object] | None:
    """Read only the compact manifest, never the HDF5 field payload."""

    manifest_path = xdmf_path.parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        axis = manifest["axis"]
        values = axis["values"]
        fields = manifest["fields"]
        selection = manifest["output_selection"]
        storage = manifest["storage"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(values, list) or not isinstance(fields, list):
        return None
    return {
        "frame_count": len(values),
        "axis": {
            "name": axis.get("name"),
            "unit": axis.get("unit"),
            "physical_time": axis.get("physical_time"),
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
        },
        "profile": selection.get("profile"),
        "fields": [
            {
                "name": field.get("name"),
                "export_name": field.get("export_name"),
                "association": field.get("association"),
                "unit": field.get("unit"),
                "components": field.get("components", []),
            }
            for field in fields
            if isinstance(field, dict)
        ],
        "portable_bytes": storage.get("actual_portable_bytes"),
        "portable_display": (
            _human_bytes(storage["actual_portable_bytes"])
            if isinstance(storage.get("actual_portable_bytes"), int)
            else None
        ),
    }


def _resolved_output_plan(
    step: Step,
    *,
    provider: str,
    openfoam: Mapping[str, object],
) -> dict[str, object]:
    """Resolve user output intent into inspectable counts and a conservative budget."""

    frames = step.output.frames
    if frames.mode == "final":
        requested_frames = 1
    else:
        if step.model.study.steady:
            extent = float(step.procedure.maximum_iterations)
            expected_coordinate = "solver-iteration"
        else:
            extent = step.procedure.end_time
            expected_coordinate = "physical-time"
        if frames.coordinate != expected_coordinate:
            raise ModelValidationError(
                f"Field frame coordinate {frames.coordinate!r} does not match the "
                f"{expected_coordinate!r} procedure coordinate."
            )
        assert frames.every is not None
        requested_frames = math.floor(extent / frames.every + 1.0e-12)
        if frames.include_initial:
            requested_frames += 1
        if frames.include_final and not math.isclose(
            extent / frames.every,
            round(extent / frames.every),
            rel_tol=0.0,
            abs_tol=1.0e-10,
        ):
            requested_frames += 1
        requested_frames = max(1, requested_frames)
    if requested_frames > frames.maximum:
        raise ModelValidationError(
            f"Output interval requests {requested_frames} full-field frames, above "
            f"maximum_frames={frames.maximum}. Increase the interval or the explicit cap."
        )

    estimated_cells: int | None = None
    if provider == "openfoam" and isinstance(step.model.domain, CircularPipe):
        cross = int(openfoam.get("cross_section_cells", 8))
        axial_setting = openfoam.get("axial_cells")
        axial = (
            int(axial_setting)
            if axial_setting is not None
            else max(
                20,
                min(
                    800,
                    math.ceil(
                        2.0 * step.model.domain.length / step.model.domain.diameter
                    ),
                ),
            )
        )
        estimated_cells = 5 * cross * cross * axial
    elif (
        provider == "openfoam"
        and isinstance(step.model.domain, RectangularChannel)
        and step.mesh is not None
        and step.mesh.method == "structured"
        and len(step.model.domain.baffles) == 1
    ):
        domain = step.model.domain
        baffle = domain.baffles[0]
        size = step.mesh.base_size
        nx = (
            math.ceil(baffle.x / size - 1.0e-12),
            math.ceil(baffle.thickness / size - 1.0e-12),
            math.ceil((domain.length - baffle.x - baffle.thickness) / size - 1.0e-12),
        )
        ny = (
            math.ceil(baffle.height / size - 1.0e-12),
            math.ceil((domain.height - baffle.height) / size - 1.0e-12),
        )
        nz = math.ceil(domain.width / size - 1.0e-12)
        estimated_cells = (
            nx[0] * (ny[0] + ny[1])
            + nx[1] * ny[1]
            + nx[2] * (ny[0] + ny[1])
        ) * nz

    components = {
        "fluid.velocity": 3,
        "fluid.vorticity": 3,
    }
    scalar_components = sum(components.get(name, 1) for name in step.output.fields)
    if estimated_cells is None:
        estimated_portable_bytes = None
        estimated_temporary_peak_bytes = None
    else:
        entities = (
            math.ceil(estimated_cells * 1.25)
            if step.output.portable_profile == "visualization"
            else estimated_cells
            if step.output.portable_profile == "native"
            else estimated_cells + math.ceil(estimated_cells * 1.25)
        )
        field_bytes = requested_frames * entities * scalar_components * 8
        mesh_bytes = estimated_cells * 8 * 10
        estimated_portable_bytes = math.ceil(1.10 * (field_bytes + mesh_bytes) + 1024**2)
        if "npz" in step.output.portable_formats:
            estimated_portable_bytes += field_bytes + mesh_bytes
        # Current external adapter stages solver-native and VTK data before publishing HDF5.
        estimated_temporary_peak_bytes = estimated_portable_bytes + field_bytes * 2

    return {
        "channels": {
            "histories": {
                "names": list(step.output.histories),
                "retention": "all scalar samples",
            },
            "reports": {
                "definitions": [item.to_dict() for item in step.output.reports],
                "retention": "all compact samples",
            },
            "field_frames": {
                **frames.to_dict(),
                "resolved_count": requested_frames,
            },
            "checkpoints": step.output.checkpoints.to_dict(),
        },
        "estimated_mesh_cells": estimated_cells,
        "estimated_portable_bytes": estimated_portable_bytes,
        "estimated_portable_display": (
            None if estimated_portable_bytes is None else _human_bytes(estimated_portable_bytes)
        ),
        "estimated_temporary_peak_bytes": estimated_temporary_peak_bytes,
        "estimated_temporary_peak_display": (
            None
            if estimated_temporary_peak_bytes is None
            else _human_bytes(estimated_temporary_peak_bytes)
        ),
        "storage": step.output.storage.to_dict(),
        "within_budget": (
            None
            if estimated_temporary_peak_bytes is None
            else estimated_temporary_peak_bytes <= step.output.storage.maximum_bytes
        ),
        "estimate_kind": "conservative-preflight-not-measured",
    }


class Project:
    """A case.py plus operational manifest and content-addressed run history."""

    def __init__(self, root: str | Path):
        selected = Path(root)
        if selected.is_file():
            selected = selected.parent
        self.root = selected.resolve()
        self.manifest_path = self.root / "agentcfd.toml"
        if not self.manifest_path.is_file():
            raise FileNotFoundError(self.manifest_path)
        self.manifest = ProjectManifest.read(self.manifest_path)
        self.entrypoint = _safe_project_path(
            self.root,
            self.manifest.entrypoint,
            label="entrypoint",
        )
        self.run_root = _safe_project_path(
            self.root,
            self.manifest.run_directory,
            label="run_directory",
        )
        if not self.entrypoint.is_file():
            raise FileNotFoundError(self.entrypoint)

    def load_step(self) -> Step:
        module = _load_module(self.entrypoint, self.root)
        factory = getattr(module, self.manifest.factory, None)
        if not callable(factory):
            raise ProjectError(
                f"Project entrypoint must define callable {self.manifest.factory}()."
            )
        try:
            step = factory()
        except Exception as error:
            raise ProjectError(f"Project factory failed: {error}") from error
        if not isinstance(step, Step):
            raise ProjectError("Project factory must return an AgentCFD Step.")
        return step

    def _openfoam_settings(self) -> dict[str, object]:
        settings = dict(self.manifest.openfoam)
        allowed = {
            "container_image",
            "cross_section_cells",
            "axial_cells",
            "nominal_wall_cell_fraction",
            "export_fields",
            "keep_workspace",
        }
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise ProjectError(f"Unknown [openfoam] keys: {', '.join(unknown)}")
        return settings

    def _provider(
        self,
        name: str,
        *,
        step: Step | None = None,
        case_directory: Path | None = None,
        container_image: str | None = None,
    ):
        if name == "reference":
            return ReferencePipeProvider()
        if name != "openfoam":
            raise ProjectError(f"Unknown provider {name!r}.")
        settings = self._openfoam_settings()
        selected_image = container_image or settings.get("container_image")
        if step is not None and isinstance(step.model.domain, RectangularChannel):
            return OpenFOAMChannelProvider(
                case_directory=case_directory,
                container_image=str(selected_image) if selected_image else None,
            )
        mesh = OpenFOAMMeshControls(
            cross_section_cells=settings.get("cross_section_cells", 8),
            axial_cells=settings.get("axial_cells"),
            nominal_wall_cell_fraction=settings.get("nominal_wall_cell_fraction"),
        )
        return OpenFOAMProvider(
            case_directory=case_directory,
            container_image=str(selected_image) if selected_image else None,
            mesh=mesh,
        )

    def _execution_fingerprint(
        self,
        analysis_sha256: object,
        *,
        provider: str,
        container_image: str | None = None,
    ) -> str:
        settings: dict[str, object] = {}
        if provider == "openfoam":
            settings = self._openfoam_settings()
            if container_image is not None:
                settings["container_image"] = container_image
        return content_fingerprint(
            {
                "analysis_sha256": analysis_sha256,
                "provider": provider,
                "provider_settings": settings,
            }
        )

    def plan(
        self,
        *,
        provider: str | None = None,
        container_image: str | None = None,
        _step: Step | None = None,
    ) -> dict[str, object]:
        selected_name = provider or self.manifest.default_provider
        if selected_name not in {"reference", "openfoam"}:
            raise ProjectError("Provider must be 'reference' or 'openfoam'.")
        step = _step or self.load_step()
        issues: list[ProjectIssue] = []
        model_valid = True
        try:
            step.model.validate()
        except ModelValidationError as error:
            model_valid = False
            issues.append(
                ProjectIssue(
                    "MODEL_INVALID",
                    "error",
                    str(error),
                    "case.py",
                    "Complete the public Model before selecting a provider.",
                )
            )
        selected = self._provider(
            selected_name,
            step=step,
            container_image=container_image,
        )
        descriptor = selected.descriptor()
        study = step.model.study
        if selected_name == "reference":
            required_capability = "reference.hagen-poiseuille"
        elif isinstance(step.model.domain, RectangularChannel):
            required_capability = "openfoam.transient-laminar-baffled-channel"
        else:
            required_capability = (
                "openfoam.steady-laminar-circular-pipe"
                if study.laminar
                else "openfoam.steady-rans-smooth-circular-pipe"
            )
        provider_compatible = model_valid
        compatibility_detail = "model validation failed"
        if model_valid:
            validate_provider = getattr(selected, "validate", None)
            if validate_provider is None:
                provider_compatible = False
                compatibility_detail = "provider has no validation contract"
            else:
                try:
                    validate_provider(step)
                except UnsupportedCaseError as error:
                    provider_compatible = False
                    compatibility_detail = str(error)
        if not provider_compatible:
            issues.append(
                ProjectIssue(
                    "PROVIDER_INCOMPATIBLE",
                    "error",
                    f"Provider {selected_name!r} does not support this step: "
                    f"{compatibility_detail.rstrip('.')}.",
                    "case.py",
                    "Choose a compatible provider or simplify the explicit step intent.",
                )
            )

        reynolds = _inlet_reynolds(step) if model_valid else None
        if selected_name == "reference" and reynolds is not None and reynolds >= 2300:
            provider_compatible = False
            issues.append(
                ProjectIssue(
                    "REFERENCE_REYNOLDS_OUT_OF_RANGE",
                    "error",
                    f"Reference Hagen-Poiseuille provider requires Re < 2300; resolved Re={reynolds:.6g}.",
                    "case.py:boundaries",
                    "Use OpenFOAM with an explicit turbulent Study or reduce the declared flow rate.",
                )
            )
        try:
            output_plan = _resolved_output_plan(
                step,
                provider=selected_name,
                openfoam=self._openfoam_settings(),
            )
        except (ModelValidationError, ValueError) as error:
            output_plan = {"valid": False, "error": str(error)}
            issues.append(
                ProjectIssue(
                    "OUTPUT_POLICY_INVALID",
                    "error",
                    str(error),
                    "case.py:output",
                    "Adjust outputs.animation interval, coordinate, maximum_frames, or budget.",
                )
            )
        else:
            if output_plan["within_budget"] is False:
                issues.append(
                    ProjectIssue(
                        "OUTPUT_BUDGET_EXCEEDED",
                        "error",
                        "Conservative temporary storage estimate "
                        f"{output_plan['estimated_temporary_peak_display']} exceeds "
                        f"the declared {_human_bytes(step.output.storage.maximum_bytes)} budget.",
                        "case.py:output.storage",
                        "Reduce field frames/variables, select one association, or raise the explicit budget.",
                    )
                )
        output_ready = not any(issue.code.startswith("OUTPUT_") for issue in issues)
        runtime_available = descriptor.available
        if not runtime_available:
            issues.append(
                ProjectIssue(
                    "PROVIDER_RUNTIME_UNAVAILABLE",
                    "warning",
                    f"Provider runtime for {selected_name!r} is not currently available.",
                    "runtime",
                    "Install the runtime or configure [openfoam].container_image.",
                )
            )
        export_fields = selected_name == "openfoam" and bool(
            self._openfoam_settings().get("export_fields", True)
        )
        io_ready = not export_fields or data_exchange.io_available()
        if not io_ready:
            issues.append(
                ProjectIssue(
                    "PORTABLE_IO_UNAVAILABLE",
                    "error",
                    "Standard XDMF/H5 output dependencies are unavailable.",
                    "runtime:io",
                    "Install `agentcfd[io]` in the execution environment.",
                )
            )
        ready_to_run = (
            model_valid
            and provider_compatible
            and runtime_available
            and io_ready
            and output_ready
        )
        decisions = {
            "study": study.to_dict(),
            "procedure": step.procedure.to_dict(),
            "outputs": step.output.to_dict(),
            "initialization": None if step.initialization is None else step.initialization.to_dict(),
            "mesh_intent": None if step.mesh is None else step.mesh.to_dict(),
            "provider": asdict(descriptor),
            "required_capability": required_capability,
            "mesh_strategy": (
                f"intent:{step.mesh.method}"
                if step.mesh is not None
                else "structured-circular-pipe-o-grid"
                if selected_name == "openfoam"
                else "analytical"
            ),
            "solver": (
                "unresolved-provider-lowering"
                if not provider_compatible
                else "pimpleFoam"
                if selected_name == "openfoam" and not study.steady
                else "simpleFoam"
                if selected_name == "openfoam"
                else "Hagen-Poiseuille"
            ),
            "portable_field_bundle": export_fields,
            "portable_formats": (
                [
                    "xdmf",
                    "hdf5",
                    *(["npz"] if "npz" in step.output.portable_formats else []),
                ]
                if export_fields
                else []
            ),
            "output_plan": output_plan,
        }
        plan: dict[str, object] = {
            "schema": "agentcfd.solution-plan/0.1",
            "project": {
                "root": str(self.root),
                "entrypoint": self.manifest.entrypoint,
                "entrypoint_sha256": file_sha256(self.entrypoint),
                "factory": self.manifest.factory,
            },
            "model": {
                "name": step.model.name,
                "sha256": step.model.fingerprint() if model_valid else None,
                "analysis_sha256": step.fingerprint() if model_valid else None,
                "summary": step.model.to_dict(),
                "reynolds_number": reynolds,
            },
            "decisions": decisions,
            "readiness": {
                "model_valid": model_valid,
                "provider_compatible": provider_compatible,
                "runtime_available": runtime_available,
                "portable_io_available": io_ready,
                "ready_to_run": ready_to_run,
            },
            "issues": [issue.to_dict() for issue in issues],
            "next_actions": (
                ["agentcfd run ."]
                if ready_to_run
                else [issue.repair for issue in issues if issue.severity != "info"]
            ),
        }
        plan["plan_sha256"] = content_fingerprint(plan)
        return plan

    def _run_records(self) -> list[dict[str, object]]:
        runs: list[dict[str, object]] = []
        candidates = [self.run_root / "run.json"]
        campaign_root = self.root / "campaigns"
        if campaign_root.is_dir():
            candidates.extend(sorted(campaign_root.glob("*/run.json"), reverse=True))
        # Backward-compatible discovery for 0.1 projects whose run_directory
        # already contains immutable run-id subdirectories.
        if self.manifest.run_mode == "campaign" and self.run_root.is_dir():
            candidates.extend(sorted(self.run_root.glob("*/run.json"), reverse=True))
        seen: set[Path] = set()
        for path in candidates:
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                runs.append(record)
        runs.sort(
            key=lambda record: str(
                record.get("completed_at")
                or record.get("started_at")
                or record.get("run_id")
                or ""
            ),
            reverse=True,
        )
        return runs

    def _active_run_ids(self) -> set[str]:
        return {
            str(record["run_id"])
            for record in self._run_records()
            if record.get("status") in {"preparing", "running", "exporting"}
            and _process_is_alive(record.get("pid"))
            and record.get("run_id")
        }

    def _record_directory(self, record: Mapping[str, object] | None) -> Path | None:
        if record is None:
            return None
        run_id = str(record.get("run_id", ""))
        local_candidates = (
            (self.run_root,)
            if record.get("mode") == "replace"
            else (self.root / "campaigns" / run_id, self.run_root / run_id)
        )
        local = next((path for path in local_candidates if path.is_dir()), None)
        if local is not None:
            return local
        recorded = Path(str(record.get("directory", "")))
        return recorded if recorded.is_absolute() else self.root / recorded

    def storage(self) -> dict[str, object]:
        """Inventory managed project data without reading field arrays."""

        project_argument = (
            "." if Path.cwd().resolve() == self.root else shlex.quote(str(self.root))
        )
        workspace_root = self.root / ".agentcfd" / "work"
        groups = {
            "current_output": self.run_root,
            "campaigns": self.root / "campaigns",
            "temporary_workspaces": workspace_root,
        }
        categories: dict[str, dict[str, object]] = {}
        for name, path in groups.items():
            size, files = _tree_usage(path)
            categories[name] = {
                "path": str(path),
                "exists": path.exists(),
                "bytes": size,
                "display": _human_bytes(size),
                "file_count": files,
            }
        managed = sum(int(item["bytes"]) for item in categories.values())
        active_run_ids = self._active_run_ids()
        active_workspace_bytes = sum(
            _tree_usage(workspace_root / run_id)[0] for run_id in active_run_ids
        )
        workspace_bytes = int(categories["temporary_workspaces"]["bytes"])
        reclaimable = max(0, workspace_bytes - active_workspace_bytes)
        categories["temporary_workspaces"]["active_bytes"] = active_workspace_bytes
        categories["temporary_workspaces"]["active_display"] = _human_bytes(
            active_workspace_bytes
        )
        categories["temporary_workspaces"]["active_run_ids"] = sorted(active_run_ids)
        disk = shutil.disk_usage(self.root)
        return {
            "schema": "agentcfd.project-storage/0.1",
            "root": str(self.root),
            "managed_bytes": managed,
            "managed_display": _human_bytes(managed),
            "reclaimable_bytes": reclaimable,
            "reclaimable_display": _human_bytes(reclaimable),
            "filesystem": {
                "free_bytes": disk.free,
                "free_display": _human_bytes(disk.free),
                "total_bytes": disk.total,
                "total_display": _human_bytes(disk.total),
            },
            "categories": categories,
            "policy": {
                "ordinary_run": "replace managed current_output",
                "campaign_run": "retain immutable campaigns",
                "temporary_workspaces": "removed after successful export unless explicitly kept or interrupted",
                "portable_fields": "XDMF index plus compressed HDF5 payload",
            },
            "next_action": (
                {
                    "command": f"agentcfd clean {project_argument} --apply",
                    "reason": "Remove only hidden temporary solver workspaces.",
                }
                if reclaimable
                else None
            ),
        }

    def clean(self, *, apply: bool = False) -> dict[str, object]:
        """Preview or remove hidden temporary workspaces; preserve all results."""

        workspace_root = self.root / ".agentcfd" / "work"
        active_run_ids = self._active_run_ids()
        before = 0
        file_count = 0
        targets = []
        if workspace_root.is_dir():
            for path in sorted(workspace_root.iterdir()):
                size, files = _tree_usage(path)
                protected = path.name in active_run_ids
                targets.append(
                    {
                        "path": str(path),
                        "bytes": size,
                        "display": _human_bytes(size),
                        "file_count": files,
                        "protected_active_run": protected,
                    }
                )
                if not protected:
                    before += size
                    file_count += files
        if apply and workspace_root.exists():
            for target in targets:
                if target["protected_active_run"]:
                    continue
                path = Path(str(target["path"]))
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            for empty_parent in (workspace_root, workspace_root.parent):
                try:
                    empty_parent.rmdir()
                except OSError:
                    pass
        after = sum(
            _tree_usage(Path(str(target["path"])))[0]
            for target in targets
            if not target["protected_active_run"]
        )
        return {
            "schema": "agentcfd.project-clean/0.1",
            "root": str(self.root),
            "applied": apply,
            "scope": "temporary-workspaces-only",
            "targets": targets,
            "preserved": [str(self.run_root), str(self.root / "campaigns")],
            "protected_active_run_ids": sorted(active_run_ids),
            "candidate_bytes": before,
            "candidate_display": _human_bytes(before),
            "candidate_file_count": file_count,
            "reclaimed_bytes": before - after if apply else 0,
            "reclaimed_display": _human_bytes(before - after if apply else 0),
        }

    def status(self, *, include_storage: bool = False) -> dict[str, object]:
        """Return one human/agent decision surface for the whole project."""

        plan = self.plan()
        current_execution_sha256 = self._execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=self.manifest.default_provider,
        )
        project_argument = (
            "." if Path.cwd().resolve() == self.root else shlex.quote(str(self.root))
        )
        runs = self._run_records()
        latest = runs[0] if runs else None
        run_directory = self._record_directory(latest)
        error_issues = [
            issue for issue in plan["issues"] if issue.get("severity") == "error"
        ]
        active = False
        changed = False
        if latest is None:
            state = "ready" if plan["readiness"]["ready_to_run"] else "blocked"
        else:
            native_status = str(latest.get("status", "unknown"))
            if native_status in {"preparing", "running", "exporting"}:
                active = _process_is_alive(latest.get("pid"))
                # The process can publish its final atomic marker between our
                # first file read and liveness probe. Re-read once before
                # reporting a false interruption during that narrow handoff.
                if not active and run_directory is not None:
                    try:
                        refreshed = json.loads(
                            (run_directory / "run.json").read_text(encoding="utf-8")
                        )
                    except (OSError, TypeError, json.JSONDecodeError):
                        refreshed = None
                    if isinstance(refreshed, dict) and refreshed.get("status") not in {
                        "preparing",
                        "running",
                        "exporting",
                    }:
                        latest = refreshed
                        native_status = str(latest.get("status", "unknown"))
            if native_status in {"preparing", "running", "exporting"}:
                state = "running" if active else "interrupted"
            else:
                latest_execution = latest.get("execution_sha256")
                latest_analysis = latest.get("analysis_sha256")
                if latest_analysis is None and run_directory is not None:
                    try:
                        saved_plan = json.loads(
                            (run_directory / "plan.json").read_text(encoding="utf-8")
                        )
                        latest_analysis = saved_plan["model"]["analysis_sha256"]
                    except (OSError, KeyError, TypeError, json.JSONDecodeError):
                        pass
                if isinstance(latest_execution, str):
                    changed = latest_execution != current_execution_sha256
                elif isinstance(latest_analysis, str):
                    changed = latest_analysis != plan["model"]["analysis_sha256"]
                else:
                    changed = latest.get("plan_sha256") != plan["plan_sha256"]
                if changed:
                    state = "modified"
                elif native_status == "completed" and latest.get("accepted") is True:
                    state = "complete"
                elif native_status == "completed":
                    state = "review"
                else:
                    state = "failed"
        needs_execution = state in {"ready", "modified", "interrupted", "failed"}
        if needs_execution and (
            error_issues or plan["readiness"]["ready_to_run"] is not True
        ):
            state = "blocked"

        result_path = None if run_directory is None else run_directory / "result.json"
        fields_path = None if run_directory is None else run_directory / "fields" / "fields.xdmf"
        if fields_path is not None and not fields_path.is_file():
            fields_path = None
        if result_path is not None and not result_path.is_file():
            result_path = None
        postprocess = {
            "primary": str(fields_path or result_path) if fields_path or result_path else None,
            "fields": None if fields_path is None else str(fields_path),
            "result": None if result_path is None else str(result_path),
            "command": (
                f"agentcfd view {project_argument}" if fields_path or result_path else None
            ),
            "field_summary": (
                None if fields_path is None else _field_bundle_summary(fields_path)
            ),
        }
        progress = _run_progress_snapshot(
            self.root,
            latest,
            plan,
            include_storage=include_storage,
        )
        if state == "blocked":
            next_action = {
                "command": f"agentcfd check {project_argument}",
                "reason": (
                    error_issues[0]["repair"]
                    if error_issues
                    else plan["issues"][0]["repair"]
                    if plan["issues"]
                    else "Resolve readiness issues."
                ),
            }
        elif state in {"ready", "modified", "interrupted", "failed"}:
            next_action = {
                "command": f"agentcfd run {project_argument}",
                "reason": {
                    "ready": "Create the first result.",
                    "modified": "Inputs changed since the latest result.",
                    "interrupted": "Safely replace the interrupted managed run.",
                    "failed": "Retry after reviewing the recorded failure.",
                }[state],
            }
        elif state == "running":
            next_action = {
                "command": f"agentcfd status {project_argument}",
                "reason": "Wait for the active solver/export phase to finish.",
            }
        elif state == "review":
            failed = latest.get("failed_checks", []) if latest is not None else []
            next_action = {
                "command": f"agentcfd view {project_argument}",
                "reason": (
                    "Review failed acceptance checks: "
                    + ", ".join(str(item) for item in failed)
                    if failed
                    else "Review result.json acceptance checks before using this result."
                ),
            }
        else:
            next_action = {
                "command": f"agentcfd view {project_argument}",
                "reason": "Open the compact result for review.",
            }
        report: dict[str, object] = {
            "schema": "agentcfd.project-status/0.1",
            "root": str(self.root),
            "state": state,
            "active": active,
            "inputs_changed": changed,
            "model": plan["model"],
            "readiness": plan["readiness"],
            "issues": plan["issues"],
            "run_count": len(runs),
            "latest_run": latest,
            "progress": progress,
            "postprocess": postprocess,
            "next_action": next_action,
        }
        if include_storage:
            report["storage"] = self.storage()
        return report

    def inspect(self) -> dict[str, object]:
        """Backward-compatible detailed inspection built from project status."""

        status = self.status()
        return {
            "schema": "agentcfd.project-inspection/0.1",
            "root": str(self.root),
            "state": status["state"],
            "model": status["model"],
            "readiness": status["readiness"],
            "issues": status["issues"],
            "run_count": status["run_count"],
            "latest_run": status["latest_run"],
            "progress": status["progress"],
            "postprocess": status["postprocess"],
            "next_action": status["next_action"],
        }

    def run(
        self,
        *,
        provider: str | None = None,
        container_image: str | None = None,
        campaign: bool = False,
        keep_workspace: bool = False,
    ) -> ProjectRun:
        selected_name = provider or self.manifest.default_provider
        step = self.load_step()
        plan = self.plan(
            provider=selected_name,
            container_image=container_image,
            _step=step,
        )
        readiness = plan["readiness"]
        assert isinstance(readiness, dict)
        if readiness["ready_to_run"] is not True:
            codes = ", ".join(issue["code"] for issue in plan["issues"])
            raise ProjectError(f"Project is not ready to run: {codes or 'unknown issue'}")
        output_plan = plan["decisions"]["output_plan"]
        estimated_peak = output_plan.get("estimated_temporary_peak_bytes")
        campaign_mode = campaign or self.manifest.run_mode == "campaign"
        existing_output_bytes = 0 if campaign_mode else _tree_usage(self.run_root)[0]
        available_bytes = shutil.disk_usage(self.root).free + existing_output_bytes
        if isinstance(estimated_peak, int) and estimated_peak > available_bytes:
            raise ProjectError(
                "Insufficient free disk for the conservative temporary-output estimate: "
                f"need {_human_bytes(estimated_peak)}, available "
                f"{_human_bytes(available_bytes)}. Reduce frames/fields or free space, "
                "then retry."
            )
        now = datetime.now(UTC)
        model_sha = step.model.fingerprint()
        run_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{model_sha[:8]}"
        run_directory = (
            self.root / "campaigns" / run_id if campaign_mode else self.run_root
        )
        if not campaign_mode and run_directory.exists() and any(run_directory.iterdir()):
            marker = run_directory / "run.json"
            try:
                owned = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owned = None
            if not isinstance(owned, dict) or owned.get("schema") != "agentcfd.project-run/0.1":
                raise ProjectError(
                    f"Refusing to replace unmanaged output directory: {run_directory}"
                )
            if owned.get("status") in {"preparing", "running", "exporting"} and _process_is_alive(
                owned.get("pid")
            ):
                raise ProjectError(
                    "Refusing to replace an active AgentCFD run. Use `agentcfd status .` "
                    "to inspect its current phase."
                )
            shutil.rmtree(run_directory)
        run_directory.mkdir(parents=True, exist_ok=not campaign_mode)
        plan_path = run_directory / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Establish ownership before external execution. If a solver or exporter
        # raises, the next replace run can safely recover this AgentCFD-owned
        # directory instead of forcing the user to delete it manually.
        marker_path = run_directory / "run.json"
        started_at = datetime.now(UTC).isoformat()
        execution_sha256 = self._execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=selected_name,
            container_image=container_image,
        )
        marker_record: dict[str, object] = {
            "schema": "agentcfd.project-run/0.1",
            "run_id": run_id,
            "mode": "campaign" if campaign_mode else "replace",
            "directory": str(run_directory),
            "status": "preparing",
            "phase": "preparing-provider-case",
            "pid": os.getpid(),
            "plan": str(plan_path),
            "plan_sha256": plan["plan_sha256"],
            "analysis_sha256": plan["model"]["analysis_sha256"],
            "execution_sha256": execution_sha256,
            "started_at": started_at,
            "completed_at": None,
        }

        def write_marker(**updates: object) -> None:
            marker_record.update(updates)
            temporary = marker_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(marker_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(marker_path)

        write_marker()
        workspace_root = self.root / ".agentcfd" / "work" / run_id
        case_directory = workspace_root / "openfoam"
        selected = self._provider(
            selected_name,
            step=step,
            case_directory=case_directory if selected_name == "openfoam" else None,
            container_image=container_image,
        )
        write_marker(status="running", phase="solver")
        try:
            result = selected.run(step)
        except Exception as error:
            write_marker(
                status="failed",
                phase="solver",
                completed_at=datetime.now(UTC).isoformat(),
                failure={
                    "type": type(error).__name__,
                    "message": str(error),
                    "safe_to_retry": True,
                    "repair": "Review the solver logs or readiness issue, then run `agentcfd run .` again.",
                },
            )
            raise
        retained_workspace = keep_workspace or bool(
            self._openfoam_settings().get("keep_workspace", False)
        )
        bundle = None
        if selected_name == "openfoam" and result.status == "completed" and bool(
            self._openfoam_settings().get("export_fields", True)
        ):
            write_marker(status="exporting", phase="portable-fields")
            try:
                bundle = data_exchange.export_openfoam_case(
                    case_directory,
                    run_directory / "fields",
                    container_image=selected.container_image,
                    density=step.model.fluid.density,
                    axis={
                        "name": "solver_iteration" if step.model.study.steady else "time",
                        "unit": "1" if step.model.study.steady else "s",
                        "physical_time": not step.model.study.steady,
                        "description": (
                            "Steady-solver iteration; not physical transient time."
                            if step.model.study.steady
                            else "Physical simulation time in SI seconds."
                        ),
                    },
                    source={
                        "model_sha256": step.model.fingerprint(),
                        "result_status": result.status,
                        "trust_level": result.trust_level,
                        "accepted": result.accepted,
                    },
                    profile=step.output.portable_profile,
                    fields=step.output.fields,
                    formats=step.output.portable_formats,
                    compression=step.output.storage.compression,
                    maximum_bytes=step.output.storage.maximum_bytes,
                    include_initial=step.output.frames.include_initial,
                    time_interval=(
                        step.output.frames.every
                        if step.output.frames.mode == "interval"
                        else None
                    ),
                    latest_only=step.output.frames.mode == "final",
                )
            except Exception as error:
                write_marker(
                    status="failed",
                    phase="portable-fields",
                    completed_at=datetime.now(UTC).isoformat(),
                    failure={
                        "type": type(error).__name__,
                        "message": str(error),
                        "safe_to_retry": True,
                        "repair": "Fix portable I/O or storage settings, then run `agentcfd run .` again.",
                    },
                )
                raise
            portable_artifacts = [
                ("fields.xdmf", bundle.xdmf, "application/x-xdmf+xml"),
                ("fields.hdf5", bundle.hdf5, "application/x-hdf5"),
                ("fields.manifest", bundle.manifest, "application/json"),
            ]
            if bundle.npz is not None:
                portable_artifacts.insert(
                    2, ("fields.npz", bundle.npz, "application/x-npz")
                )
            for name, path, media_type in portable_artifacts:
                result.artifacts[name] = Artifact.from_path(
                    path,
                    role="portable-field-bundle",
                    media_type=media_type,
                )
            manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
            for record in manifest["fields"]:
                name = record["export_name"]
                result.fields[name] = FieldRecord(
                    unit=record["unit"],
                    location=record["association"],
                    artifact=str(bundle.xdmf.relative_to(run_directory)),
                    components=tuple(record["components"]),
                    representation="xdmf-hdf5",
                    description=record["description"],
                    processing={"operation": record["processing"]},
                )
            if not retained_workspace:
                result.fields = {
                    name: field
                    for name, field in result.fields.items()
                    if field.representation != "provider-native"
                }
                result.artifacts = {
                    name: artifact
                    for name, artifact in result.artifacts.items()
                    if not name.startswith("field_")
                }
        if selected_name == "openfoam":
            evidence_directory = run_directory / "evidence"
            copied_paths: dict[str, Path] = {}
            for name, artifact in tuple(result.artifacts.items()):
                source_path = Path(artifact.path)
                try:
                    source_path.resolve().relative_to(workspace_root.resolve())
                except ValueError:
                    continue
                if name.startswith("log_"):
                    target_name = f"{name.removeprefix('log_')}.log"
                elif name == "restart_bundle":
                    target_name = "restart.zip"
                elif source_path.suffix == ".json":
                    target_name = f"{name}.json"
                else:
                    target_name = name
                target_path = evidence_directory / target_name
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                copied_paths[str(source_path)] = target_path
                result.artifacts[name] = Artifact.from_path(
                    target_path,
                    role=artifact.role,
                    media_type=artifact.media_type,
                )
            for name, field in tuple(result.fields.items()):
                copied = copied_paths.get(field.artifact)
                if copied is not None:
                    result.fields[name] = replace(field, artifact=str(copied))
            if not retained_workspace and workspace_root.is_dir():
                shutil.rmtree(workspace_root)
                for empty_parent in (workspace_root.parent, workspace_root.parent.parent):
                    try:
                        empty_parent.rmdir()
                    except OSError:
                        pass
        result_path = result.write(run_directory / "result.json")
        completed = ProjectRun(
            run_id=run_id,
            directory=run_directory,
            result=result,
            result_path=result_path,
            plan_path=plan_path,
            field_bundle=bundle,
            mode="campaign" if campaign_mode else "replace",
            solver_workspace=workspace_root if retained_workspace else None,
        )
        run_record = completed.to_dict()
        run_record["plan_sha256"] = plan["plan_sha256"]
        run_record["analysis_sha256"] = plan["model"]["analysis_sha256"]
        run_record["execution_sha256"] = execution_sha256
        run_record["phase"] = "complete"
        run_record["pid"] = None
        run_record["started_at"] = started_at
        run_record["completed_at"] = datetime.now(UTC).isoformat()
        _write_output_guide(completed, model_name=step.model.name)
        (run_directory / "run.json").write_text(
            json.dumps(run_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return completed


_CASE_TEMPLATE = '''"""Readable AgentCFD engineering model: edit this file, not backend dictionaries."""

from agentcfd import Model, boundaries, fluids, geometry, outputs, procedures, studies


def build():
    model = Model(
        name="water-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=10.0, diameter=0.05),
        fluid=fluids.newtonian(
            "water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.02),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    return model.step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
'''


_BAFFLE_CHANNEL_TEMPLATE = '''"""Low-Re transient wake behind a bottom-attached baffle."""

from agentcfd import Model, boundaries, fluids, geometry, initialization, meshing, outputs, procedures, studies


def build():
    channel = geometry.rectangular_channel(length=1.2, height=0.20, width=0.10).with_baffle(
        name="baffle", x=0.35, height=0.12, thickness=0.01, attached_to="bottom"
    )
    model = Model(
        name="bottom-baffle-wake",
        study=studies.internal_flow(steady=False),
        domain=channel,
        fluid=fluids.newtonian("viscous-liquid", density=1000.0, dynamic_viscosity=0.05),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.5),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
        baffle=boundaries.no_slip_wall(),
    )
    return model.step(
        procedure=procedures.transient(
            end_time=2.0,
            initial_time_step=0.001,
            maximum_time_step=0.005,
            maximum_courant_number=0.5,
        ),
        initialization=initialization.potential_flow(maximum_iterations=500),
        mesh=meshing.structured(base_size=0.01),
        output=outputs.animation(
            every=0.01,
            maximum_frames=201,
            storage_budget="1 GiB",
            reports=(
                outputs.probe("near-wake", at=(0.50, 0.05, 0.05)),
                outputs.surface_report(
                    "outlet-pressure", region="outlet", field="fluid.pressure"
                ),
                outputs.force_report("baffle-drag", regions=("baffle",)),
            ),
        ),
    )
'''


def init_project(
    directory: str | Path,
    *,
    provider: str = "reference",
    template: str = "industrial-pipe",
) -> Project:
    """Create a complete editable project without overwriting user data."""

    if provider not in {"reference", "openfoam"}:
        raise ValueError("Project provider must be 'reference' or 'openfoam'.")
    if template not in {"industrial-pipe", "baffle-channel"}:
        raise ValueError("Project template must be 'industrial-pipe' or 'baffle-channel'.")
    if template == "baffle-channel" and provider != "openfoam":
        raise ValueError("The baffle-channel template requires provider='openfoam'.")
    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Project directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    pipe_mesh_settings = (
        "cross_section_cells = 8\naxial_cells = 120\n"
        if template == "industrial-pipe"
        else ""
    )
    manifest = f'''schema = "agentcfd.project/0.1"
entrypoint = "case.py"
factory = "build"
default_provider = "{provider}"
run_directory = "output"
run_mode = "replace"

[openfoam]
container_image = "opencfd/openfoam-run:2606"
{pipe_mesh_settings}export_fields = true
keep_workspace = false
'''
    (root / "agentcfd.toml").write_text(manifest, encoding="utf-8")
    (root / "case.py").write_text(
        _BAFFLE_CHANNEL_TEMPLATE if template == "baffle-channel" else _CASE_TEMPLATE,
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"# AgentCFD {template}\n\n"
        "Edit `case.py`, then run `agentcfd status .` and follow its one recommended "
        "next action. The normal loop is `agentcfd run .` followed by "
        "`agentcfd view .`; `check`, `plan`, and `inspect` remain available for deeper "
        "diagnosis. Ordinary runs replace the managed `output/` directory. Use "
        "`agentcfd run . --campaign` to preserve an immutable run, "
        "`agentcfd storage .` to audit space, or `--keep-workspace` only for expert "
        "solver debugging.\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# Agent instructions\n\n"
        "Treat `case.py` as the modeling source of truth. Begin with "
        "`agentcfd status . --json`, execute only its `next_action.command`, and re-read "
        "status after each action. Do not edit generated OpenFOAM dictionaries to "
        "change scientific intent. Preserve plan, result, XDMF/H5, selected NPZ, and "
        "failed checks together. Never promote a result whose `accepted` value is false.\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "output/\ncampaigns/\n.agentcfd/\n__pycache__/\n",
        encoding="utf-8",
    )
    return Project(root)


__all__ = [
    "Project",
    "ProjectIssue",
    "ProjectManifest",
    "ProjectRun",
    "init_project",
]
