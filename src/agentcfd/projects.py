"""One readable project lifecycle shared by people, agents, CLIs, and GUIs."""

from __future__ import annotations

import csv
import importlib.util
import inspect
import errno
import json
import keyword
import math
import os
import re
import shlex
import shutil
import statistics
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Mapping, Sequence

from . import (
    boundaries,
    data_exchange,
    diagnostics,
    engineering,
    geometry_io,
    parameters as parameter_definitions,
    postprocessing,
)
from .errors import (
    AgentCFDError,
    ModelValidationError,
    ProjectError,
    UnsupportedCaseError,
)
from .geometry import CircularPipe, ImportedSurface, RectangularChannel
from .jsonio import strict_json_object
from .model import Step
from .provenance import content_fingerprint, file_sha256
from .providers import (
    OpenFOAMChannelProvider,
    OpenFOAMImportedProvider,
    OpenFOAMMeshControls,
    OpenFOAMProvider,
    ReferencePipeProvider,
    plan_imported_mesh,
)
from .providers.openfoam_channel import materialize_interrupted_restart
from .results import Artifact, FieldRecord, SimulationResult, read_result_record


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
            raise ProjectError(
                f"Cannot read AgentCFD project manifest {path}: {error}"
            ) from error
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
        if any(
            not isinstance(value, str) or not value.strip()
            for value in strings.values()
        ):
            raise ProjectError(
                "Project entrypoint, factory, provider, and run directory are required strings."
            )
        provider = str(strings["default_provider"]).strip()
        if provider not in {"reference", "openfoam"}:
            raise ProjectError(
                "Project default_provider must be 'reference' or 'openfoam'."
            )
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


def _discover_project_root(selected: Path) -> Path:
    """Find the nearest project manifest from a file or nested directory."""

    resolved = selected.expanduser().resolve()
    if not resolved.exists():
        return resolved
    start = resolved.parent if resolved.is_file() else resolved
    for candidate in (start, *start.parents):
        if (candidate / "agentcfd.toml").is_file():
            return candidate
    return start


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
    if not hasattr(step.model.domain, "area") or not hasattr(
        step.model.domain, "hydraulic_diameter"
    ):
        return None
    if isinstance(inlet, boundaries.MassFlowInlet):
        velocity = inlet.mass_flow_rate / (
            step.model.fluid.density * step.model.domain.area
        )
    else:
        velocity = inlet.velocity
    return engineering.reynolds_number(
        density=step.model.fluid.density,
        mean_velocity=velocity,
        hydraulic_diameter=step.model.domain.hydraulic_diameter,
        dynamic_viscosity=step.model.fluid.dynamic_viscosity,
    )


def _thermal_preflight(step: Step) -> dict[str, object]:
    """Return a truthful thermal planning record without pretending to solve."""

    if not step.model.study.energy:
        return {
            "status": "not-requested",
            "reason": "The study does not request an energy equation.",
            "calculation": None,
        }
    domain = step.model.domain
    if not isinstance(domain, CircularPipe):
        return {
            "status": "deferred",
            "reason": (
                "Automatic heat-rate preflight currently requires circular-pipe "
                "wall area; imported and baffled surface areas remain explicit inputs."
            ),
            "calculation": None,
        }
    inlet = next(
        (
            condition
            for condition in step.model.boundary_conditions.values()
            if isinstance(condition, boundaries.Inlet)
        ),
        None,
    )
    if inlet is None or getattr(inlet, "temperature", None) is None:
        return {
            "status": "deferred",
            "reason": "A resolved inlet bulk temperature is required.",
            "calculation": None,
        }
    if isinstance(inlet, boundaries.MassFlowInlet):
        mean_velocity = inlet.mass_flow_rate / (
            step.model.fluid.density * domain.area
        )
    elif isinstance(
        inlet,
        (
            boundaries.MeanVelocityInlet,
            boundaries.FullyDevelopedVelocityInlet,
            boundaries.TurbulentMeanVelocityInlet,
        ),
    ):
        mean_velocity = inlet.velocity
    else:
        return {
            "status": "deferred",
            "reason": (
                "Pressure-driven or Cartesian imported flow requires a solved or "
                "explicit bulk flow rate before thermal screening."
            ),
            "calculation": None,
        }
    wall = step.model.boundary_conditions.get("wall")
    thermal = getattr(wall, "thermal", None)
    if isinstance(thermal, boundaries.AdiabaticWall):
        heat_rate = 0.0
    elif isinstance(thermal, boundaries.HeatFluxWall):
        heat_rate = (
            thermal.heat_flux_into_fluid
            * math.pi
            * domain.diameter
            * domain.length
        )
    else:
        return {
            "status": "deferred",
            "reason": (
                "Fixed wall temperature requires a solved wall heat rate; the plan "
                "does not invent a heat-transfer coefficient."
            ),
            "calculation": None,
        }
    fluid = step.model.fluid
    assert fluid.specific_heat is not None
    assert fluid.thermal_conductivity is not None
    calculation = engineering.screen_thermal_internal_flow(
        density=fluid.density,
        dynamic_viscosity=fluid.dynamic_viscosity,
        specific_heat=fluid.specific_heat,
        thermal_conductivity=fluid.thermal_conductivity,
        mean_velocity=mean_velocity,
        hydraulic_diameter=domain.hydraulic_diameter,
        flow_area=domain.area,
        inlet_bulk_temperature=inlet.temperature,
        heat_rate_into_fluid=heat_rate,
    ).to_dict()
    return {
        "status": "calculated",
        "reason": (
            "First-law mixed-mean estimate from declared flow, properties, and "
            "circular-pipe heat-flux area; this is not a CFD result."
        ),
        "calculation": calculation,
    }


def _human_bytes(value: int) -> str:
    for unit, divisor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= divisor:
            return f"{value / divisor:.2f} {unit}"
    return f"{value} B"


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Replace one managed JSON object without exposing a partial record."""

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _structured_project_action(action: Mapping[str, object]) -> dict[str, object]:
    """Add a bounded machine operation to a human-readable next command."""

    command = action.get("command")
    reason = action.get("reason")
    if not isinstance(command, str) or not isinstance(reason, str):
        raise ProjectError("Project next actions require command and reason strings.")
    try:
        tokens = shlex.split(command)
    except ValueError as error:
        raise ProjectError("Project next action is not shell-tokenizable.") from error
    operation = tokens[1] if len(tokens) >= 2 and tokens[0] == "agentcfd" else None
    policies = {
        "check": ("observe", False, False),
        "clean": ("maintain", True, False),
        "diagnose": ("observe", False, False),
        "logs": ("observe", False, False),
        "plan": ("observe", False, False),
        "result": ("review", False, False),
        "run": ("execute", True, True),
        "view": ("review", False, False),
        "watch": ("observe", False, False),
    }
    if operation not in policies:
        raise ProjectError(
            f"Project next action uses unsupported operation {operation!r}."
        )
    kind, mutates_project, starts_solver = policies[operation]
    return {
        "operation": operation,
        "kind": kind,
        "arguments": tokens[2:],
        "mutates_project": mutates_project,
        "starts_solver": starts_solver,
        "command": command,
        "reason": reason,
    }


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


def _windows_process_is_alive(pid: int) -> bool:
    """Query one Windows PID without using ``os.kill(pid, 0)``.

    Python maps ``os.kill`` to Windows console/termination semantics rather
    than the POSIX existence probe.  Calling it with signal zero can therefore
    interrupt the test runner or, worse, the process being observed.
    """

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        # EPERM proves that the process exists even though this observer cannot
        # signal it (common across managed execution boundaries).
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        return error.errno == errno.EPERM
    except ValueError:
        return False
    return True


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    return _posix_process_is_alive(pid)


_OPENFOAM_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_PERFORMANCE_HISTORY_LIMIT = 50
_PERFORMANCE_HISTORY_MAX_BYTES = 512 * 1024


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


def _elapsed_seconds(started_at: object, completed_at: object) -> float | None:
    try:
        start = datetime.fromisoformat(str(started_at))
        end = datetime.fromisoformat(str(completed_at))
    except (TypeError, ValueError):
        return None
    value = (end - start).total_seconds()
    return value if math.isfinite(value) and value >= 0.0 else None


def _performance_key(
    plan: Mapping[str, object],
    *,
    provider: str,
    result_profile: str,
) -> str:
    """Identify runtime-relevant intent without coupling ETA to exact flow values."""

    decisions = plan.get("decisions", {})
    model = plan.get("model", {})
    summary = model.get("summary", {}) if isinstance(model, Mapping) else {}
    domain = summary.get("domain") if isinstance(summary, Mapping) else None
    boundary_records = (
        summary.get("boundaries", {}) if isinstance(summary, Mapping) else {}
    )
    boundary_types = {
        str(name): {
            key: value
            for key, value in record.items()
            if key in {"type", "thermal"}
        }
        for name, record in (
            boundary_records.items() if isinstance(boundary_records, Mapping) else ()
        )
        if isinstance(record, Mapping)
    }
    reynolds = model.get("reynolds_number") if isinstance(model, Mapping) else None
    reynolds_band = (
        math.floor(math.log2(float(reynolds)))
        if isinstance(reynolds, (int, float))
        and not isinstance(reynolds, bool)
        and float(reynolds) > 0.0
        and math.isfinite(float(reynolds))
        else None
    )
    study = decisions.get("study") if isinstance(decisions, Mapping) else None
    output_plan = (
        decisions.get("output_plan", {}) if isinstance(decisions, Mapping) else {}
    )
    frames = (
        output_plan.get("channels", {}).get("field_frames")
        if isinstance(output_plan, Mapping)
        and isinstance(output_plan.get("channels"), Mapping)
        else None
    )
    provider_record = (
        decisions.get("provider", {}) if isinstance(decisions, Mapping) else {}
    )
    payload = {
        "provider": provider,
        "provider_version": (
            provider_record.get("version")
            if isinstance(provider_record, Mapping)
            else None
        ),
        "required_capability": (
            decisions.get("required_capability")
            if isinstance(decisions, Mapping)
            else None
        ),
        "solver": decisions.get("solver") if isinstance(decisions, Mapping) else None,
        "study": study,
        "procedure": (
            decisions.get("procedure") if isinstance(decisions, Mapping) else None
        ),
        "mesh": (
            decisions.get("mesh_intent") if isinstance(decisions, Mapping) else None
        ),
        "domain": domain,
        "boundary_types": boundary_types,
        "reynolds_factor_two_band": reynolds_band,
        "estimated_mesh_cells": (
            output_plan.get("estimated_mesh_cells")
            if isinstance(output_plan, Mapping)
            else None
        ),
        "field_frames": frames,
        "result_profile": result_profile,
    }
    return content_fingerprint(payload)


def _read_performance_history(root: Path) -> tuple[list[dict[str, object]], str]:
    path = root / ".agentcfd" / "performance.json"
    if not path.is_file():
        return [], "missing"
    try:
        if path.stat().st_size > _PERFORMANCE_HISTORY_MAX_BYTES:
            raise ValueError("AgentCFD performance history exceeds its size limit.")
        record = strict_json_object(
            path.read_text(encoding="utf-8"),
            label=f"AgentCFD performance history {path}",
        )
        samples = record.get("samples")
        if (
            record.get("schema") != "agentcfd.performance-history/0.1"
            or not isinstance(samples, list)
            or set(record)
            != {"schema", "updated_at", "maximum_samples", "samples"}
            or record.get("maximum_samples") != _PERFORMANCE_HISTORY_LIMIT
            or len(samples) > _PERFORMANCE_HISTORY_LIMIT
            or (
                record.get("updated_at") is not None
                and not isinstance(record.get("updated_at"), str)
            )
        ):
            raise ValueError("Unsupported AgentCFD performance-history record.")
        required = {
            "run_id",
            "recorded_at",
            "status",
            "accepted",
            "provider",
            "performance_key",
            "analysis_sha256",
            "result_profile",
            "model_name",
            "solver",
            "estimated_mesh_cells",
            "total_seconds",
        }
        valid = []
        for item in samples:
            if not isinstance(item, Mapping) or set(item) != required:
                raise ValueError("Performance history contains an invalid sample.")
            key = item.get("performance_key")
            duration = item.get("total_seconds")
            provider = item.get("provider")
            cells = item.get("estimated_mesh_cells")
            nullable_strings = (
                item.get("run_id"),
                item.get("recorded_at"),
                item.get("status"),
                item.get("analysis_sha256"),
                item.get("result_profile"),
                item.get("model_name"),
                item.get("solver"),
            )
            if (
                not isinstance(key, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", key) is None
                or not isinstance(provider, str)
                or not provider
                or not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(float(duration))
                or float(duration) < 0.0
                or any(
                    value is not None and not isinstance(value, str)
                    for value in nullable_strings
                )
                or (
                    item.get("accepted") is not None
                    and not isinstance(item.get("accepted"), bool)
                )
                or (
                    cells is not None
                    and (
                        not isinstance(cells, int)
                        or isinstance(cells, bool)
                        or cells < 1
                    )
                )
            ):
                raise ValueError("Performance history contains an invalid sample.")
            valid.append(dict(item))
    except (OSError, TypeError, ValueError):
        return [], "invalid"
    return valid[-_PERFORMANCE_HISTORY_LIMIT:], "valid"


def _calibrated_remaining(
    root: Path,
    record: Mapping[str, object],
    *,
    elapsed_seconds: float,
) -> dict[str, object] | None:
    key = record.get("performance_key")
    if not isinstance(key, str):
        return None
    samples, status = _read_performance_history(root)
    if status != "valid":
        return None
    durations = [
        float(item["total_seconds"])
        for item in samples
        if item.get("performance_key") == key
        and item.get("status") == "completed"
        and isinstance(item.get("total_seconds"), (int, float))
        and float(item["total_seconds"]) >= 0.0
    ]
    if not durations:
        return None
    median = statistics.median(durations)
    lower_total = min(durations) if len(durations) > 1 else median * 0.75
    upper_total = max(durations) if len(durations) > 1 else median * 1.5
    upper_remaining = max(1.0, upper_total - elapsed_seconds)
    if elapsed_seconds > upper_total:
        upper_remaining = max(upper_remaining, elapsed_seconds * 0.5)
    return {
        "minimum_seconds": round(max(0.0, lower_total - elapsed_seconds), 1),
        "maximum_seconds": round(upper_remaining, 1),
        "basis": f"bounded project history; {len(durations)} comparable run(s)",
    }


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
            values = [
                float(value)
                for value in stripped.replace("(", " ").replace(")", " ").split()
            ]
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
    current_command = (
        None if current_log is None else current_log.name.removeprefix("log.")
    )
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
    elapsed_seconds = _elapsed_seconds(started_at, completed_at or now.isoformat())

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
    if isinstance(elapsed_seconds, float):
        calibrated = _calibrated_remaining(
            root,
            record,
            elapsed_seconds=elapsed_seconds,
        )
        if calibrated is not None:
            remaining_range = calibrated

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
        "elapsed_seconds": None
        if elapsed_seconds is None
        else round(elapsed_seconds, 3),
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
            "display": None
            if workspace_bytes is None
            else _human_bytes(workspace_bytes),
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
    elif result.provenance.get("result_profile") == "summary-only":
        lines.extend(
            (
                "This campaign point intentionally keeps summaries and evidence only.",
                "Rerun the selected point without `--summary-only` to publish XDMF/HDF5 fields.",
                "",
            )
        )
    recipe_manifest = postprocessing.read_recipe_manifest(run.directory)
    if recipe_manifest is not None and recipe_manifest.get("recipes"):
        lines.extend(
            (
                "Reusable ParaView recipes are in `postprocess/`; launch one with "
                "`agentcfd view . --recipe NAME --launch`.",
                "These scripts share `fields/fields.h5` and do not duplicate volume data.",
                "",
            )
        )
    lines.extend(
        (
            "Read `result.json` for quantities, checks, histories, provenance, and artifacts.",
            "Read `plan.json` for the resolved modeling and output decisions.",
            "Read `run.json` for lifecycle state and machine automation.",
            "Run `agentcfd verify project .` before archive, coupling, or dataset handoff.",
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
    export_fields: bool = True,
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
            nx[0] * (ny[0] + ny[1]) + nx[1] * ny[1] + nx[2] * (ny[0] + ny[1])
        ) * nz
    elif (
        provider == "openfoam"
        and isinstance(step.model.domain, ImportedSurface)
        and step.mesh is not None
    ):
        # snappy refinement is geometry-dependent; use the public hard stop as
        # a conservative storage bound rather than inventing a precise count.
        estimated_cells = step.mesh.maximum_cells

    components = {
        "fluid.velocity": 3,
        "fluid.vorticity": 3,
    }
    scalar_components = sum(components.get(name, 1) for name in step.output.fields)
    if estimated_cells is None:
        estimated_portable_bytes = None
        estimated_temporary_peak_bytes = None
        estimate_calibration = None
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
        estimated_portable_bytes = (
            math.ceil(1.10 * (field_bytes + mesh_bytes) + 1024**2)
            if export_fields
            else 0
        )
        if export_fields and "npz" in step.output.portable_formats:
            estimated_portable_bytes += field_bytes + mesh_bytes
        # Full export stages solver-native and VTK data before publishing HDF5.
        # Summary-only still needs native solver frames during execution, but
        # neither VTK conversion nor a permanent portable field copy.
        raw_staging_bytes = (
            estimated_portable_bytes + field_bytes * 2
            if export_fields
            else field_bytes + mesh_bytes
        )
        temporary_peak_safety_factor = 1.25
        estimated_temporary_peak_bytes = math.ceil(
            temporary_peak_safety_factor * raw_staging_bytes
        )
        imported_bound = isinstance(step.model.domain, ImportedSurface)
        estimate_calibration = {
            "method": (
                "snappy-hard-cell-bound-plus-export-staging"
                if imported_bound and export_fields
                else "snappy-hard-cell-bound-native-only"
                if imported_bound
                else "native-plus-vtk-plus-portable-with-measured-headroom"
                if export_fields
                else "native-solver-only-summary-with-conservative-headroom"
            ),
            "uncompressed_requested_field_bytes": field_bytes,
            "raw_staging_bytes": raw_staging_bytes,
            "safety_factor": temporary_peak_safety_factor,
            "evidence": (
                "Imported geometry uses maximum_cells/maxGlobalCells as a fail-safe "
                "upper bound; the checked duct resolved 6,400 of 200,000 allowed cells."
                if imported_bound
                else "OpenCFD-v2606 baffled-channel 20-frame run: 122.05 MiB managed "
                "during retained-workspace publication versus 101.84 MiB raw estimate"
            ),
        }

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
            "views": {
                "definitions": [item.to_dict() for item in step.output.views],
                "retention": (
                    "scripts and state only; shared portable fields"
                    if export_fields
                    else "not published in summary-only result profile"
                ),
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
            None
            if estimated_portable_bytes is None
            else _human_bytes(estimated_portable_bytes)
        ),
        "estimated_temporary_peak_bytes": estimated_temporary_peak_bytes,
        "estimated_temporary_peak_display": (
            None
            if estimated_temporary_peak_bytes is None
            else _human_bytes(estimated_temporary_peak_bytes)
        ),
        "estimate_calibration": estimate_calibration,
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
        self.root = _discover_project_root(Path(root))
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
        self._factory_cache: tuple[str, object] | None = None

    @classmethod
    def discover(cls, start: str | Path = ".") -> "Project":
        """Open the nearest AgentCFD project at or above ``start``."""

        return cls(start)

    def _cli_project_argument(self) -> str:
        """Use a short relative argument whenever the current directory is inside."""

        try:
            Path.cwd().resolve().relative_to(self.root)
        except ValueError:
            return shlex.quote(str(self.root))
        return "."

    @staticmethod
    def _parameters(
        values: Mapping[str, object] | None,
    ) -> dict[str, str | int | float | bool | None]:
        selected: dict[str, str | int | float | bool | None] = {}
        for name, value in dict(values or {}).items():
            if (
                not isinstance(name, str)
                or not name.isidentifier()
                or keyword.iskeyword(name)
            ):
                raise ProjectError(
                    "Project parameter names must be valid non-keyword Python identifiers."
                )
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ProjectError(
                    f"Project parameter {name!r} must be a JSON scalar."
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise ProjectError(f"Project parameter {name!r} must be finite.")
            selected[name] = value
        return dict(sorted(selected.items()))

    def _factory(self):
        entrypoint_sha256 = file_sha256(self.entrypoint)
        if (
            self._factory_cache is not None
            and self._factory_cache[0] == entrypoint_sha256
        ):
            return self._factory_cache[1]
        module = _load_module(self.entrypoint, self.root)
        factory = getattr(module, self.manifest.factory, None)
        if not callable(factory):
            raise ProjectError(
                f"Project entrypoint must define callable {self.manifest.factory}()."
            )
        self._factory_cache = (entrypoint_sha256, factory)
        return factory

    @staticmethod
    def _parameter_specs(factory) -> dict[str, parameter_definitions.ParameterSpec]:
        declared = getattr(factory, "__agentcfd_parameter_specs__", {})
        if not isinstance(declared, Mapping):
            raise ProjectError("Project factory parameter metadata must be a mapping.")
        signature_names = set(inspect.signature(factory).parameters)
        unknown = sorted(set(declared) - signature_names)
        if unknown:
            raise ProjectError(
                "Project parameter metadata targets unknown inputs: "
                + ", ".join(unknown)
                + "."
            )
        invalid = sorted(
            name
            for name, specification in declared.items()
            if not isinstance(specification, parameter_definitions.ParameterSpec)
        )
        if invalid:
            raise ProjectError(
                "Invalid project parameter metadata: " + ", ".join(invalid) + "."
            )
        return dict(declared)

    def parameter_contract(
        self,
        selected: Mapping[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """Describe the editable ``case.py`` factory surface without solving.

        Defaults remain owned by Python. This record gives humans, agents, and
        future GUIs the same bounded discovery surface used by ``--param``.
        """

        factory = self._factory()
        selected_parameters = self._parameters(selected)
        declared = self._parameter_specs(factory)
        records: list[dict[str, object]] = []
        for parameter in inspect.signature(factory).parameters.values():
            specification = declared.get(parameter.name)
            keyword_overrideable = parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
            has_default = parameter.default is not inspect.Parameter.empty
            default = parameter.default if has_default else None
            default_is_scalar = isinstance(
                default, (str, int, float, bool, type(None))
            ) and not (isinstance(default, float) and not math.isfinite(default))
            overrideable = keyword_overrideable and (not has_default or default_is_scalar)
            selected_here = parameter.name in selected_parameters
            current = (
                selected_parameters[parameter.name]
                if selected_here
                else default
                if default_is_scalar
                else None
            )
            records.append(
                {
                    "name": parameter.name,
                    "required": not has_default,
                    "default": default if default_is_scalar else None,
                    "default_type": (
                        "none"
                        if default is None and default_is_scalar
                        else type(default).__name__
                        if default_is_scalar
                        else None
                    ),
                    "overrideable": overrideable,
                    "selected": selected_here,
                    "current": current,
                    "input_contract": "json-scalar" if overrideable else None,
                    "metadata": (
                        None if specification is None else specification.to_dict()
                    ),
                }
            )
        return records

    def parameter_set(
        self,
        selected: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Freeze one validated operating point as portable JSON scalars."""

        selected_parameters = self._parameters(selected)
        self.load_step(selected_parameters)
        records = self.parameter_contract(selected_parameters)
        resolved = {
            str(record["name"]): record["current"]
            for record in records
            if record["overrideable"] is True
        }
        return {
            "schema": "agentcfd.parameter-set/0.1",
            "parameters": resolved,
        }

    def load_step(self, parameters: Mapping[str, object] | None = None) -> Step:
        factory = self._factory()
        selected_parameters = self._parameters(parameters)
        try:
            inspect.signature(factory).bind(**selected_parameters)
        except TypeError as error:
            supplied = ", ".join(selected_parameters) or "no explicit parameters"
            raise ProjectError(
                f"Project factory rejected parameter selection ({supplied}): {error}"
            ) from error
        specifications = self._parameter_specs(factory)
        for name, value in selected_parameters.items():
            specification = specifications.get(name)
            if specification is None:
                continue
            try:
                specification.validate(value, name=name)
            except ValueError as error:
                raise ProjectError(
                    f"Project parameter {name!r} violates its declared contract: "
                    f"{error}"
                ) from error
        try:
            step = factory(**selected_parameters)
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
            "timeout_seconds",
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
        timeout_seconds = float(settings.get("timeout_seconds", 3600.0))
        if step is not None and isinstance(step.model.domain, ImportedSurface):
            source = _safe_project_path(
                self.root,
                step.model.domain.asset,
                label="imported surface asset",
            )
            return OpenFOAMImportedProvider(
                source=source,
                case_directory=case_directory,
                mesh_cache_directory=self.root / ".agentcfd" / "mesh-cache",
                container_image=str(selected_image) if selected_image else None,
                timeout_seconds=timeout_seconds,
            )
        if step is not None and isinstance(step.model.domain, RectangularChannel):
            return OpenFOAMChannelProvider(
                case_directory=case_directory,
                container_image=str(selected_image) if selected_image else None,
                timeout_seconds=timeout_seconds,
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
            timeout_seconds=timeout_seconds,
        )

    def _execution_fingerprint(
        self,
        analysis_sha256: object,
        *,
        provider: str,
        container_image: str | None = None,
        portable_fields: bool | None = None,
    ) -> str:
        settings: dict[str, object] = {}
        if provider == "openfoam":
            settings = self._openfoam_settings()
            if portable_fields is not None:
                settings["export_fields"] = portable_fields
            if container_image is not None:
                settings["container_image"] = container_image
        return content_fingerprint(
            {
                "analysis_sha256": analysis_sha256,
                "provider": provider,
                "provider_settings": settings,
            }
        )

    def _resume_execution_fingerprint(
        self,
        analysis_sha256: object,
        *,
        provider: str,
        container_image: str | None = None,
    ) -> str:
        """Fingerprint solver-affecting settings while excluding execution limits."""

        settings: dict[str, object] = {}
        if provider == "openfoam":
            settings = self._openfoam_settings()
            for operational in (
                "timeout_seconds",
                "keep_workspace",
                "export_fields",
            ):
                settings.pop(operational, None)
            if container_image is not None:
                settings["container_image"] = container_image
        return content_fingerprint(
            {
                "analysis_sha256": analysis_sha256,
                "provider": provider,
                "solver_affecting_provider_settings": settings,
            }
        )

    def _result_execution_fingerprint(
        self,
        analysis_sha256: object,
        *,
        provider: str,
        container_image: str | None = None,
        portable_fields: bool | None = None,
    ) -> str:
        """Fingerprint settings that can change the published result surface."""

        settings: dict[str, object] = {}
        if provider == "openfoam":
            settings = self._openfoam_settings()
            if portable_fields is not None:
                settings["export_fields"] = portable_fields
            settings.pop("timeout_seconds", None)
            settings.pop("keep_workspace", None)
            if container_image is not None:
                settings["container_image"] = container_image
        return content_fingerprint(
            {
                "analysis_sha256": analysis_sha256,
                "provider": provider,
                "result_affecting_provider_settings": settings,
            }
        )

    def plan(
        self,
        *,
        provider: str | None = None,
        container_image: str | None = None,
        parameters: Mapping[str, object] | None = None,
        portable_fields: bool | None = None,
        _step: Step | None = None,
    ) -> dict[str, object]:
        selected_name = provider or self.manifest.default_provider
        if selected_name not in {"reference", "openfoam"}:
            raise ProjectError("Provider must be 'reference' or 'openfoam'.")
        if portable_fields is not None and not isinstance(portable_fields, bool):
            raise ProjectError("Portable fields override must be true, false, or null.")
        selected_parameters = self._parameters(parameters)
        step = _step or self.load_step(selected_parameters)
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
        if isinstance(step.model.domain, ImportedSurface):
            required_capability = (
                "openfoam.steady-laminar-imported-surface"
                if study.laminar
                else "openfoam.steady-rans-imported-surface"
            )
        elif selected_name == "reference":
            required_capability = "reference.hagen-poiseuille"
        elif isinstance(step.model.domain, RectangularChannel):
            required_capability = "openfoam.transient-laminar-baffled-channel"
        else:
            required_capability = (
                "openfoam.steady-laminar-heated-circular-pipe"
                if study.energy
                else
                "openfoam.steady-laminar-circular-pipe"
                if study.laminar
                else "openfoam.steady-rans-smooth-circular-pipe"
            )
        mesh_intent_ready = True
        imported_mesh_plan = None
        if isinstance(step.model.domain, ImportedSurface) and model_valid:
            try:
                imported_mesh_plan = plan_imported_mesh(step).to_dict()
            except UnsupportedCaseError as error:
                mesh_intent_ready = False
                issues.append(
                    ProjectIssue(
                        "IMPORTED_MESH_INTENT_UNSUPPORTED",
                        "error",
                        str(error),
                        "case.py:mesh",
                        "Declare supported automatic mesh intent, an interior point, and a hard cell budget.",
                    )
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

        input_assets_ready = True
        if isinstance(step.model.domain, ImportedSurface):
            asset = _safe_project_path(
                self.root,
                step.model.domain.asset,
                label="imported surface asset",
            )
            if not asset.is_file():
                input_assets_ready = False
                issues.append(
                    ProjectIssue(
                        "IMPORTED_GEOMETRY_MISSING",
                        "error",
                        f"Imported geometry asset is missing: {step.model.domain.asset}.",
                        f"case.py:domain.asset",
                        "Restore the content-addressed project-relative geometry asset.",
                    )
                )
            elif "sha256:" + file_sha256(asset) != step.model.domain.source_sha256:
                input_assets_ready = False
                issues.append(
                    ProjectIssue(
                        "IMPORTED_GEOMETRY_CHANGED",
                        "error",
                        "Imported geometry bytes differ from the inspected source hash.",
                        f"case.py:domain.source_sha256",
                        "Reinspect the asset and explicitly update model intent.",
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
        export_fields = selected_name == "openfoam" and (
            bool(self._openfoam_settings().get("export_fields", True))
            if portable_fields is None
            else portable_fields
        )
        try:
            output_plan = _resolved_output_plan(
                step,
                provider=selected_name,
                openfoam=self._openfoam_settings(),
                export_fields=export_fields,
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
            and input_assets_ready
            and mesh_intent_ready
        )
        decisions = {
            "study": study.to_dict(),
            "procedure": step.procedure.to_dict(),
            "outputs": step.output.to_dict(),
            "initialization": None
            if step.initialization is None
            else step.initialization.to_dict(),
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
                else "simpleFoam + scalarTransport(T)"
                if selected_name == "openfoam" and study.energy
                else "pimpleFoam"
                if selected_name == "openfoam" and not study.steady
                else "simpleFoam"
                if selected_name == "openfoam"
                else "Hagen-Poiseuille"
            ),
            "portable_field_bundle": export_fields,
            "result_profile": (
                "summary-only"
                if selected_name == "openfoam" and not export_fields
                else "full-fields"
            ),
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
            "imported_mesh_plan": imported_mesh_plan,
            "thermal_preflight": (
                _thermal_preflight(step)
                if model_valid
                else {
                    "status": "deferred",
                    "reason": "Thermal preflight requires a valid model.",
                    "calculation": None,
                }
            ),
        }
        plan: dict[str, object] = {
            "schema": "agentcfd.solution-plan/0.1",
            "project": {
                "root": str(self.root),
                "entrypoint": self.manifest.entrypoint,
                "entrypoint_sha256": file_sha256(self.entrypoint),
                "factory": self.manifest.factory,
                "parameters": selected_parameters,
                "factory_parameters": self.parameter_contract(selected_parameters),
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
                "input_assets_ready": input_assets_ready,
                "mesh_intent_ready": mesh_intent_ready,
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

    def _select_run_record(
        self, run_id: str | None = None
    ) -> dict[str, object] | None:
        """Select the latest run or one immutable run without opening result payloads."""

        runs = self._run_records()
        if run_id is None:
            return runs[0] if runs else None
        if not isinstance(run_id, str) or not run_id.strip():
            raise ProjectError("Run id must be a non-empty string.")
        selected = next(
            (record for record in runs if record.get("run_id") == run_id),
            None,
        )
        if selected is None:
            raise ProjectError(
                f"No project run exists with run id {run_id!r}. "
                "Use `agentcfd campaigns .` or `agentcfd status .` to list runs."
            )
        return selected

    def campaign_index(self, *, include_storage: bool = False) -> dict[str, object]:
        """Summarize immutable design points without opening any field payload."""

        campaign_root = self.root / "campaigns"
        rows: list[dict[str, object]] = []
        plan_files_opened = 0
        markers = (
            sorted(campaign_root.glob("*/run.json"))
            if campaign_root.is_dir()
            else []
        )
        for marker in markers:
            try:
                record = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            directory = marker.parent
            model_name = record.get("model_name")
            reynolds_number = record.get("reynolds_number")
            if not isinstance(model_name, str):
                try:
                    plan = json.loads(
                        (directory / "plan.json").read_text(encoding="utf-8")
                    )
                    plan_files_opened += 1
                    model = plan.get("model", {})
                    if isinstance(model, dict):
                        model_name = model.get("name")
                        reynolds_number = model.get("reynolds_number")
                except (OSError, json.JSONDecodeError):
                    pass
            started_at = record.get("started_at")
            completed_at = record.get("completed_at")
            duration_seconds = None
            try:
                if started_at is not None and completed_at is not None:
                    duration_seconds = max(
                        0.0,
                        (
                            datetime.fromisoformat(str(completed_at))
                            - datetime.fromisoformat(str(started_at))
                        ).total_seconds(),
                    )
            except ValueError:
                pass
            quantities = record.get("quantities", {})
            if not isinstance(quantities, dict):
                quantities = {}
            parameters = record.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}
            size = files = None
            if include_storage:
                size, files = _tree_usage(directory)
            rows.append(
                {
                    "run_id": record.get("run_id", directory.name),
                    "design_point_name": record.get("design_point_name"),
                    "status": record.get("status", "unknown"),
                    "accepted": record.get("accepted"),
                    "trust_level": record.get("trust_level"),
                    "provider": record.get("provider"),
                    "result_profile": record.get("result_profile", "full-fields"),
                    "model_name": model_name,
                    "analysis_sha256": record.get("analysis_sha256"),
                    "reynolds_number": reynolds_number,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "duration_seconds": duration_seconds,
                    "directory": str(directory),
                    "result": str(directory / "result.json")
                    if (directory / "result.json").is_file()
                    else None,
                    "quantities": quantities,
                    "parameters": parameters,
                    "bytes": size,
                    "display": None if size is None else _human_bytes(size),
                    "file_count": files,
                }
            )
        total_bytes = (
            sum(int(row["bytes"]) for row in rows if row["bytes"] is not None)
            if include_storage
            else None
        )
        return {
            "schema": "agentcfd.campaign-index/0.1",
            "root": str(self.root),
            "run_count": len(rows),
            "accepted_count": sum(row["accepted"] is True for row in rows),
            "failed_count": sum(row["status"] == "failed" for row in rows),
            "include_storage": include_storage,
            "total_bytes": total_bytes,
            "total_display": None
            if total_bytes is None
            else _human_bytes(total_bytes),
            "exported_csv": None,
            "runs": rows,
            "observation_cost": {
                "run_markers_opened": len(markers),
                "plan_files_opened": plan_files_opened,
                "result_manifests_opened": 0,
                "field_payloads_opened": 0,
                "recursive_storage_scans": len(rows) if include_storage else 0,
            },
        }

    def result_summary(
        self,
        *,
        run_id: str | None = None,
        quantities: Sequence[str] = (),
    ) -> dict[str, object]:
        """Return compact result metadata without opening external field payloads."""

        selected_run = self._select_run_record(run_id)
        if selected_run is None:
            raise ProjectError("No project result exists; run the project first.")
        run_directory = self._record_directory(selected_run)
        if run_directory is None:
            raise ProjectError("The selected run directory cannot be resolved.")
        result_path = run_directory / "result.json"
        if not result_path.is_file():
            raise ProjectError(
                "The selected run has no compact result.json; rerun or diagnose it."
            )
        record = read_result_record(result_path, verify_artifacts=False)
        available_quantities = record["quantities"]
        requested = tuple(dict.fromkeys(str(name).strip() for name in quantities))
        if any(not name for name in requested):
            raise ProjectError("Result quantity names must not be empty.")
        unknown = sorted(set(requested) - set(available_quantities))
        if unknown:
            available = ", ".join(sorted(available_quantities)) or "none"
            raise ProjectError(
                "Unknown result quantities: "
                + ", ".join(unknown)
                + f". Available: {available}."
            )
        selected_quantities = (
            {name: available_quantities[name] for name in requested}
            if requested
            else dict(available_quantities)
        )
        fields = record.get("fields", {})
        histories = record.get("histories", {})
        checks = record.get("checks", [])
        project_argument = self._cli_project_argument()
        verification_command = (
            f"agentcfd verify result {shlex.quote(str(result_path))}"
        )
        return {
            "schema": "agentcfd.result-summary/0.1",
            "root": str(self.root),
            "run_id": selected_run.get("run_id"),
            "result": str(result_path),
            "status": record["status"],
            "converged": record["converged"],
            "accepted": record["accepted"],
            "trust_level": record["trust_level"],
            "provider": record["provider"],
            "parameters": selected_run.get("parameters", {}),
            "quantities": selected_quantities,
            "histories": histories,
            "fields": fields,
            "available": {
                "quantities": sorted(available_quantities),
                "histories": sorted(histories),
                "fields": sorted(fields),
            },
            "failed_checks": [check for check in checks if not check["passed"]],
            "provenance": record.get("provenance", {}),
            "artifact_integrity": {
                "verified": False,
                "reason": "External artifacts were not opened by this lightweight view.",
                "command": verification_command,
            },
            "observation_cost": {
                "result_json_bytes_read": result_path.stat().st_size,
                "field_payloads_opened": 0,
                "artifacts_hashed": 0,
            },
            "next_action": {
                "command": (
                    f"agentcfd view {project_argument}"
                    if run_id is None
                    else verification_command
                ),
                "reason": (
                    "Open the accepted result for spatial review."
                    if record["accepted"]
                    else "Review the failed checks before using this result."
                ),
            },
        }

    def export_campaign_csv(
        self,
        path: str | Path,
        *,
        include_storage: bool = False,
    ) -> tuple[Path, dict[str, object]]:
        """Export the compact design-point table with canonical quantity columns."""

        report = self.campaign_index(include_storage=include_storage)
        quantity_names = sorted(
            {
                name
                for row in report["runs"]
                for name in row["quantities"]
                if isinstance(name, str)
            }
        )
        parameter_names = sorted(
            {
                name
                for row in report["runs"]
                for name in row["parameters"]
                if isinstance(name, str)
            }
        )
        units = {
            name: next(
                (
                    str(row["quantities"][name].get("unit") or "1")
                    for row in report["runs"]
                    if isinstance(row["quantities"].get(name), dict)
                ),
                "1",
            )
            for name in quantity_names
        }
        base_columns = [
            "run_id",
            "design_point_name",
            "status",
            "accepted",
            "trust_level",
            "provider",
            "result_profile",
            "model_name",
            "analysis_sha256",
            "reynolds_number",
            "duration_seconds",
            "completed_at",
        ]
        if include_storage:
            base_columns.extend(["bytes", "file_count"])
        quantity_columns = {
            name: f"quantity:{name} [{units[name]}]" for name in quantity_names
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    *base_columns,
                    *(f"parameter:{name}" for name in parameter_names),
                    *quantity_columns.values(),
                ],
            )
            writer.writeheader()
            for row in report["runs"]:
                flat = {name: row.get(name) for name in base_columns}
                for name in parameter_names:
                    flat[f"parameter:{name}"] = row["parameters"].get(name)
                for name, column in quantity_columns.items():
                    record = row["quantities"].get(name)
                    flat[column] = (
                        record.get("value") if isinstance(record, dict) else None
                    )
                writer.writerow(flat)
        temporary.replace(target)
        report["exported_csv"] = str(target)
        return target, report

    def _reusable_campaign_records(self) -> dict[str, dict[str, object]]:
        """Index accepted campaign markers whose compact result still exists."""

        reusable = {}
        for record in self._run_records():
            identity = record.get("result_execution_sha256")
            directory = self._record_directory(record)
            if (
                record.get("mode") == "campaign"
                and record.get("status") == "completed"
                and record.get("accepted") is True
                and isinstance(identity, str)
                and directory is not None
                and (directory / "result.json").is_file()
            ):
                reusable[identity] = record
        return reusable

    def plan_campaign(
        self,
        points: Mapping[str, Mapping[str, object]],
        *,
        provider: str | None = None,
        container_image: str | None = None,
        summary_only: bool = False,
    ) -> dict[str, object]:
        """Preview campaign readiness and reuse without starting a solver."""

        if not isinstance(points, Mapping) or not points:
            raise ProjectError("A campaign sweep requires at least one named point.")
        selected_provider = provider or self.manifest.default_provider
        reusable = self._reusable_campaign_records()
        rows = []
        planned_identities: set[str] = set()
        for name, raw_parameters in points.items():
            parameters: dict[str, object] = {}
            try:
                if (
                    not isinstance(name, str)
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) is None
                ):
                    raise ProjectError(
                        "Point names must start with a letter and contain only letters, "
                        "numbers, underscores, or hyphens."
                    )
                parameters = self._parameters(raw_parameters)
                plan = self.plan(
                    provider=selected_provider,
                    container_image=container_image,
                    parameters=parameters,
                    portable_fields=False if summary_only else None,
                )
            except ProjectError as error:
                rows.append(
                    {
                        "name": str(name),
                        "parameters": parameters,
                        "ready": False,
                        "issues": [],
                        "plan_sha256": None,
                        "result_execution_sha256": None,
                        "reusable": False,
                        "reuse_source": None,
                        "source_run_id": None,
                        "error": str(error),
                    }
                )
                continue
            ready = plan["readiness"]["ready_to_run"] is True
            identity = self._result_execution_fingerprint(
                plan["model"]["analysis_sha256"],
                provider=selected_provider,
                container_image=container_image,
                portable_fields=False if summary_only else None,
            )
            cached = reusable.get(identity)
            request_duplicate = identity in planned_identities
            is_reusable = ready and (cached is not None or request_duplicate)
            rows.append(
                {
                    "name": name,
                    "parameters": parameters,
                    "ready": ready,
                    "issues": plan["issues"],
                    "plan_sha256": plan["plan_sha256"],
                    "result_execution_sha256": identity,
                    "reusable": is_reusable,
                    "reuse_source": (
                        "existing-campaign"
                        if cached is not None
                        else "request-duplicate"
                        if request_duplicate
                        else None
                    ),
                    "source_run_id": None if cached is None else cached.get("run_id"),
                    "error": None,
                }
            )
            if ready:
                planned_identities.add(identity)
        all_ready = all(row["ready"] is True for row in rows)
        reusable_count = sum(row["reusable"] is True for row in rows)
        return {
            "schema": "agentcfd.campaign-plan/0.1",
            "root": str(self.root),
            "provider": selected_provider,
            "result_profile": (
                "summary-only"
                if summary_only and selected_provider == "openfoam"
                else "full-fields"
            ),
            "point_count": len(rows),
            "ready_count": sum(row["ready"] is True for row in rows),
            "reusable_count": reusable_count,
            "would_execute_count": len(rows) - reusable_count if all_ready else 0,
            "all_ready": all_ready,
            "points": rows,
            "observation_cost": {
                "result_manifests_opened": 0,
                "field_payloads_opened": 0,
                "solver_processes_started": 0,
            },
        }

    def run_campaign(
        self,
        points: Mapping[str, Mapping[str, object]],
        *,
        provider: str | None = None,
        container_image: str | None = None,
        fail_fast: bool = False,
        maximum_solver_runs: int | None = None,
        summary_only: bool = False,
    ) -> dict[str, object]:
        """Preflight and execute named design points, reusing accepted identities."""

        if not isinstance(points, Mapping) or not points:
            raise ProjectError("A campaign sweep requires at least one named point.")
        if maximum_solver_runs is not None and (
            isinstance(maximum_solver_runs, bool)
            or not isinstance(maximum_solver_runs, int)
            or maximum_solver_runs < 0
        ):
            raise ProjectError("Maximum solver runs must be a non-negative integer.")
        selected_provider = provider or self.manifest.default_provider
        prepared = []
        for name, raw_parameters in points.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) is None
            ):
                raise ProjectError(
                    "Campaign point names must start with a letter and contain only "
                    "letters, numbers, underscores, or hyphens."
                )
            parameters = self._parameters(raw_parameters)
            plan = self.plan(
                provider=selected_provider,
                container_image=container_image,
                parameters=parameters,
                portable_fields=False if summary_only else None,
            )
            if plan["readiness"]["ready_to_run"] is not True:
                codes = ", ".join(issue["code"] for issue in plan["issues"])
                raise ProjectError(
                    f"Campaign point {name!r} is not ready: {codes or 'unknown issue'}. "
                    "No design point was executed."
                )
            identity = self._result_execution_fingerprint(
                plan["model"]["analysis_sha256"],
                provider=selected_provider,
                container_image=container_image,
                portable_fields=False if summary_only else None,
            )
            prepared.append((name, parameters, plan, identity))

        reusable = self._reusable_campaign_records()
        planned_new_runs = len(
            {
                identity
                for _name, _parameters, _plan, identity in prepared
                if identity not in reusable
            }
        )
        if (
            maximum_solver_runs is not None
            and planned_new_runs > maximum_solver_runs
        ):
            raise ProjectError(
                f"Campaign would start {planned_new_runs} solver processes, exceeding "
                f"the explicit --max-runs {maximum_solver_runs} budget. No design "
                "point was executed; inspect `agentcfd sweep . REQUEST --plan-only`."
            )
        progress_path = self.root / "campaigns" / "last-sweep.json"
        rows: list[dict[str, object]] = []
        request_results: dict[str, dict[str, object]] = {}

        def report() -> dict[str, object]:
            processed = len(rows)
            accepted = sum(row["accepted"] is True for row in rows)
            failed = sum(row["outcome"] == "failed" for row in rows)
            unaccepted = sum(row["outcome"] == "review" for row in rows)
            return {
                "schema": "agentcfd.campaign-sweep/0.1",
                "root": str(self.root),
                "provider": selected_provider,
                "result_profile": (
                    "summary-only"
                    if summary_only and selected_provider == "openfoam"
                    else "full-fields"
                ),
                "requested_count": len(prepared),
                "processed_count": processed,
                "executed_count": sum(row["execution"] == "executed" for row in rows),
                "reused_count": sum(row["execution"] == "reused" for row in rows),
                "deduplicated_count": sum(
                    row["execution"] == "deduplicated" for row in rows
                ),
                "accepted_count": accepted,
                "failed_count": failed,
                "unaccepted_count": unaccepted,
                "complete": processed == len(prepared),
                "successful": processed == len(prepared) and accepted == len(prepared),
                "progress": str(progress_path),
                "points": rows,
                "solver_budget": {
                    "maximum_runs": maximum_solver_runs,
                    "planned_new_runs": planned_new_runs,
                    "solver_processes_started": sum(
                        row["execution"] == "executed" for row in rows
                    ),
                },
                "observation_cost": {
                    "result_manifests_opened": 0,
                    "field_payloads_opened": 0,
                },
            }

        def write_progress() -> None:
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = progress_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(report(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(progress_path)

        write_progress()
        for name, parameters, plan, identity in prepared:
            cached = reusable.get(identity)
            if cached is not None:
                rows.append(
                    {
                        "name": name,
                        "parameters": parameters,
                        "plan_sha256": plan["plan_sha256"],
                        "result_execution_sha256": identity,
                        "execution": "reused",
                        "outcome": "accepted",
                        "run_id": cached.get("run_id"),
                        "directory": cached.get("directory"),
                        "accepted": True,
                        "diagnose_command": None,
                        "error": None,
                    }
                )
            elif identity in request_results:
                source = request_results[identity]
                rows.append(
                    {
                        "name": name,
                        "parameters": parameters,
                        "plan_sha256": plan["plan_sha256"],
                        "result_execution_sha256": identity,
                        "execution": "deduplicated",
                        "outcome": source["outcome"],
                        "run_id": source["run_id"],
                        "directory": source["directory"],
                        "accepted": source["accepted"],
                        "diagnose_command": source["diagnose_command"],
                        "error": source["error"],
                    }
                )
            else:
                try:
                    completed = self.run(
                        provider=selected_provider,
                        container_image=container_image,
                        campaign=True,
                        parameters=parameters,
                        design_point_name=name,
                        portable_fields=False if summary_only else None,
                    )
                except Exception as error:
                    failed_record = next(
                        (
                            record
                            for record in self._run_records()
                            if record.get("design_point_name") == name
                            and record.get("parameters") == parameters
                            and record.get("result_execution_sha256") == identity
                            and record.get("status") == "failed"
                        ),
                        None,
                    )
                    failed_run_id = (
                        failed_record.get("run_id")
                        if failed_record is not None
                        else None
                    )
                    failed_directory = self._record_directory(failed_record)
                    project_argument = self._cli_project_argument()
                    row = {
                            "name": name,
                            "parameters": parameters,
                            "plan_sha256": plan["plan_sha256"],
                            "result_execution_sha256": identity,
                            "execution": "executed",
                            "outcome": "failed",
                            "run_id": failed_run_id,
                            "directory": (
                                None
                                if failed_directory is None
                                else str(failed_directory)
                            ),
                            "accepted": False,
                            "diagnose_command": (
                                None
                                if not isinstance(failed_run_id, str)
                                else "agentcfd diagnose "
                                f"{project_argument} --run-id "
                                f"{shlex.quote(failed_run_id)}"
                            ),
                            "error": {
                                "type": type(error).__name__,
                                "message": str(error),
                            },
                        }
                    rows.append(row)
                    request_results[identity] = row
                    write_progress()
                    if fail_fast:
                        break
                    continue
                accepted = completed.result.accepted
                outcome = (
                    "accepted"
                    if accepted
                    else "failed"
                    if completed.result.status != "completed"
                    else "review"
                )
                project_argument = self._cli_project_argument()
                row = {
                    "name": name,
                    "parameters": parameters,
                    "plan_sha256": plan["plan_sha256"],
                    "result_execution_sha256": identity,
                    "execution": "executed",
                    "outcome": outcome,
                    "run_id": completed.run_id,
                    "directory": str(completed.directory),
                    "accepted": accepted,
                    "diagnose_command": (
                        "agentcfd diagnose "
                        f"{project_argument} --run-id {shlex.quote(completed.run_id)}"
                        if outcome == "failed"
                        else None
                    ),
                    "error": None,
                }
                rows.append(row)
                request_results[identity] = row
                if accepted:
                    reusable[identity] = {
                        "run_id": completed.run_id,
                        "directory": str(completed.directory),
                    }
            write_progress()
        return report()

    def promote_campaign_run(
        self,
        run_id: str,
        *,
        container_image: str | None = None,
    ) -> dict[str, object]:
        """Publish full fields for one accepted summary-only campaign point."""

        source = self._select_run_record(run_id)
        assert source is not None
        if source.get("mode") != "campaign":
            raise ProjectError("Only immutable campaign runs can be promoted.")
        if source.get("result_profile") != "summary-only":
            raise ProjectError(
                f"Run {run_id!r} is not summary-only; no field promotion is needed."
            )
        if source.get("accepted") is not True:
            raise ProjectError(
                f"Run {run_id!r} is not accepted; review its checks before promotion."
            )
        if self.manifest.default_provider != "openfoam":
            raise ProjectError("Full-field campaign promotion requires OpenFOAM.")
        parameters = source.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ProjectError("Source campaign parameters are missing or malformed.")
        selected_parameters = self._parameters(parameters)
        plan = self.plan(
            provider="openfoam",
            container_image=container_image,
            parameters=selected_parameters,
            portable_fields=True,
        )
        if plan["model"]["analysis_sha256"] != source.get("analysis_sha256"):
            raise ProjectError(
                "Current case.py no longer matches the summary-only source. Restore "
                "the source model before promoting fields."
            )
        if plan["readiness"]["ready_to_run"] is not True:
            codes = ", ".join(issue["code"] for issue in plan["issues"])
            raise ProjectError(
                f"Full-field promotion is not ready: {codes or 'unknown issue'}."
            )
        identity = self._result_execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider="openfoam",
            container_image=container_image,
            portable_fields=True,
        )
        cached = self._reusable_campaign_records().get(identity)
        source_directory = self._record_directory(source)
        if cached is not None:
            target_run_id = cached.get("run_id")
            target_directory = self._record_directory(cached)
            execution = "reused"
            accepted = True
            solver_processes_started = 0
        else:
            source_name = source.get("design_point_name")
            target_name = (
                f"{source_name}-full"
                if isinstance(source_name, str) and source_name
                else "promoted-full"
            )
            completed = self.run(
                provider="openfoam",
                container_image=container_image,
                campaign=True,
                parameters=selected_parameters,
                design_point_name=target_name,
                portable_fields=True,
                _promotion_source_run_id=run_id,
            )
            target_run_id = completed.run_id
            target_directory = completed.directory
            execution = "executed"
            accepted = completed.result.accepted
            solver_processes_started = 1
        return {
            "schema": "agentcfd.campaign-promotion/0.1",
            "root": str(self.root),
            "source": {
                "run_id": run_id,
                "directory": (
                    None if source_directory is None else str(source_directory)
                ),
                "result_profile": "summary-only",
                "parameters": selected_parameters,
            },
            "target": {
                "run_id": target_run_id,
                "directory": (
                    None if target_directory is None else str(target_directory)
                ),
                "result_profile": "full-fields",
                "result_execution_sha256": identity,
                "plan_sha256": plan["plan_sha256"],
                "accepted": accepted,
            },
            "execution": execution,
            "successful": accepted is True,
            "observation_cost": {
                "field_payloads_opened": 0,
                "solver_processes_started": solver_processes_started,
            },
        }

    def compact_campaign_run(
        self,
        run_id: str,
        *,
        apply: bool = False,
    ) -> dict[str, object]:
        """Preview or remove reproducible full-field bulk from one campaign run."""

        if not isinstance(apply, bool):
            raise ProjectError("Campaign compaction apply flag must be boolean.")
        source = self._select_run_record(run_id)
        assert source is not None
        if source.get("mode") != "campaign":
            raise ProjectError("Only immutable campaign runs can be compacted.")
        if source.get("status") != "completed" or source.get("accepted") is not True:
            raise ProjectError("Only completed, accepted campaign runs can be compacted.")
        if source.get("result_profile", "full-fields") != "full-fields":
            raise ProjectError(f"Run {run_id!r} is already summary-only.")
        if self.manifest.default_provider != "openfoam":
            raise ProjectError("Field compaction currently requires an OpenFOAM project.")
        run_directory = self._record_directory(source)
        if run_directory is None or not run_directory.is_dir():
            raise ProjectError("Campaign run directory is missing.")
        result_path = run_directory / "result.json"
        if not result_path.is_file():
            raise ProjectError("Campaign result.json is missing; refusing compaction.")
        analysis_sha256 = source.get("analysis_sha256")
        parameters = source.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ProjectError("Source campaign parameters are missing or malformed.")
        current_plan = self.plan(parameters=parameters, portable_fields=True)
        if current_plan["model"]["analysis_sha256"] != analysis_sha256:
            raise ProjectError(
                "Current case.py no longer matches the full-field source; refusing "
                "to rewrite its result identity."
            )
        expected_full_identity = self._result_execution_fingerprint(
            analysis_sha256,
            provider="openfoam",
            portable_fields=True,
        )
        if source.get("result_execution_sha256") != expected_full_identity:
            raise ProjectError(
                "Current OpenFOAM result settings do not match the source run; "
                "refusing ambiguous compaction."
            )
        result = read_result_record(result_path, verify_artifacts=False)
        artifact_records = result.get("artifact_records", {})
        if not isinstance(artifact_records, dict):
            raise ProjectError("Campaign artifact records are malformed.")
        removable_names = {
            name
            for name, record in artifact_records.items()
            if isinstance(name, str)
            and isinstance(record, dict)
            and (
                name.startswith("fields.")
                or name.startswith("postprocess.")
                or record.get("role") == "portable-field-bundle"
            )
        }
        candidates: set[Path] = set()
        fields_directory = run_directory / "fields"
        if fields_directory.exists():
            candidates.add(fields_directory)
        for name in removable_names:
            artifact = artifact_records[name]
            path_value = artifact.get("path")
            if not isinstance(path_value, str) or not path_value:
                continue
            path = Path(path_value)
            resolved = (
                path.resolve()
                if path.is_absolute()
                else (run_directory / path).resolve()
            )
            try:
                resolved.relative_to(run_directory.resolve())
            except ValueError as error:
                raise ProjectError(
                    f"Refusing to compact artifact outside the campaign: {resolved}"
                ) from error
            if resolved.exists():
                candidates.add(resolved)
        roots = sorted(
            (
                candidate
                for candidate in candidates
                if not any(
                    candidate != parent and candidate.is_relative_to(parent)
                    for parent in candidates
                )
            ),
            key=lambda path: str(path),
        )
        targets = []
        candidate_bytes = 0
        candidate_files = 0
        for path in roots:
            size, files = _tree_usage(path)
            candidate_bytes += size
            candidate_files += files
            targets.append(
                {
                    "path": str(path),
                    "bytes": size,
                    "display": _human_bytes(size),
                    "file_count": files,
                }
            )
        summary_identity = self._result_execution_fingerprint(
            analysis_sha256,
            provider="openfoam",
            portable_fields=False,
        )
        report = {
            "schema": "agentcfd.campaign-compaction/0.1",
            "root": str(self.root),
            "run_id": run_id,
            "directory": str(run_directory),
            "applied": apply,
            "from_profile": "full-fields",
            "to_profile": "summary-only",
            "targets": targets,
            "candidate_bytes": candidate_bytes,
            "candidate_display": _human_bytes(candidate_bytes),
            "candidate_file_count": candidate_files,
            "reclaimed_bytes": candidate_bytes if apply else 0,
            "result_execution_sha256": summary_identity,
            "preserved": [
                "result.json",
                "run.json",
                "plan.json",
                "README.md",
                "quantities",
                "checks",
                "histories",
                "logs-and-evidence",
                "derived-csv-png-mp4-not-listed-as-field-artifacts",
            ],
            "observation_cost": {"field_payloads_opened": 0},
        }
        if not apply:
            return report

        compacted_at = datetime.now(UTC).isoformat()
        result["fields"] = {}
        result["field_records"] = []
        result["artifacts"] = {
            name: path
            for name, path in result.get("artifacts", {}).items()
            if name not in removable_names
        }
        result["artifact_records"] = {
            name: record
            for name, record in artifact_records.items()
            if name not in removable_names
        }
        provenance = result.get("provenance", {})
        if not isinstance(provenance, dict):
            provenance = {}
        provenance["result_profile"] = "summary-only"
        provenance["compaction"] = {
            "compacted_at": compacted_at,
            "source_result_execution_sha256": expected_full_identity,
            "reclaimed_bytes": candidate_bytes,
        }
        result["provenance"] = provenance
        messages = result.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        messages.append(
            "Full fields were explicitly compacted; use campaign promotion to regenerate them."
        )
        result["messages"] = messages
        _write_json_atomic(result_path, result)

        updated_source = dict(source)
        updated_source["result_profile"] = "summary-only"
        updated_source["result_execution_sha256"] = summary_identity
        updated_source["compaction"] = provenance["compaction"]
        _write_json_atomic(run_directory / "run.json", updated_source)
        guide = run_directory / "README.md"
        guide_text = guide.read_text(encoding="utf-8") if guide.is_file() else ""
        note = (
            "\n## Compacted fields\n\n"
            "This accepted campaign point now keeps summaries and evidence only. "
            f"Regenerate standard XDMF/HDF5 with `agentcfd promote . {run_id}`.\n"
        )
        if "## Compacted fields" not in guide_text:
            temporary_guide = guide.with_suffix(".md.tmp")
            temporary_guide.write_text(guide_text.rstrip() + note, encoding="utf-8")
            temporary_guide.replace(guide)
        for path in roots:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        read_result_record(result_path, verify_artifacts=True)
        return report

    def _active_run_ids(self) -> set[str]:
        return {
            str(record["run_id"])
            for record in self._run_records()
            if record.get("status") in {"preparing", "running", "exporting"}
            and _process_is_alive(record.get("pid"))
            and record.get("run_id")
        }

    def _protected_recovery_run_ids(self) -> set[str]:
        """Protect the latest workspace when it is the only checkpoint copy."""

        runs = self._run_records()
        latest = runs[0] if runs else None
        if latest is None:
            return set()
        run_id = latest.get("run_id")
        status = str(latest.get("status", "unknown"))
        interrupted = status in {"preparing", "running", "exporting"} and not _process_is_alive(
            latest.get("pid")
        )
        if not isinstance(run_id, str) or (status != "failed" and not interrupted):
            return set()
        run_directory = self._record_directory(latest)
        if (
            run_directory is not None
            and (run_directory / "evidence" / "restart.zip").is_file()
        ):
            return set()
        case = self.root / ".agentcfd" / "work" / run_id / "openfoam"
        if not case.is_dir():
            return set()
        for path in case.iterdir():
            try:
                value = float(path.name)
            except ValueError:
                continue
            if value > 0 and all((path / field).is_file() for field in ("U", "p")):
                return {run_id}
        return set()

    def _protected_retained_run_ids(self) -> set[str]:
        """Return workspaces that a person or project policy explicitly retained."""

        root = self.root / ".agentcfd" / "work"
        if not root.is_dir():
            return set()
        retained = set()
        for workspace in root.iterdir():
            marker = workspace / ".agentcfd-workspace.json"
            try:
                record = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(record, dict)
                and record.get("schema") == "agentcfd.workspace/0.1"
                and record.get("run_id") == workspace.name
                and record.get("protected") is True
                and record.get("retention_reason")
                in {"explicit-cli", "manifest-policy"}
            ):
                retained.add(workspace.name)
        return retained

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

    def _log_candidates(
        self,
        record: Mapping[str, object] | None,
        run_directory: Path | None,
    ) -> tuple[tuple[str, Path, str], ...]:
        if record is None:
            return ()
        run_id = record.get("run_id")
        workspace_case = (
            self.root / ".agentcfd" / "work" / str(run_id) / "openfoam"
            if isinstance(run_id, str) and run_id
            else None
        )
        candidates: list[tuple[str, Path, str]] = []
        if workspace_case is not None and workspace_case.is_dir():
            candidates.extend(
                (
                    path.name.removeprefix("log."),
                    path,
                    "workspace",
                )
                for path in workspace_case.glob("log.*")
                if path.is_file()
            )
        if run_directory is not None:
            evidence = run_directory / "evidence"
            if evidence.is_dir():
                candidates.extend(
                    (
                        path.name.removesuffix(".log"),
                        path,
                        "published-evidence",
                    )
                    for path in evidence.glob("*.log")
                    if path.is_file()
                )
        unique: dict[tuple[str, str], tuple[str, Path, str]] = {}
        for candidate in candidates:
            unique[(candidate[0], str(candidate[1]))] = candidate

        def modified(candidate: tuple[str, Path, str]) -> float:
            try:
                return candidate[1].stat().st_mtime
            except OSError:
                return 0.0

        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (modified(item), item[0]),
            )
        )

    def _recovery_status(
        self,
        step: Step,
        plan: Mapping[str, object],
        latest: Mapping[str, object] | None,
    ) -> dict[str, object]:
        project_argument = self._cli_project_argument()
        unavailable = {
            "schema": "agentcfd.project-recovery/0.1",
            "available": False,
            "source_run_id": None,
            "source": None,
            "coordinate": None,
            "identity_match": False,
            "reason": "No eligible failed or interrupted checkpoint is available.",
            "command": None,
        }
        if latest is None:
            return unavailable
        run_id = latest.get("run_id")
        native_status = str(latest.get("status", "unknown"))
        interrupted = native_status in {"preparing", "running", "exporting"} and not _process_is_alive(
            latest.get("pid")
        )
        if native_status != "failed" and not interrupted:
            return unavailable
        if (
            not isinstance(run_id, str)
            or not run_id
            or self.manifest.default_provider != "openfoam"
            or not isinstance(step.model.domain, RectangularChannel)
            or step.model.study.steady
            or not step.output.checkpoints.enabled
        ):
            return {
                **unavailable,
                "source_run_id": run_id if isinstance(run_id, str) else None,
                "reason": "The latest run does not use a resumable transient capability and checkpoint policy.",
            }
        analysis_sha256 = plan["model"]["analysis_sha256"]
        expected_execution = self._execution_fingerprint(
            analysis_sha256,
            provider="openfoam",
        )
        expected_resume_execution = self._resume_execution_fingerprint(
            analysis_sha256,
            provider="openfoam",
        )
        source_resume_execution = latest.get("resume_execution_sha256")
        identity_match = (
            latest.get("analysis_sha256") == analysis_sha256
            and (
                source_resume_execution == expected_resume_execution
                if isinstance(source_resume_execution, str)
                else latest.get("execution_sha256") == expected_execution
            )
        )
        if not identity_match:
            return {
                **unavailable,
                "source_run_id": run_id,
                "reason": "Current project or runtime inputs differ from the checkpoint source.",
            }
        run_directory = self._record_directory(latest)
        archive = (
            run_directory / "evidence" / "restart.zip"
            if run_directory is not None
            else None
        )
        latest_time: float | None = None
        source: str | None = None
        if archive is not None and archive.is_file():
            try:
                with zipfile.ZipFile(archive) as bundle:
                    metadata = json.loads(bundle.read("restart.json"))
                if (
                    metadata.get("schema") == "agentcfd.openfoam-restart/0.1"
                    and metadata.get("model_sha256") == step.model.fingerprint()
                ):
                    retained_times = tuple(
                        float(value) for value in metadata["retained_times"]
                    )
                    candidates = tuple(
                        value
                        for value in retained_times
                        if 0 < value <= step.procedure.end_time
                    )
                    latest_time = max(candidates) if candidates else None
                    source = "published-checkpoint"
            except (KeyError, OSError, TypeError, ValueError, zipfile.BadZipFile):
                latest_time = None
        if latest_time is None:
            workspace_case = (
                self.root / ".agentcfd" / "work" / run_id / "openfoam"
            )
            interval = step.output.checkpoints.every
            assert interval is not None
            times = []
            if workspace_case.is_dir():
                for path in workspace_case.iterdir():
                    try:
                        value = float(path.name)
                    except ValueError:
                        continue
                    if (
                        value > 0
                        and value <= step.procedure.end_time
                        and math.isclose(
                            value / interval,
                            round(value / interval),
                            rel_tol=0.0,
                            abs_tol=1.0e-8,
                        )
                        and all((path / field).is_file() for field in ("U", "p"))
                    ):
                        times.append(value)
            if times:
                latest_time = max(times)
                source = "retained-workspace"
        if latest_time is None or latest_time > step.procedure.end_time * (1.0 + 1e-10):
            return {
                **unavailable,
                "source_run_id": run_id,
                "identity_match": True,
                "reason": "No complete checkpoint at or before the requested end time is available.",
            }
        return {
            **unavailable,
            "available": True,
            "source_run_id": run_id,
            "source": source,
            "coordinate": {
                "name": "time",
                "value": latest_time,
                "unit": "s",
            },
            "identity_match": True,
            "reason": "An identity-matched complete checkpoint can avoid recomputing earlier time steps.",
            "command": f"agentcfd resume {project_argument}",
        }

    def recovery(self, *, run_id: str | None = None) -> dict[str, object]:
        """Report checkpoint-resume eligibility without opening field payloads."""

        selected = self._select_run_record(run_id)
        parameters = (
            selected.get("parameters", {}) if selected is not None else {}
        )
        if not isinstance(parameters, Mapping):
            parameters = {}
        step = self.load_step(parameters)
        plan = self.plan(parameters=parameters, _step=step)
        return self._recovery_status(step, plan, selected)

    def logs(
        self,
        *,
        command: str | None = None,
        lines: int = 80,
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Return a bounded tail from the selected workspace or published log."""

        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 1000:
            raise ValueError("Log line count must be an integer from 1 through 1000.")
        selected_run = self._select_run_record(run_id)
        run_directory = self._record_directory(selected_run)
        candidates = self._log_candidates(selected_run, run_directory)
        available = sorted({name for name, _path, _source in candidates})
        if command is not None:
            selected = [item for item in candidates if item[0] == command]
            if not selected:
                raise ProjectError(
                    f"No log exists for command {command!r}. Available: "
                    + (", ".join(available) or "none")
                    + "."
                )
            chosen = selected[-1]
        elif candidates:
            chosen = candidates[-1]
        else:
            raise ProjectError(
                "No solver log is available. Run the project or inspect its readiness first."
            )
        name, path, source = chosen
        tail, bytes_read = _read_text_tail(path, maximum_bytes=1024 * 1024)
        all_tail_lines = tail.splitlines()
        selected_lines = all_tail_lines[-lines:]
        try:
            total_bytes = path.stat().st_size
        except OSError:
            total_bytes = bytes_read
        project_argument = self._cli_project_argument()
        return {
            "schema": "agentcfd.project-logs/0.1",
            "root": str(self.root),
            "run_id": None if selected_run is None else selected_run.get("run_id"),
            "run_status": (
                None if selected_run is None else selected_run.get("status")
            ),
            "command": name,
            "available_commands": available,
            "source": source,
            "path": str(path),
            "requested_lines": lines,
            "returned_lines": len(selected_lines),
            "total_bytes": total_bytes,
            "bytes_read": bytes_read,
            "truncated": total_bytes > bytes_read or len(all_tail_lines) > lines,
            "tail": "\n".join(selected_lines) + ("\n" if selected_lines else ""),
            "next_action": {
                "command": f"agentcfd run {project_argument}",
                "reason": "Retry after addressing the diagnostic evidence.",
            },
        }

    def diagnose(
        self,
        *,
        command: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Classify bounded provider evidence into conservative repair guidance."""

        selected_run = self._select_run_record(run_id)
        run_directory = self._record_directory(selected_run)
        candidates = self._log_candidates(selected_run, run_directory)
        available = sorted({name for name, _path, _source in candidates})
        if command is not None:
            candidates = tuple(item for item in candidates if item[0] == command)
            if not candidates:
                raise ProjectError(
                    f"No log exists for command {command!r}. Available: "
                    + (", ".join(available) or "none")
                    + "."
                )
        elif not candidates:
            raise ProjectError(
                "No solver log is available. Run the project or inspect its readiness first."
            )

        newest_by_command: dict[str, tuple[str, Path, str]] = {}
        for candidate in candidates:
            newest_by_command[candidate[0]] = candidate
        selected = tuple(newest_by_command.values())[-8:]
        observations: list[diagnostics.LogObservation] = []
        scanned: list[dict[str, object]] = []
        total_bytes_read = 0
        for name, path, source in selected:
            try:
                text, bytes_read = _read_text_tail(path, maximum_bytes=256 * 1024)
            except OSError:
                continue
            observations.append(
                diagnostics.LogObservation(command=name, source=source, text=text)
            )
            total_bytes_read += bytes_read
            scanned.append(
                {
                    "command": name,
                    "source": source,
                    "path": str(path),
                    "bytes_read": bytes_read,
                }
            )
        if not observations:
            raise ProjectError("Solver logs disappeared before they could be diagnosed.")

        findings = [dict(item) for item in diagnostics.diagnose(observations)]
        primary = findings[0] if findings else None
        project_argument = self._cli_project_argument()
        run_selector = (
            ""
            if selected_run is None or selected_run.get("run_id") is None
            else " --run-id " + shlex.quote(str(selected_run["run_id"]))
        )
        if primary is None:
            fallback_command = observations[-1].command
            next_action = {
                "command": (
                    f"agentcfd logs {project_argument} --command "
                    f"{shlex.quote(fallback_command)} --lines 200{run_selector}"
                ),
                "reason": (
                    "No supported deterministic signature was found; inspect the "
                    "bounded raw evidence without changing generated files."
                ),
            }
        else:
            next_step = primary.pop("next_step")
            for finding in findings[1:]:
                finding.pop("next_step", None)
            evidence = primary["evidence"]
            evidence_command = str(evidence["command"])
            action_commands = {
                "clean": f"agentcfd clean {project_argument}",
                "plan": f"agentcfd plan {project_argument}",
                "check": f"agentcfd check {project_argument}",
                "logs": (
                    f"agentcfd logs {project_argument} --command "
                    f"{shlex.quote(evidence_command)} --lines 200{run_selector}"
                ),
            }
            next_action = {
                "command": action_commands[str(next_step)],
                "reason": str(primary["repair"]),
            }

        recovery = self.recovery(
            run_id=(
                None
                if selected_run is None
                else str(selected_run.get("run_id") or "") or None
            )
        )

        return {
            "schema": "agentcfd.project-diagnosis/0.1",
            "root": str(self.root),
            "run_id": None if selected_run is None else selected_run.get("run_id"),
            "run_status": (
                None if selected_run is None else selected_run.get("status")
            ),
            "available_commands": available,
            "logs_scanned": scanned,
            "findings": findings,
            "primary_finding": primary,
            "recovery": recovery,
            "resume_after_repair": (
                {
                    "command": recovery["command"],
                    "reason": recovery["reason"],
                }
                if recovery["available"]
                else None
            ),
            "observation_cost": {
                "field_payloads_opened": 0,
                "log_files_opened": len(scanned),
                "maximum_bytes_per_log": 256 * 1024,
                "total_bytes_read": total_bytes_read,
            },
            "next_action": next_action,
        }

    def _record_performance(
        self,
        *,
        run_record: Mapping[str, object],
        plan: Mapping[str, object],
        execution_provider: str,
        performance_key: str,
    ) -> None:
        """Append one bounded runtime sample without risking a completed result."""

        total_seconds = _elapsed_seconds(
            run_record.get("started_at"), run_record.get("completed_at")
        )
        if total_seconds is None:
            return
        samples, history_status = _read_performance_history(self.root)
        if history_status == "invalid":
            # Runtime calibration is advisory. Never overwrite an unrecognized
            # record or turn a successful simulation into a failed run.
            return
        decisions = plan.get("decisions", {})
        output_plan = (
            decisions.get("output_plan", {})
            if isinstance(decisions, Mapping)
            else {}
        )
        samples.append(
            {
                "run_id": run_record.get("run_id"),
                "recorded_at": run_record.get("completed_at"),
                "status": run_record.get("status"),
                "accepted": run_record.get("accepted"),
                "provider": execution_provider,
                "performance_key": performance_key,
                "analysis_sha256": run_record.get("analysis_sha256"),
                "result_profile": run_record.get("result_profile"),
                "model_name": run_record.get("model_name"),
                "solver": (
                    decisions.get("solver")
                    if isinstance(decisions, Mapping)
                    else None
                ),
                "estimated_mesh_cells": (
                    output_plan.get("estimated_mesh_cells")
                    if isinstance(output_plan, Mapping)
                    else None
                ),
                "total_seconds": round(total_seconds, 6),
            }
        )
        samples = samples[-_PERFORMANCE_HISTORY_LIMIT:]
        path = self.root / ".agentcfd" / "performance.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                path,
                {
                    "schema": "agentcfd.performance-history/0.1",
                    "updated_at": run_record.get("completed_at"),
                    "maximum_samples": _PERFORMANCE_HISTORY_LIMIT,
                    "samples": samples,
                },
            )
        except (OSError, TypeError, ValueError):
            return

    def performance(self) -> dict[str, object]:
        """Return bounded runtime evidence and current-project ETA calibration."""

        plan = self.plan()
        provider = self.manifest.default_provider
        result_profile = str(plan["decisions"]["result_profile"])
        current_key = _performance_key(
            plan,
            provider=provider,
            result_profile=result_profile,
        )
        samples, history_status = _read_performance_history(self.root)
        comparable = [
            item
            for item in samples
            if item.get("performance_key") == current_key
            and item.get("status") == "completed"
            and isinstance(item.get("total_seconds"), (int, float))
        ]
        durations = [float(item["total_seconds"]) for item in comparable]
        calibration = (
            None
            if not durations
            else {
                "sample_count": len(durations),
                "minimum_seconds": min(durations),
                "median_seconds": statistics.median(durations),
                "maximum_seconds": max(durations),
                "minimum_display": _human_duration(min(durations)),
                "median_display": _human_duration(statistics.median(durations)),
                "maximum_display": _human_duration(max(durations)),
                "basis": (
                    "same provider, solver, geometry, boundary types, Reynolds "
                    "regime, mesh, procedure, and output profile"
                ),
            }
        )
        project_argument = self._cli_project_argument()
        return {
            "schema": "agentcfd.project-performance/0.1",
            "root": str(self.root),
            "history": {
                "path": str(self.root / ".agentcfd" / "performance.json"),
                "status": history_status,
                "maximum_samples": _PERFORMANCE_HISTORY_LIMIT,
                "sample_count": len(samples),
            },
            "current": {
                "performance_key": current_key,
                "provider": provider,
                "result_profile": result_profile,
                "estimated_mesh_cells": plan["decisions"]["output_plan"].get(
                    "estimated_mesh_cells"
                ),
                "calibration": calibration,
            },
            "recent_samples": samples[-10:],
            "next_action": {
                "command": f"agentcfd status {project_argument}",
                "reason": (
                    "Run the project to create the first comparable runtime sample."
                    if calibration is None
                    else "Use status/watch for a history-calibrated remaining-time range."
                ),
            },
        }

    def storage(self) -> dict[str, object]:
        """Inventory managed project data without reading field arrays."""

        project_argument = self._cli_project_argument()
        workspace_root = self.root / ".agentcfd" / "work"
        groups = {
            "current_output": self.run_root,
            "campaigns": self.root / "campaigns",
            "temporary_workspaces": workspace_root,
            "mesh_cache": self.root / ".agentcfd" / "mesh-cache",
            "runtime_history": self.root / ".agentcfd" / "performance.json",
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
        recovery_run_ids = self._protected_recovery_run_ids()
        retained_run_ids = self._protected_retained_run_ids()
        protected_run_ids = active_run_ids | recovery_run_ids | retained_run_ids
        active_workspace_bytes = sum(
            _tree_usage(workspace_root / run_id)[0] for run_id in active_run_ids
        )
        recovery_workspace_bytes = sum(
            _tree_usage(workspace_root / run_id)[0] for run_id in recovery_run_ids
        )
        retained_workspace_bytes = sum(
            _tree_usage(workspace_root / run_id)[0] for run_id in retained_run_ids
        )
        workspace_bytes = int(categories["temporary_workspaces"]["bytes"])
        protected_workspace_bytes = sum(
            _tree_usage(workspace_root / run_id)[0] for run_id in protected_run_ids
        )
        reclaimable = max(0, workspace_bytes - protected_workspace_bytes)
        categories["temporary_workspaces"]["active_bytes"] = active_workspace_bytes
        categories["temporary_workspaces"]["active_display"] = _human_bytes(
            active_workspace_bytes
        )
        categories["temporary_workspaces"]["active_run_ids"] = sorted(active_run_ids)
        categories["temporary_workspaces"]["recovery_checkpoint_bytes"] = (
            recovery_workspace_bytes
        )
        categories["temporary_workspaces"]["recovery_checkpoint_display"] = (
            _human_bytes(recovery_workspace_bytes)
        )
        categories["temporary_workspaces"]["protected_recovery_run_ids"] = sorted(
            recovery_run_ids
        )
        categories["temporary_workspaces"]["retained_bytes"] = retained_workspace_bytes
        categories["temporary_workspaces"]["retained_display"] = _human_bytes(
            retained_workspace_bytes
        )
        categories["temporary_workspaces"]["protected_retained_run_ids"] = sorted(
            retained_run_ids
        )
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
                "mesh_cache": "content-addressed and retained for compatible imported-geometry runs",
                "runtime_history": "bounded to 50 field-free advisory samples",
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

    def clean(
        self,
        *,
        apply: bool = False,
        include_retained: bool = False,
        include_cache: bool = False,
    ) -> dict[str, object]:
        """Preview or remove managed bulk; preserve all published results."""

        workspace_root = self.root / ".agentcfd" / "work"
        active_run_ids = self._active_run_ids()
        recovery_run_ids = self._protected_recovery_run_ids()
        retained_run_ids = (
            set() if include_retained else self._protected_retained_run_ids()
        )
        before = 0
        file_count = 0
        targets = []
        if workspace_root.is_dir():
            for path in sorted(workspace_root.iterdir()):
                size, files = _tree_usage(path)
                protected_active = path.name in active_run_ids
                protected_recovery = path.name in recovery_run_ids
                protected_retained = path.name in retained_run_ids
                protected = (
                    protected_active or protected_recovery or protected_retained
                )
                targets.append(
                    {
                        "path": str(path),
                        "bytes": size,
                        "display": _human_bytes(size),
                        "file_count": files,
                        "protected_active_run": protected_active,
                        "protected_recovery_checkpoint": protected_recovery,
                        "protected_retained_workspace": protected_retained,
                    }
                )
                if not protected:
                    before += size
                    file_count += files
        mesh_cache_root = self.root / ".agentcfd" / "mesh-cache"
        if include_cache and mesh_cache_root.exists():
            size, files = _tree_usage(mesh_cache_root)
            targets.append(
                {
                    "path": str(mesh_cache_root),
                    "category": "mesh-cache",
                    "bytes": size,
                    "display": _human_bytes(size),
                    "file_count": files,
                    "protected_active_run": False,
                    "protected_recovery_checkpoint": False,
                    "protected_retained_workspace": False,
                }
            )
            before += size
            file_count += files
        if apply:
            for target in targets:
                if target["protected_active_run"] or target[
                    "protected_recovery_checkpoint"
                ] or target["protected_retained_workspace"]:
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
            and not target["protected_recovery_checkpoint"]
            and not target["protected_retained_workspace"]
        )
        return {
            "schema": "agentcfd.project-clean/0.1",
            "root": str(self.root),
            "applied": apply,
            "include_retained": include_retained,
            "include_cache": include_cache,
            "scope": (
                "temporary-workspaces-and-mesh-cache"
                if include_cache
                else "temporary-workspaces-only"
            ),
            "targets": targets,
            "preserved": [
                str(self.run_root),
                str(self.root / "campaigns"),
                *(
                    []
                    if include_cache
                    else [str(self.root / ".agentcfd" / "mesh-cache")]
                ),
            ],
            "protected_active_run_ids": sorted(active_run_ids),
            "protected_recovery_run_ids": sorted(recovery_run_ids),
            "protected_retained_run_ids": sorted(retained_run_ids),
            "candidate_bytes": before,
            "candidate_display": _human_bytes(before),
            "candidate_file_count": file_count,
            "reclaimed_bytes": before - after if apply else 0,
            "reclaimed_display": _human_bytes(before - after if apply else 0),
        }

    def status(self, *, include_storage: bool = False) -> dict[str, object]:
        """Return one human/agent decision surface for the whole project."""

        step = self.load_step()
        plan = self.plan(_step=step)
        current_execution_sha256 = self._execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=self.manifest.default_provider,
        )
        current_result_execution_sha256 = self._result_execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=self.manifest.default_provider,
        )
        project_argument = self._cli_project_argument()
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
                latest_result_execution = latest.get("result_execution_sha256")
                latest_analysis = latest.get("analysis_sha256")
                if latest_analysis is None and run_directory is not None:
                    try:
                        saved_plan = json.loads(
                            (run_directory / "plan.json").read_text(encoding="utf-8")
                        )
                        latest_analysis = saved_plan["model"]["analysis_sha256"]
                    except (OSError, KeyError, TypeError, json.JSONDecodeError):
                        pass
                if isinstance(latest_result_execution, str):
                    changed = (
                        latest_result_execution != current_result_execution_sha256
                    )
                elif isinstance(latest_execution, str):
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
        fields_path = (
            None if run_directory is None else run_directory / "fields" / "fields.xdmf"
        )
        if fields_path is not None and not fields_path.is_file():
            fields_path = None
        if result_path is not None and not result_path.is_file():
            result_path = None
        postprocess = {
            "primary": str(fields_path or result_path)
            if fields_path or result_path
            else None,
            "fields": None if fields_path is None else str(fields_path),
            "result": None if result_path is None else str(result_path),
            "command": (
                f"agentcfd view {project_argument}"
                if fields_path or result_path
                else None
            ),
            "field_summary": (
                None if fields_path is None else _field_bundle_summary(fields_path)
            ),
            "recipes": [],
        }
        if run_directory is not None:
            recipe_manifest = postprocessing.read_recipe_manifest(run_directory)
            if isinstance(recipe_manifest, dict) and isinstance(
                recipe_manifest.get("recipes"), list
            ):
                postprocess["recipes"] = [
                    {
                        **recipe,
                        "script": str(run_directory / "postprocess" / recipe["script"]),
                    }
                    for recipe in recipe_manifest["recipes"]
                    if isinstance(recipe, dict)
                    and isinstance(recipe.get("script"), str)
                ]
        progress = _run_progress_snapshot(
            self.root,
            latest,
            plan,
            include_storage=include_storage,
        )
        recovery = self._recovery_status(step, plan, latest)
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
        elif state in {"ready", "modified"}:
            next_action = {
                "command": f"agentcfd run {project_argument}",
                "reason": {
                    "ready": "Create the first result.",
                    "modified": "Inputs changed since the latest result.",
                }[state],
            }
        elif state in {"interrupted", "failed"}:
            has_logs = bool(self._log_candidates(latest, run_directory))
            next_action = {
                "command": (
                    f"agentcfd diagnose {project_argument}"
                    if has_logs
                    else f"agentcfd run {project_argument}"
                ),
                "reason": (
                    "Classify bounded solver evidence before retrying."
                    if has_logs
                    else "Retry the managed run; no solver log was produced."
                ),
            }
        elif state == "running":
            next_action = {
                "command": f"agentcfd watch {project_argument}",
                "reason": "Follow low-overhead progress until the run finishes.",
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
            "parameters": plan["project"]["factory_parameters"],
            "readiness": plan["readiness"],
            "issues": plan["issues"],
            "run_count": len(runs),
            "latest_run": latest,
            "progress": progress,
            "postprocess": postprocess,
            "recovery": recovery,
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
            "recovery": status["recovery"],
            "next_action": status["next_action"],
        }

    def snapshot(
        self,
        *,
        include_result: bool = True,
        include_storage: bool = False,
    ) -> dict[str, object]:
        """Return one lightweight project surface for Python, agents, and GUIs.

        The snapshot opens compact JSON metadata only. It never opens HDF5 or
        provider-native field payloads, and it never starts a solver or viewer.
        """

        status = self.status()
        latest = status["latest_run"]
        run_directory = self._record_directory(latest)
        output = None
        compact_result = None
        result_bytes = 0
        if run_directory is not None:
            run_id = latest.get("run_id") if isinstance(latest, Mapping) else None
            candidates = (
                ("guide", "human-start-here", run_directory / "README.md"),
                ("plan", "resolved-analysis-plan", run_directory / "plan.json"),
                ("run", "execution-record", run_directory / "run.json"),
                ("result", "scientific-result", run_directory / "result.json"),
            )
            published_files = [
                {"name": name, "role": role, "path": str(path)}
                for name, role, path in candidates
                if path.is_file()
            ]
            fields_directory = run_directory / "fields"
            xdmf_path = fields_directory / "fields.xdmf"
            h5_path = fields_directory / "fields.h5"
            manifest_path = fields_directory / "manifest.json"
            fields = None
            if any(path.is_file() for path in (xdmf_path, h5_path, manifest_path)):
                fields = {
                    "xdmf": str(xdmf_path) if xdmf_path.is_file() else None,
                    "h5": str(h5_path) if h5_path.is_file() else None,
                    "manifest": (
                        str(manifest_path) if manifest_path.is_file() else None
                    ),
                    "summary": status["postprocess"]["field_summary"],
                }
            workspace = (
                self.root / ".agentcfd" / "work" / str(run_id) / "openfoam"
                if isinstance(run_id, str)
                else None
            )
            retained = workspace is not None and workspace.is_dir()
            retention = (
                latest.get("workspace_retention", {})
                if isinstance(latest, Mapping)
                else {}
            )
            output = {
                "run_id": run_id,
                "mode": latest.get("mode") if isinstance(latest, Mapping) else None,
                "directory": str(run_directory),
                "result_profile": (
                    latest.get("result_profile")
                    if isinstance(latest, Mapping)
                    else None
                ),
                "published_files": published_files,
                "fields": fields,
                "expert_workspace": {
                    "retained": retained,
                    "path": str(workspace) if retained else None,
                    "visibility": "hidden-provider-implementation",
                    "reason": (
                        retention.get("reason")
                        if isinstance(retention, Mapping)
                        else None
                    ),
                },
            }
            result_path = run_directory / "result.json"
            if include_result and result_path.is_file():
                compact_result = self.result_summary()
                result_bytes = int(
                    compact_result["observation_cost"]["result_json_bytes_read"]
                )

        storage = self.storage() if include_storage else None
        run = None
        if isinstance(latest, Mapping):
            run = {
                name: latest.get(name)
                for name in (
                    "run_id",
                    "mode",
                    "status",
                    "phase",
                    "accepted",
                    "trust_level",
                    "provider",
                    "parameters",
                    "result_profile",
                    "started_at",
                    "completed_at",
                )
            }
        return {
            "schema": "agentcfd.project-snapshot/0.1",
            "project": {
                "root": str(self.root),
                "manifest": str(self.manifest_path),
                "entrypoint": str(self.entrypoint),
                "provider": self.manifest.default_provider,
                "run_mode": self.manifest.run_mode,
                "output_directory": str(self.run_root),
            },
            "state": status["state"],
            "model": {
                name: status["model"].get(name)
                for name in (
                    "name",
                    "sha256",
                    "analysis_sha256",
                    "reynolds_number",
                )
            },
            "parameters": status["parameters"],
            "readiness": status["readiness"],
            "issues": status["issues"],
            "run": run,
            "progress": status["progress"],
            "output": output,
            "result": compact_result,
            "storage": storage,
            "next_action": _structured_project_action(status["next_action"]),
            "observation_cost": {
                "field_payloads_opened": 0,
                "compact_result_json_bytes_read": result_bytes,
                "recursive_storage_scan": include_storage,
            },
        }

    def verify(self, *, run_id: str | None = None) -> dict[str, object]:
        """Verify one published project result and its portable field bundle.

        Unlike :meth:`snapshot`, this explicit integrity operation hashes every
        result artifact and opens the standard XDMF/H5 bundle when present.
        """

        selected = self._select_run_record(run_id)
        if selected is None:
            raise ProjectError("No project result exists; run the project first.")
        run_directory = self._record_directory(selected)
        if run_directory is None:
            raise ProjectError("The selected run directory cannot be resolved.")
        run_path = run_directory / "run.json"
        plan_path = run_directory / "plan.json"
        result_path = run_directory / "result.json"
        checks: list[dict[str, object]] = []

        def add_check(
            code: str,
            passed: bool,
            message: str,
            path: Path,
            repair: str,
        ) -> None:
            checks.append(
                {
                    "code": code,
                    "passed": passed,
                    "message": message,
                    "path": str(path),
                    "repair": None if passed else repair,
                }
            )

        run_record = None
        try:
            run_record = strict_json_object(
                run_path.read_text(encoding="utf-8"),
                label=f"AgentCFD project run {run_path}",
            )
            if run_record.get("schema") != "agentcfd.project-run/0.1":
                raise ValueError("Unsupported AgentCFD project-run schema.")
            if run_record.get("run_id") != selected.get("run_id"):
                raise ValueError("Run marker identity disagrees with project discovery.")
        except (OSError, TypeError, ValueError) as error:
            add_check(
                "RUN_RECORD_INTEGRITY",
                False,
                str(error),
                run_path,
                "Restore the managed output from case.py with `agentcfd run .`.",
            )
        else:
            add_check(
                "RUN_RECORD_INTEGRITY",
                True,
                "The atomic project run record has a supported identity.",
                run_path,
                "",
            )

        plan_record = None
        plan_analysis = None
        legacy_analysis = None
        try:
            plan_record = strict_json_object(
                plan_path.read_text(encoding="utf-8"),
                label=f"AgentCFD solution plan {plan_path}",
            )
            if plan_record.get("schema") != "agentcfd.solution-plan/0.1":
                raise ValueError("Unsupported AgentCFD solution-plan schema.")
            recorded_plan_identity = plan_record.get("plan_sha256")
            unsigned_plan = dict(plan_record)
            unsigned_plan.pop("plan_sha256", None)
            computed_plan_identity = content_fingerprint(unsigned_plan)
            if recorded_plan_identity != computed_plan_identity:
                raise ValueError("Solution plan content does not match plan_sha256.")
            if (
                run_record is not None
                and run_record.get("plan_sha256") != computed_plan_identity
            ):
                raise ValueError("Run marker points to a different solution plan.")
            model_record = plan_record.get("model")
            decisions = plan_record.get("decisions")
            if not isinstance(model_record, Mapping) or not isinstance(
                decisions, Mapping
            ):
                raise ValueError("Solution plan is missing model or decision records.")
            plan_analysis = model_record.get("analysis_sha256")
            if not isinstance(plan_analysis, str) or not plan_analysis:
                raise ValueError("Solution plan has no analysis identity.")
            # 0.1.0a3 OpenFOAM results used this transparent precursor to the
            # public Step fingerprint.  Recompute it only from the verified,
            # content-addressed plan; accepting a legacy result remains fail-closed.
            legacy_payload = {
                "model": model_record.get("summary"),
                "procedure": decisions.get("procedure"),
                "output_request": decisions.get("outputs"),
            }
            if decisions.get("initialization") is not None:
                legacy_payload["initialization"] = decisions["initialization"]
            if decisions.get("mesh_intent") is not None:
                legacy_payload["mesh"] = decisions["mesh_intent"]
            legacy_analysis = content_fingerprint(legacy_payload).removeprefix(
                "sha256:"
            )
        except (OSError, TypeError, ValueError) as error:
            add_check(
                "PLAN_INTEGRITY",
                False,
                str(error),
                plan_path,
                "Restore the managed output from case.py with `agentcfd run .`.",
            )
        else:
            add_check(
                "PLAN_INTEGRITY",
                True,
                "The saved solution plan and its recorded content identity agree.",
                plan_path,
                "",
            )

        result_record = None
        artifact_count = 0
        try:
            result_record = read_result_record(result_path, verify_artifacts=True)
            artifact_count = len(result_record["artifact_records"])
        except (AgentCFDError, OSError, KeyError, TypeError, ValueError) as error:
            add_check(
                "RESULT_INTEGRITY",
                False,
                str(error),
                result_path,
                "Restore the managed output from case.py with `agentcfd run .`.",
            )
        else:
            add_check(
                "RESULT_INTEGRITY",
                True,
                f"The result and {artifact_count} registered artifacts match their identities.",
                result_path,
                "",
            )

        consistency_errors = []
        legacy_identity = False
        if run_record is not None and result_record is not None:
            for key in ("status", "accepted", "trust_level", "provider"):
                if run_record.get(key) != result_record.get(key):
                    consistency_errors.append(key)
            run_analysis = run_record.get("analysis_sha256")
            provenance = result_record.get("provenance", {})
            result_analysis = (
                provenance.get("analysis_sha256")
                if isinstance(provenance, Mapping)
                else None
            )
            if (
                isinstance(plan_analysis, str)
                and run_analysis == plan_analysis
                and result_analysis == plan_analysis
            ):
                pass
            elif (
                run_analysis is None
                and isinstance(legacy_analysis, str)
                and result_analysis == legacy_analysis
            ):
                legacy_identity = True
            else:
                consistency_errors.append("analysis_sha256")
        elif run_record is None or result_record is None:
            consistency_errors.append("unavailable-record")
        add_check(
            "RUN_RESULT_CONSISTENCY",
            not consistency_errors,
            (
                (
                    "Run lifecycle and scientific state agree; the analysis identity "
                    "was verified through the reproducible 0.1.0a3 compatibility path."
                    if legacy_identity
                    else "Run lifecycle, scientific state, provider, and analysis identity agree."
                )
                if not consistency_errors
                else "Run and result records disagree or are unavailable: "
                + ", ".join(consistency_errors)
                + "."
            ),
            run_directory,
            "Treat this output as unverified and rerun from the readable case.py.",
        )

        fields_directory = run_directory / "fields"
        field_paths = {
            "xdmf": fields_directory / "fields.xdmf",
            "h5": fields_directory / "fields.h5",
            "manifest": fields_directory / "manifest.json",
        }
        present_fields = {name for name, path in field_paths.items() if path.is_file()}
        field_bundle = None
        field_payloads_opened = False
        if present_fields:
            missing = sorted(set(field_paths) - present_fields)
            if missing:
                add_check(
                    "FIELD_BUNDLE_INTEGRITY",
                    False,
                    "The standard field bundle is incomplete; missing: "
                    + ", ".join(missing)
                    + ".",
                    fields_directory,
                    "Regenerate XDMF/H5 from the readable project with `agentcfd run .`.",
                )
            else:
                field_payloads_opened = True
                try:
                    field_bundle = data_exchange.verify_field_bundle(fields_directory)
                except (
                    AgentCFDError,
                    OSError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    add_check(
                        "FIELD_BUNDLE_INTEGRITY",
                        False,
                        str(error),
                        fields_directory,
                        "Repair optional I/O dependencies or regenerate the field bundle.",
                    )
                else:
                    add_check(
                        "FIELD_BUNDLE_INTEGRITY",
                        True,
                        "XDMF, H5, manifest, hashes, mesh, and frame axes agree.",
                        fields_directory,
                        "",
                    )

        verified = all(check["passed"] is True for check in checks)
        project_argument = self._cli_project_argument()
        next_action = _structured_project_action(
            {
                "command": (
                    f"agentcfd view {project_argument}"
                    if verified
                    else f"agentcfd run {project_argument}"
                ),
                "reason": (
                    "Review the verified published result."
                    if verified
                    else "Regenerate the managed output from readable project intent."
                ),
            }
        )
        return {
            "schema": "agentcfd.project-verification/0.1",
            "root": str(self.root),
            "run_id": selected.get("run_id"),
            "run_directory": str(run_directory),
            "verified": verified,
            "accepted": (
                result_record.get("accepted")
                if isinstance(result_record, Mapping)
                else None
            ),
            "trust_level": (
                result_record.get("trust_level")
                if isinstance(result_record, Mapping)
                else None
            ),
            "checks": checks,
            "result": (
                None
                if result_record is None
                else {
                    "path": str(result_path),
                    "artifact_count": artifact_count,
                    "provider": result_record["provider"],
                }
            ),
            "field_bundle": field_bundle,
            "observation_cost": {
                "run_json_bytes_read": run_path.stat().st_size
                if run_path.is_file()
                else 0,
                "plan_json_bytes_read": plan_path.stat().st_size
                if plan_path.is_file()
                else 0,
                "result_json_bytes_read": result_path.stat().st_size
                if result_path.is_file()
                else 0,
                "artifacts_hashed": artifact_count,
                "field_payloads_opened": field_payloads_opened,
                "recursive_storage_scan": False,
            },
            "next_action": next_action,
        }

    def doctor(self) -> dict[str, object]:
        """Audit one project, runtime, and resource envelope without solving."""

        step = self.load_step()
        plan = self.plan(_step=step)
        status = self.status(include_storage=True)
        output_plan = plan["decisions"]["output_plan"]
        storage = status["storage"]
        checks: list[dict[str, object]] = []

        def add_check(
            code: str,
            passed: bool,
            message: str,
            repair: str,
            *,
            severity: str = "error",
        ) -> None:
            checks.append(
                {
                    "code": code,
                    "status": "passed" if passed else "failed",
                    "severity": "info" if passed else severity,
                    "message": message,
                    "repair": "" if passed else repair,
                }
            )

        readiness = plan["readiness"]
        add_check(
            "MODEL_READY",
            bool(readiness["model_valid"]),
            "The public model is internally valid.",
            "Repair the addressable model issues in `case.py`.",
        )
        add_check(
            "PROVIDER_COMPATIBLE",
            bool(readiness["provider_compatible"]),
            "The selected provider advertises the requested capability.",
            "Select a supported capability or revise the declared physics.",
        )
        needs_runtime = status["state"] in {
            "ready",
            "modified",
            "failed",
            "interrupted",
            "blocked",
        }
        add_check(
            "RUNTIME_AVAILABLE",
            bool(readiness["runtime_available"]),
            "The configured solver runtime is available.",
            "Install the required OpenFOAM commands or configure a reachable container image.",
            severity="error" if needs_runtime else "warning",
        )
        within_budget = output_plan.get("within_budget")
        add_check(
            "OUTPUT_WITHIN_BUDGET",
            within_budget is not False,
            "The requested portable and temporary output fits its declared budget.",
            "Reduce full-field frames/fields or raise the explicit storage budget.",
        )
        estimated_peak = output_plan.get("estimated_temporary_peak_bytes")
        free_bytes = storage["filesystem"]["free_bytes"]
        disk_ok = not isinstance(estimated_peak, int) or estimated_peak <= free_bytes
        add_check(
            "FILESYSTEM_HEADROOM",
            disk_ok,
            "The filesystem has room for the conservative temporary peak.",
            "Preview cleanup or reduce output/mesh demand before starting the solver.",
        )
        run_state_ok = status["state"] not in {"failed", "interrupted"}
        add_check(
            "LATEST_RUN_HEALTH",
            run_state_ok,
            "The latest run is not failed or interrupted.",
            "Follow the project status diagnosis before retrying or resuming.",
        )
        accepted_or_not_run = status["state"] not in {"review"}
        add_check(
            "RESULT_ACCEPTANCE",
            accepted_or_not_run,
            "No completed result is awaiting failed-check review.",
            "Review failed scientific checks before design or training use.",
            severity="warning",
        )

        cells = output_plan.get("estimated_mesh_cells")
        if step.model.study.steady:
            nominal_steps = step.procedure.maximum_iterations
            step_basis = "maximum steady iterations"
            correctors = 1
        else:
            nominal_steps = math.ceil(
                step.procedure.end_time / step.procedure.maximum_time_step
            )
            step_basis = "minimum steps at declared maximum time step"
            correctors = step.procedure.pressure_velocity_correctors
        cell_updates = (
            int(cells) * nominal_steps * correctors
            if isinstance(cells, int)
            else None
        )
        peak_ratio = (
            float(estimated_peak) / free_bytes
            if isinstance(estimated_peak, int) and free_bytes > 0
            else None
        )
        return {
            "schema": "agentcfd.project-doctor/0.1",
            "root": str(self.root),
            "healthy": not any(
                item["status"] == "failed" and item["severity"] == "error"
                for item in checks
            ),
            "state": status["state"],
            "checks": checks,
            "resource_estimate": {
                "estimated_mesh_cells": cells,
                "nominal_solver_steps": nominal_steps,
                "solver_step_basis": step_basis,
                "pressure_velocity_correctors": correctors,
                "cell_updates_proxy": cell_updates,
                "estimated_portable_bytes": output_plan.get(
                    "estimated_portable_bytes"
                ),
                "estimated_temporary_peak_bytes": estimated_peak,
                "filesystem_free_bytes": free_bytes,
                "temporary_peak_to_free_ratio": peak_ratio,
                "energy": {
                    "status": "not-measured",
                    "reason": (
                        "Energy cannot be inferred honestly from mesh size; an executor "
                        "must report hardware power or joule telemetry."
                    ),
                },
            },
            "storage": storage,
            "recovery": status["recovery"],
            "observation_cost": {
                "field_payloads_opened": 0,
                "recursive_storage_scan": True,
            },
            "next_action": status["next_action"],
        }

    def run(
        self,
        *,
        provider: str | None = None,
        container_image: str | None = None,
        campaign: bool = False,
        keep_workspace: bool = False,
        parameters: Mapping[str, object] | None = None,
        design_point_name: str | None = None,
        portable_fields: bool | None = None,
        _resume_archive: Path | None = None,
        _resume_source_run_id: str | None = None,
        _promotion_source_run_id: str | None = None,
    ) -> ProjectRun:
        selected_name = provider or self.manifest.default_provider
        if design_point_name is not None and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_-]*", design_point_name
        ) is None:
            raise ProjectError("Invalid campaign design-point name.")
        selected_parameters = self._parameters(parameters)
        step = self.load_step(selected_parameters)
        plan = self.plan(
            provider=selected_name,
            container_image=container_image,
            parameters=selected_parameters,
            portable_fields=portable_fields,
            _step=step,
        )
        readiness = plan["readiness"]
        assert isinstance(readiness, dict)
        if readiness["ready_to_run"] is not True:
            codes = ", ".join(issue["code"] for issue in plan["issues"])
            raise ProjectError(
                f"Project is not ready to run: {codes or 'unknown issue'}"
            )
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
        if (
            not campaign_mode
            and run_directory.exists()
            and any(run_directory.iterdir())
        ):
            marker = run_directory / "run.json"
            try:
                owned = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owned = None
            if (
                not isinstance(owned, dict)
                or owned.get("schema") != "agentcfd.project-run/0.1"
            ):
                raise ProjectError(
                    f"Refusing to replace unmanaged output directory: {run_directory}"
                )
            if owned.get("status") in {
                "preparing",
                "running",
                "exporting",
            } and _process_is_alive(owned.get("pid")):
                raise ProjectError(
                    "Refusing to replace an active AgentCFD run. Use `agentcfd status .` "
                    "to inspect its current phase."
                )
            shutil.rmtree(run_directory)
        run_directory.mkdir(parents=True, exist_ok=not campaign_mode)
        plan_path = run_directory / "plan.json"
        plan_path.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # Establish ownership before external execution. If a solver or exporter
        # raises, the next replace run can safely recover this AgentCFD-owned
        # directory instead of forcing the user to delete it manually.
        marker_path = run_directory / "run.json"
        started_at = datetime.now(UTC).isoformat()
        execution_sha256 = self._execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=selected_name,
            container_image=container_image,
            portable_fields=portable_fields,
        )
        resume_execution_sha256 = self._resume_execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=selected_name,
            container_image=container_image,
        )
        result_execution_sha256 = self._result_execution_fingerprint(
            plan["model"]["analysis_sha256"],
            provider=selected_name,
            container_image=container_image,
            portable_fields=portable_fields,
        )
        result_profile = (
            "summary-only"
            if selected_name == "openfoam"
            and plan["decisions"]["portable_field_bundle"] is False
            else "full-fields"
        )
        performance_key = _performance_key(
            plan,
            provider=selected_name,
            result_profile=result_profile,
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
            "resume_execution_sha256": resume_execution_sha256,
            "result_execution_sha256": result_execution_sha256,
            "result_profile": result_profile,
            "performance_key": performance_key,
            "parameters": selected_parameters,
            "model_name": step.model.name,
            "reynolds_number": _inlet_reynolds(step),
            "design_point_name": design_point_name,
            "started_at": started_at,
            "completed_at": None,
        }
        if _resume_archive is not None:
            marker_record["resume"] = {
                "source_run_id": _resume_source_run_id,
                "archive_sha256": file_sha256(_resume_archive),
            }
        if _promotion_source_run_id is not None:
            marker_record["promotion"] = {
                "source_run_id": _promotion_source_run_id,
                "source_result_profile": "summary-only",
                "target_result_profile": "full-fields",
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

        def write_workspace_marker(
            *, retention_reason: str, protected: bool
        ) -> None:
            if selected_name != "openfoam":
                return
            workspace_root.mkdir(parents=True, exist_ok=True)
            marker = workspace_root / ".agentcfd-workspace.json"
            temporary = marker.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "schema": "agentcfd.workspace/0.1",
                        "run_id": run_id,
                        "retention_reason": retention_reason,
                        "protected": protected,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(marker)

        write_workspace_marker(retention_reason="active-run", protected=False)
        selected = self._provider(
            selected_name,
            step=step,
            case_directory=case_directory if selected_name == "openfoam" else None,
            container_image=container_image,
        )
        write_marker(status="running", phase="solver")
        try:
            if _resume_archive is not None:
                if not isinstance(selected, OpenFOAMChannelProvider):
                    raise ProjectError(
                        "Managed checkpoint resume currently supports the transient "
                        "OpenFOAM channel provider only."
                    )
                result = selected.run(
                    step,
                    restart_archive=_resume_archive,
                    source_run_id=_resume_source_run_id,
                    expected_analysis_sha256=str(
                        plan["model"]["analysis_sha256"]
                    ),
                )
            else:
                result = selected.run(step)
            provider_analysis_sha256 = result.provenance.get("analysis_sha256")
            expected_analysis_sha256 = str(plan["model"]["analysis_sha256"])
            if provider_analysis_sha256 is None:
                result.provenance["analysis_sha256"] = expected_analysis_sha256
            elif provider_analysis_sha256 != expected_analysis_sha256:
                raise ProjectError(
                    "Provider returned a result for a different analysis identity: "
                    f"expected {expected_analysis_sha256}, got "
                    f"{provider_analysis_sha256!r}."
                )
        except Exception as error:
            write_workspace_marker(retention_reason="failure", protected=False)
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
        manifest_retention = bool(
            self._openfoam_settings().get("keep_workspace", False)
        )
        retained_workspace = selected_name == "openfoam" and (
            keep_workspace or manifest_retention or result.status != "completed"
        )
        retention_reason = (
            "explicit-cli"
            if selected_name == "openfoam" and keep_workspace
            else "manifest-policy"
            if selected_name == "openfoam" and manifest_retention
            else "failure"
            if selected_name == "openfoam" and result.status != "completed"
            else "none"
        )
        if retained_workspace:
            write_workspace_marker(
                retention_reason=retention_reason,
                protected=retention_reason in {"explicit-cli", "manifest-policy"},
            )
        result.provenance["result_profile"] = marker_record["result_profile"]
        if _promotion_source_run_id is not None:
            result.provenance["promotion"] = marker_record["promotion"]
        bundle = None
        if (
            selected_name == "openfoam"
            and result.status == "completed"
            and plan["decisions"]["portable_field_bundle"] is True
        ):
            write_marker(status="exporting", phase="portable-fields")
            try:
                bundle = data_exchange.export_openfoam_case(
                    case_directory,
                    run_directory / "fields",
                    container_image=selected.container_image,
                    density=step.model.fluid.density,
                    axis={
                        "name": "solver_iteration"
                        if step.model.study.steady
                        else "time",
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
                        "mesh_sha256": result.provenance.get("mesh_sha256"),
                        "result_status": result.status,
                        "trust_level": result.trust_level,
                        "accepted": result.accepted,
                        "case_directory": (
                            str(case_directory.resolve()) if retained_workspace else None
                        ),
                        "case_directory_retained": retained_workspace,
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
            portable_mesh_sha256 = manifest.get("mesh", {}).get("source_sha256")
            for record in manifest["fields"]:
                name = record["export_name"]
                result.fields[name] = FieldRecord(
                    unit=record["unit"],
                    location=record["association"],
                    artifact=str(bundle.xdmf.relative_to(run_directory)),
                    components=tuple(record["components"]),
                    representation="xdmf-hdf5",
                    mesh_sha256=portable_mesh_sha256,
                    description=record["description"],
                    processing={"operation": record["processing"]},
                )
            if step.output.views:
                recipe_manifest, recipe_scripts = (
                    postprocessing.publish_paraview_recipes(
                        run_directory,
                        step.output.views,
                        manifest,
                    )
                )
                result.artifacts["postprocess.manifest"] = Artifact.from_path(
                    recipe_manifest,
                    role="post-processing-recipe-index",
                    media_type="application/json",
                )
                for script in recipe_scripts:
                    result.artifacts[f"postprocess.{script.stem}"] = Artifact.from_path(
                        script,
                        role="paraview-python-recipe",
                        media_type="text/x-python",
                    )
        if selected_name == "openfoam":
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
                for empty_parent in (
                    workspace_root.parent,
                    workspace_root.parent.parent,
                ):
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
        run_record["resume_execution_sha256"] = resume_execution_sha256
        run_record["result_execution_sha256"] = result_execution_sha256
        run_record["result_profile"] = marker_record["result_profile"]
        run_record["performance_key"] = performance_key
        run_record["phase"] = "complete"
        run_record["pid"] = None
        run_record["started_at"] = started_at
        run_record["completed_at"] = datetime.now(UTC).isoformat()
        run_record["workspace_retention"] = {
            "retained": retained_workspace,
            "reason": retention_reason,
            "protected_from_default_cleanup": retention_reason
            in {"explicit-cli", "manifest-policy"},
        }
        run_record["model_name"] = step.model.name
        run_record["reynolds_number"] = _inlet_reynolds(step)
        run_record["parameters"] = selected_parameters
        run_record["design_point_name"] = design_point_name
        run_record["quantities"] = {
            name: {"value": quantity.value, "unit": quantity.unit}
            for name, quantity in sorted(result.quantities.items())
        }
        mesh_acquisition = result.provenance.get("mesh_acquisition")
        if isinstance(mesh_acquisition, str):
            run_record["mesh_acquisition"] = mesh_acquisition
        if _resume_archive is not None:
            run_record["resume"] = marker_record["resume"]
        if _promotion_source_run_id is not None:
            run_record["promotion"] = marker_record["promotion"]
        _write_output_guide(completed, model_name=step.model.name)
        (run_directory / "run.json").write_text(
            json.dumps(run_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._record_performance(
            run_record=run_record,
            plan=plan,
            execution_provider=selected_name,
            performance_key=performance_key,
        )
        if (
            _resume_source_run_id is not None
            and result.status == "completed"
            and not keep_workspace
        ):
            source_workspace = (
                self.root / ".agentcfd" / "work" / _resume_source_run_id
            )
            if source_workspace.is_dir() and source_workspace != workspace_root:
                shutil.rmtree(source_workspace)
        return completed

    def resume(
        self,
        *,
        container_image: str | None = None,
        keep_workspace: bool = False,
    ) -> ProjectRun:
        """Resume an identical failed/interrupted transient run from a checkpoint."""

        if self.manifest.run_mode != "replace":
            raise ProjectError(
                "Managed resume currently requires replace mode; campaign retries keep "
                "their own immutable orchestration state."
            )
        runs = self._run_records()
        latest = runs[0] if runs else None
        if latest is None:
            raise ProjectError("No failed or interrupted run exists to resume.")
        source_parameters = self._parameters(
            latest.get("parameters")
            if isinstance(latest.get("parameters"), dict)
            else None
        )
        step = self.load_step(source_parameters)
        if (
            self.manifest.default_provider != "openfoam"
            or not isinstance(step.model.domain, RectangularChannel)
            or step.model.study.steady
        ):
            raise ProjectError(
                "Managed resume currently supports transient OpenFOAM channel projects only."
            )
        if not step.output.checkpoints.enabled:
            raise ProjectError(
                "This project declares no restart checkpoints. Add "
                "`restart=outputs.checkpoints(...)` to the output request before the run."
            )
        plan = self.plan(
            container_image=container_image,
            parameters=source_parameters,
            _step=step,
        )
        if plan["readiness"]["ready_to_run"] is not True:
            raise ProjectError("The current project is not ready for checkpoint resume.")
        native_status = str(latest.get("status", "unknown"))
        interrupted = native_status in {"preparing", "running", "exporting"} and not _process_is_alive(
            latest.get("pid")
        )
        if native_status != "failed" and not interrupted:
            raise ProjectError(
                "Only a failed or interrupted inactive run can be resumed."
            )
        analysis_sha256 = str(plan["model"]["analysis_sha256"])
        if latest.get("analysis_sha256") != analysis_sha256:
            raise ProjectError(
                "Project inputs changed after the interrupted run; start a fresh run "
                "instead of applying an incompatible checkpoint."
            )
        execution_sha256 = self._execution_fingerprint(
            analysis_sha256,
            provider="openfoam",
            container_image=container_image,
        )
        resume_execution_sha256 = self._resume_execution_fingerprint(
            analysis_sha256,
            provider="openfoam",
            container_image=container_image,
        )
        source_resume_execution = latest.get("resume_execution_sha256")
        execution_matches = (
            source_resume_execution == resume_execution_sha256
            if isinstance(source_resume_execution, str)
            else latest.get("execution_sha256") == execution_sha256
        )
        if not execution_matches:
            raise ProjectError(
                "Solver-affecting provider settings changed after the interrupted run; "
                "resume requires the identical mesh/runtime identity."
            )
        run_id = latest.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ProjectError("The source run has no stable run identity.")
        run_directory = self._record_directory(latest)
        published_archive = (
            run_directory / "evidence" / "restart.zip"
            if run_directory is not None
            else None
        )
        source_case = self.root / ".agentcfd" / "work" / run_id / "openfoam"
        if published_archive is not None and published_archive.is_file():
            archive = published_archive
        elif source_case.is_dir():
            archive, _latest_time = materialize_interrupted_restart(
                step,
                source_case,
                expected_analysis_sha256=analysis_sha256,
            )
        else:
            raise ProjectError(
                "No published or retained complete checkpoint exists for the latest run."
            )
        with tempfile.TemporaryDirectory(prefix="agentcfd-resume-") as temporary:
            staged_archive = Path(temporary) / "restart.zip"
            shutil.copy2(archive, staged_archive)
            return self.run(
                container_image=container_image,
                keep_workspace=keep_workspace,
                parameters=source_parameters,
                _resume_archive=staged_archive,
                _resume_source_run_id=run_id,
            )


_CASE_TEMPLATE = '''"""Readable AgentCFD engineering model: edit this file, not backend dictionaries."""

from agentcfd import Model, boundaries, fluids, geometry, outputs, parameters, procedures, studies


@parameters.describe(
    length=parameters.number(
        "Pipe length", unit="m", minimum=0, exclusive_minimum=True,
        description="Axial length of the circular fluid domain.",
    ),
    diameter=parameters.number(
        "Pipe diameter", unit="m", minimum=0, exclusive_minimum=True,
        description="Internal circular diameter.",
    ),
    mean_velocity=parameters.number(
        "Mean inlet velocity", unit="m/s", minimum=0, exclusive_minimum=True,
        description="Bulk inlet velocity used by the internal-flow model.",
    ),
)
def build(*, length=10.0, diameter=0.05, mean_velocity=0.02):
    model = Model(
        name="water-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=length, diameter=diameter),
        fluid=fluids.newtonian(
            "water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(mean_velocity),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    return model.step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
'''


_HEATED_PIPE_PARAMETER_DEFAULTS = {
    "length": 2.0,
    "diameter": 0.1,
    "mean_velocity": 0.01,
    "inlet_temperature": 300.0,
    "wall_heat_flux": 100.0,
}


def _heated_pipe_template(
    parameter_defaults: Mapping[str, object] | None = None,
) -> str:
    selected = dict(_HEATED_PIPE_PARAMETER_DEFAULTS)
    provided = {} if parameter_defaults is None else dict(parameter_defaults)
    unknown = sorted(set(provided) - set(selected))
    if unknown:
        raise ValueError(
            "Unknown heated-pipe parameter defaults: " + ", ".join(unknown) + "."
        )
    for name, value in provided.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Heated-pipe parameter {name!r} must be a number.")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"Heated-pipe parameter {name!r} must be finite.")
        if name != "wall_heat_flux" and normalized <= 0.0:
            raise ValueError(f"Heated-pipe parameter {name!r} must be positive.")
        if name == "wall_heat_flux" and normalized == 0.0:
            raise ValueError("Heated-pipe wall_heat_flux must be non-zero.")
        selected[name] = normalized

    return f'''"""Constant-property laminar heated-pipe validation workflow."""

from agentcfd import Model, boundaries, fluids, geometry, outputs, parameters, procedures, studies


@parameters.describe(
    length=parameters.number(
        "Pipe length", unit="m", minimum=0, exclusive_minimum=True,
        description="Axial length of the circular fluid domain.",
    ),
    diameter=parameters.number(
        "Pipe diameter", unit="m", minimum=0, exclusive_minimum=True,
        description="Internal circular diameter.",
    ),
    mean_velocity=parameters.number(
        "Mean inlet velocity", unit="m/s", minimum=0, exclusive_minimum=True,
        description="Bulk velocity of the fully developed laminar inlet.",
    ),
    inlet_temperature=parameters.number(
        "Inlet temperature", unit="K", minimum=0, exclusive_minimum=True,
        description="Absolute mixed-mean inlet temperature.",
    ),
    wall_heat_flux=parameters.number(
        "Wall heat flux into fluid", unit="W/m^2",
        description="Signed heat flux; positive values heat the fluid.",
    ),
)
def build(
    *,
    length={selected["length"]!r},
    diameter={selected["diameter"]!r},
    mean_velocity={selected["mean_velocity"]!r},
    inlet_temperature={selected["inlet_temperature"]!r},
    wall_heat_flux={selected["wall_heat_flux"]!r},
):
    model = Model(
        name="heated-water-pipe",
        study=studies.internal_flow(energy=True),
        domain=geometry.circular_pipe(length=length, diameter=diameter),
        fluid=fluids.newtonian(
            "constant-property-water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
            specific_heat=4180.0,
            thermal_conductivity=0.6,
        ),
    ).boundaries(
        inlet=boundaries.fully_developed_velocity_inlet(
            mean_velocity, temperature=inlet_temperature
        ),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(
            thermal=boundaries.heat_flux_into_fluid(wall_heat_flux)
        ),
    )
    return model.step(
        procedure=procedures.steady(
            relative_tolerance=1.0e-6,
            maximum_iterations=700,
        ),
        output=outputs.thermal_internal_flow(),
    )
'''


_BAFFLE_CHANNEL_TEMPLATE = '''"""Low-Re transient wake behind a bottom-attached baffle."""

from agentcfd import Model, boundaries, fluids, geometry, initialization, meshing, outputs, parameters, procedures, studies


@parameters.describe(
    mean_velocity=parameters.number(
        "Mean inlet velocity", unit="m/s", minimum=0, exclusive_minimum=True,
        description="Bulk velocity entering the baffled channel.",
    ),
    baffle_height=parameters.number(
        "Baffle height", unit="m", minimum=0, exclusive_minimum=True,
        description="Height of the bottom-attached vertical obstruction.",
    ),
)
def build(*, mean_velocity=0.5, baffle_height=0.12):
    channel = geometry.rectangular_channel(length=1.2, height=0.20, width=0.10).with_baffle(
        name="baffle", x=0.35, height=baffle_height, thickness=0.01, attached_to="bottom"
    )
    model = Model(
        name="bottom-baffle-wake",
        study=studies.internal_flow(steady=False),
        domain=channel,
        fluid=fluids.newtonian("viscous-liquid", density=1000.0, dynamic_viscosity=0.05),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(mean_velocity),
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
            storage_budget="2 GiB",
            restart=outputs.checkpoints(every=0.5, keep=2),
            reports=(
                outputs.probe("near-wake", at=(0.50, 0.05, 0.05)),
                outputs.surface_report(
                    "outlet-pressure", region="outlet", field="fluid.pressure"
                ),
                outputs.force_report("baffle-drag", regions=("baffle",)),
            ),
            views=(
                outputs.slice_view(
                    "midplane-vorticity",
                    field="fluid.vorticity",
                    origin=(0.6, 0.1, 0.05),
                    normal=(0.0, 0.0, 1.0),
                ),
                outputs.streamline_view(
                    "wake-streamlines",
                    seed_start=(0.02, 0.01, 0.05),
                    seed_end=(0.02, 0.19, 0.05),
                    seeds=40,
                ),
                outputs.line_profile(
                    "centerline-pressure",
                    field="fluid.pressure",
                    start=(0.05, 0.10, 0.05),
                    end=(1.15, 0.10, 0.05),
                    samples=121,
                ),
            ),
        ),
    )
'''


def _imported_internal_flow_template(
    *,
    model_name: str,
    asset: str,
    roles: Mapping[str, str],
    interior_point_m: tuple[float, float, float],
    inlet_velocity_m_s: tuple[float, float, float] | None,
    inlet_mass_flow_kg_s: float | None,
    inlet_total_gauge_pressure_pa: float | None,
    base_size_m: float,
    maximum_cells: int,
) -> str:
    default_velocity = inlet_velocity_m_s or (0.0, 0.0, 0.0)
    conditions = []
    for name, role in sorted(roles.items()):
        constructor = {
            "inlet": "inlet_condition",
            "outlet": "boundaries.pressure_outlet()",
            "wall": "boundaries.no_slip_wall()",
            "symmetry": "boundaries.symmetry()",
            "empty": "boundaries.symmetry()",
        }[role]
        conditions.append(f"        {name!r}: {constructor},")
    boundary_block = "\n".join(conditions)
    return f'''"""Imported internal flow: edit engineering intent, not OpenFOAM files."""

import json
from pathlib import Path

from agentcfd import (
    Model,
    boundaries,
    fluids,
    geometry,
    geometry_io,
    meshing,
    outputs,
    parameters,
    procedures,
    studies,
)


@parameters.describe(
    velocity_x=parameters.number(
        "Inlet velocity X", unit="m/s",
        description="Cartesian X component; the vector is active when non-zero.",
    ),
    velocity_y=parameters.number(
        "Inlet velocity Y", unit="m/s",
        description="Cartesian Y component; the vector is active when non-zero.",
    ),
    velocity_z=parameters.number(
        "Inlet velocity Z", unit="m/s",
        description="Cartesian Z component; the vector is active when non-zero.",
    ),
    mass_flow_rate=parameters.number(
        "Inlet mass flow", unit="kg/s", minimum=0, exclusive_minimum=True,
        nullable=True, description="Scalar laminar inlet flow; overrides velocity.",
    ),
    inlet_total_gauge_pressure=parameters.number(
        "Inlet total gauge pressure", unit="Pa", minimum=0,
        exclusive_minimum=True, nullable=True,
        description="Laminar total pressure against the zero-gauge static outlet.",
    ),
    density=parameters.number(
        "Fluid density", unit="kg/m^3", minimum=0, exclusive_minimum=True,
        description="Constant fluid density.",
    ),
    dynamic_viscosity=parameters.number(
        "Dynamic viscosity", unit="Pa*s", minimum=0, exclusive_minimum=True,
        description="Constant fluid dynamic viscosity.",
    ),
    base_size=parameters.number(
        "Base mesh size", unit="m", minimum=0, exclusive_minimum=True,
        description="Target background cell size before local surface refinement.",
    ),
    maximum_cells=parameters.integer(
        "Maximum cells", unit="1", minimum=1,
        description="Hard global cell-count safety limit.",
    ),
    turbulence_model=parameters.choice(
        "Turbulence model", ("k-omega-sst",), nullable=True,
        description="Leave null for laminar flow or select the released RANS model.",
    ),
    turbulence_intensity=parameters.number(
        "Turbulence intensity", unit="1", minimum=0, maximum=1,
        exclusive_minimum=True, nullable=True,
        description="Inlet RMS intensity as a fraction, not percent.",
    ),
    turbulence_length_scale=parameters.number(
        "Turbulence length scale", unit="m", minimum=0,
        exclusive_minimum=True, nullable=True,
        description="Explicit inlet turbulence length scale.",
    ),
)
def build(
    *,
    velocity_x={default_velocity[0]!r},
    velocity_y={default_velocity[1]!r},
    velocity_z={default_velocity[2]!r},
    mass_flow_rate={inlet_mass_flow_kg_s!r},
    inlet_total_gauge_pressure={inlet_total_gauge_pressure_pa!r},
    density=998.2,
    dynamic_viscosity=1.002e-3,
    base_size={base_size_m!r},
    maximum_cells={maximum_cells!r},
    turbulence_model=None,
    turbulence_intensity=None,
    turbulence_length_scale=None,
):
    root = Path(__file__).parent
    inspection = json.loads((root / "geometry/inspection.json").read_text())
    domain = geometry.imported_surface_from_inspection(
        inspection,
        asset={asset!r},
        interior_point_m={interior_point_m!r},
    )
    velocity = (velocity_x, velocity_y, velocity_z)
    velocity_active = any(component != 0.0 for component in velocity)
    if mass_flow_rate is not None and inlet_total_gauge_pressure is not None:
        raise ValueError(
            "Mass flow and inlet total gauge pressure cannot be active together."
        )
    if (
        mass_flow_rate is None
        and inlet_total_gauge_pressure is None
        and not velocity_active
    ):
        raise ValueError("A non-zero velocity or scalar inlet control is required.")
    direction_assessment = (
        geometry_io.validate_inlet_velocity_direction(inspection, velocity)
        if mass_flow_rate is None and inlet_total_gauge_pressure is None
        else None
    )
    if turbulence_model is None:
        if turbulence_intensity is not None or turbulence_length_scale is not None:
            raise ValueError(
                "Turbulence intensity and length scale require a turbulence model."
            )
        study = studies.internal_flow()
        if mass_flow_rate is not None:
            inlet_condition = boundaries.mass_flow_inlet(mass_flow_rate)
        elif inlet_total_gauge_pressure is not None:
            inlet_condition = boundaries.pressure_inlet(inlet_total_gauge_pressure)
        else:
            inlet_condition = boundaries.velocity_inlet(velocity)
        output_request = outputs.standard()
    else:
        if turbulence_model != "k-omega-sst":
            raise ValueError("Imported RANS currently supports k-omega-sst only.")
        if turbulence_intensity is None or turbulence_length_scale is None:
            raise ValueError(
                "Imported RANS requires explicit turbulence_intensity and "
                "turbulence_length_scale."
            )
        if mass_flow_rate is not None:
            raise ValueError(
                "Imported RANS mass-flow inlet is not released; use an explicit "
                "velocity vector."
            )
        if inlet_total_gauge_pressure is not None:
            raise ValueError(
                "Imported RANS pressure inlet is not released; use an explicit "
                "velocity vector."
            )
        study = studies.internal_flow(
            turbulence=turbulence_model,
            wall_treatment="blended-wall-functions",
        )
        inlet_condition = boundaries.turbulent_velocity_inlet(
            velocity,
            intensity=turbulence_intensity,
            length_scale=turbulence_length_scale,
        )
        output_request = outputs.turbulent_internal_flow(
            turbulence_model=turbulence_model,
        )
    boundary_conditions = {{
{boundary_block}
    }}
    model = Model(
        name={model_name!r},
        study=study,
        domain=domain,
        fluid=fluids.newtonian(
            "water",
            density=density,
            dynamic_viscosity=dynamic_viscosity,
        ),
        metadata=(
            {{"inlet_velocity_direction": direction_assessment}}
            if direction_assessment is not None
            else {{}}
        ),
    ).boundaries(**boundary_conditions)
    return model.step(
        procedure=procedures.steady(),
        mesh=meshing.automatic(
            base_size=base_size,
            maximum_cells=maximum_cells,
        ),
        output=output_request,
    )
'''


def init_project_from_request(
    directory: str | Path,
    request: Mapping[str, object],
    *,
    base_directory: str | Path | None = None,
) -> Project:
    """Create a readable project from a versioned, ephemeral creation request.

    The request is an automation and GUI boundary only. The generated
    ``case.py`` remains the project's scientific source of truth.
    """

    if not isinstance(request, Mapping):
        raise ProjectError("Project creation request must be a JSON object.")
    payload = dict(request)
    allowed = {
        "schema",
        "template",
        "provider",
        "geometry",
        "interior_point_m",
        "inlet_velocity_m_s",
        "inlet_mass_flow_kg_s",
        "inlet_total_gauge_pressure_pa",
        "mesh",
        "parameters",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProjectError(
            "Unknown project creation request keys: " + ", ".join(unknown) + "."
        )
    if payload.get("schema") != "agentcfd.project-creation-request/0.1":
        raise ProjectError("Unsupported AgentCFD project creation request schema.")
    template = payload.get("template")
    provider = payload.get("provider")
    if not isinstance(template, str) or not isinstance(provider, str):
        raise ProjectError("Project creation request requires template and provider strings.")
    if template == "heated-pipe":
        unexpected = sorted(
            key
            for key in (
                "geometry",
                "interior_point_m",
                "inlet_velocity_m_s",
                "inlet_mass_flow_kg_s",
                "inlet_total_gauge_pressure_pa",
                "mesh",
            )
            if key in payload
        )
        if unexpected:
            raise ProjectError(
                "Template 'heated-pipe' does not accept request sections: "
                + ", ".join(unexpected)
                + "."
            )
        parameter_defaults = payload.get("parameters")
        if parameter_defaults is not None and not isinstance(
            parameter_defaults, Mapping
        ):
            raise ProjectError("Heated-pipe request parameters must be an object.")
        try:
            return init_project(
                directory,
                provider=provider,
                template=template,
                parameter_defaults=parameter_defaults,
            )
        except ValueError as error:
            raise ProjectError(str(error)) from error
    if template != "imported-internal-flow":
        unexpected = sorted(
            key
            for key in (
                "geometry",
                "interior_point_m",
                "inlet_velocity_m_s",
                "inlet_mass_flow_kg_s",
                "inlet_total_gauge_pressure_pa",
                "mesh",
                "parameters",
            )
            if key in payload
        )
        if unexpected:
            raise ProjectError(
                f"Template {template!r} does not accept request sections: "
                + ", ".join(unexpected)
                + "."
            )
        return init_project(directory, provider=provider, template=template)

    missing = sorted(
        key
        for key in ("geometry", "interior_point_m", "mesh")
        if key not in payload
    )
    if missing:
        raise ProjectError(
            "Imported project creation request is missing: "
            + ", ".join(missing)
            + "."
        )
    inlet_controls = sum(
        key in payload
        for key in (
            "inlet_velocity_m_s",
            "inlet_mass_flow_kg_s",
            "inlet_total_gauge_pressure_pa",
        )
    )
    if inlet_controls != 1:
        raise ProjectError(
            "Imported project creation requires exactly one of "
            "inlet_velocity_m_s, inlet_mass_flow_kg_s, or "
            "inlet_total_gauge_pressure_pa."
        )
    geometry_record = payload["geometry"]
    mesh_record = payload["mesh"]
    if not isinstance(geometry_record, Mapping):
        raise ProjectError("Project creation geometry must be an object.")
    if not isinstance(mesh_record, Mapping):
        raise ProjectError("Project creation mesh must be an object.")
    geometry_unknown = sorted(
        set(geometry_record)
        - {"path", "unit", "boundary_roles", "role_confirmation"}
    )
    mesh_unknown = sorted(set(mesh_record) - {"base_size_m", "maximum_cells"})
    if geometry_unknown:
        raise ProjectError(
            "Unknown project creation geometry keys: "
            + ", ".join(geometry_unknown)
            + "."
        )
    if mesh_unknown:
        raise ProjectError(
            "Unknown project creation mesh keys: "
            + ", ".join(mesh_unknown)
            + "."
        )
    geometry_missing = sorted(
        key for key in ("path", "unit") if key not in geometry_record
    )
    mesh_missing = sorted(
        key for key in ("base_size_m", "maximum_cells") if key not in mesh_record
    )
    if geometry_missing or mesh_missing:
        raise ProjectError(
            "Project creation request is incomplete: "
            + ", ".join(
                [
                    *(f"geometry.{key}" for key in geometry_missing),
                    *(f"mesh.{key}" for key in mesh_missing),
                ]
            )
            + "."
        )
    geometry_path = geometry_record["path"]
    geometry_unit = geometry_record["unit"]
    boundary_roles = geometry_record.get("boundary_roles")
    role_confirmation = geometry_record.get("role_confirmation")
    if not isinstance(geometry_path, str) or not geometry_path.strip():
        raise ProjectError("Project creation geometry.path must be a non-empty string.")
    if not isinstance(geometry_unit, str):
        raise ProjectError("Project creation geometry.unit must be a string.")
    if (boundary_roles is None) == (role_confirmation is None):
        raise ProjectError(
            "Project creation geometry requires exactly one of boundary_roles or "
            "role_confirmation."
        )
    if boundary_roles is not None and not isinstance(boundary_roles, Mapping):
        raise ProjectError("Project creation geometry.boundary_roles must be an object.")
    if role_confirmation is not None and role_confirmation != "accept-name-suggestions":
        raise ProjectError(
            "Project creation geometry.role_confirmation must be "
            "'accept-name-suggestions'."
        )
    source = Path(geometry_path).expanduser()
    if not source.is_absolute():
        base = (
            Path.cwd()
            if base_directory is None
            else Path(base_directory).expanduser()
        )
        source = base.resolve() / source
    return init_project(
        directory,
        provider=provider,
        template=template,
        geometry_path=source,
        geometry_unit=geometry_unit,
        boundary_roles=boundary_roles,
        accept_name_roles=role_confirmation == "accept-name-suggestions",
        interior_point_m=payload["interior_point_m"],
        inlet_velocity_m_s=payload.get("inlet_velocity_m_s"),
        inlet_mass_flow_kg_s=payload.get("inlet_mass_flow_kg_s"),
        inlet_total_gauge_pressure_pa=payload.get(
            "inlet_total_gauge_pressure_pa"
        ),
        base_size_m=mesh_record["base_size_m"],
        maximum_cells=mesh_record["maximum_cells"],
    )


def init_project(
    directory: str | Path,
    *,
    provider: str = "reference",
    template: str = "industrial-pipe",
    geometry_path: str | Path | None = None,
    geometry_unit: str | None = None,
    boundary_roles: Mapping[str, str] | None = None,
    accept_name_roles: bool = False,
    interior_point_m: tuple[float, float, float] | None = None,
    inlet_velocity_m_s: tuple[float, float, float] | None = None,
    inlet_mass_flow_kg_s: float | None = None,
    inlet_total_gauge_pressure_pa: float | None = None,
    base_size_m: float | None = None,
    maximum_cells: int | None = None,
    parameter_defaults: Mapping[str, object] | None = None,
) -> Project:
    """Create a complete editable project without overwriting user data."""

    if provider not in {"reference", "openfoam"}:
        raise ValueError("Project provider must be 'reference' or 'openfoam'.")
    if template not in {
        "industrial-pipe",
        "heated-pipe",
        "baffle-channel",
        "imported-internal-flow",
    }:
        raise ValueError(
            "Project template must be 'industrial-pipe', 'heated-pipe', "
            "'baffle-channel', or 'imported-internal-flow'."
        )
    if template in {
        "heated-pipe",
        "baffle-channel",
        "imported-internal-flow",
    } and provider != "openfoam":
        raise ValueError(f"The {template} template requires provider='openfoam'.")
    imported_options = (
        geometry_path,
        geometry_unit,
        boundary_roles,
        accept_name_roles,
        interior_point_m,
        inlet_velocity_m_s,
        inlet_mass_flow_kg_s,
        inlet_total_gauge_pressure_pa,
        base_size_m,
        maximum_cells,
    )
    if template != "imported-internal-flow" and any(
        value is not None and value is not False for value in imported_options
    ):
        raise ValueError(
            "Imported geometry options require template='imported-internal-flow'."
        )
    if template != "heated-pipe" and parameter_defaults is not None:
        raise ValueError(
            "parameter_defaults currently require template='heated-pipe'."
        )

    imported_report: dict[str, object] | None = None
    imported_source: Path | None = None
    imported_asset: str | None = None
    normalized_roles: dict[str, str] | None = None
    case_template: str
    if template == "imported-internal-flow":
        if not isinstance(accept_name_roles, bool):
            raise ValueError("accept_name_roles must be a boolean.")
        if boundary_roles is not None and accept_name_roles:
            raise ValueError(
                "Choose either explicit boundary_roles or accept_name_roles, not both."
            )
        missing = [
            name
            for name, value in (
                ("geometry_path", geometry_path),
                ("geometry_unit", geometry_unit),
                ("boundary_roles or accept_name_roles", boundary_roles or accept_name_roles),
                ("interior_point_m", interior_point_m),
                ("base_size_m", base_size_m),
                ("maximum_cells", maximum_cells),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                "Imported internal-flow initialization requires: "
                + ", ".join(missing)
                + "."
            )
        inlet_control_count = sum(
            value is not None
            for value in (
                inlet_velocity_m_s,
                inlet_mass_flow_kg_s,
                inlet_total_gauge_pressure_pa,
            )
        )
        if inlet_control_count != 1:
            raise ValueError(
                "Imported internal-flow initialization requires exactly one of "
                "inlet_velocity_m_s, inlet_mass_flow_kg_s, or "
                "inlet_total_gauge_pressure_pa."
            )
        selected_velocity = (
            boundaries.VelocityInlet(inlet_velocity_m_s).velocity
            if inlet_velocity_m_s is not None
            else None
        )
        selected_mass_flow = (
            boundaries.MassFlowInlet(inlet_mass_flow_kg_s).mass_flow_rate
            if inlet_mass_flow_kg_s is not None
            else None
        )
        selected_total_pressure = (
            boundaries.PressureInlet(
                inlet_total_gauge_pressure_pa
            ).total_gauge_pressure
            if inlet_total_gauge_pressure_pa is not None
            else None
        )
        if selected_total_pressure is not None and selected_total_pressure <= 0.0:
            raise ValueError(
                "Imported inlet total gauge pressure must exceed the default "
                "zero-gauge outlet pressure."
            )
        assert geometry_path is not None
        assert geometry_unit is not None
        assert interior_point_m is not None
        assert base_size_m is not None
        assert maximum_cells is not None
        imported_source = Path(geometry_path).expanduser().resolve()
        if boundary_roles is None:
            preliminary_report = geometry_io.inspect_geometry(
                imported_source,
                unit=geometry_unit,
                internal_flow=True,
            )
            normalized_roles = geometry_io.accept_name_role_suggestions(
                preliminary_report
            )
        else:
            if not isinstance(boundary_roles, Mapping):
                raise ValueError("Imported boundary_roles must be a mapping.")
            normalized_roles = {
                str(name): str(role).strip().lower()
                for name, role in boundary_roles.items()
            }
        imported_report = geometry_io.inspect_geometry(
            imported_source,
            unit=geometry_unit,
            boundary_roles=normalized_roles,
            internal_flow=True,
        )
        if not imported_report["readiness"]["ready_for_import_setup"]:
            repairs = [
                str(issue["repair"])
                for issue in imported_report["issues"]
                if issue["severity"] == "error"
            ]
            raise ProjectError(
                "Imported geometry is not ready: " + " ".join(repairs)
            )
        role_values = tuple(normalized_roles.values())
        unsupported = sorted(set(role_values) - {"inlet", "outlet", "wall", "symmetry", "empty"})
        if unsupported:
            raise ProjectError(
                "The released imported internal-flow template does not support roles: "
                + ", ".join(unsupported)
                + "."
            )
        if role_values.count("inlet") != 1 or role_values.count("outlet") != 1:
            raise ProjectError(
                "The released imported internal-flow template requires exactly one inlet and one outlet."
            )
        if selected_velocity is not None:
            geometry_io.validate_inlet_velocity_direction(
                imported_report,
                selected_velocity,
            )
        invalid_names = sorted(
            name
            for name in normalized_roles
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        )
        if invalid_names:
            raise ProjectError(
                "Imported surface names must be OpenFOAM-compatible words: "
                + ", ".join(invalid_names)
                + "."
            )
        if (
            isinstance(base_size_m, bool)
            or not isinstance(base_size_m, (int, float))
            or not math.isfinite(base_size_m)
            or base_size_m <= 0.0
        ):
            raise ValueError("Imported base_size_m must be positive and finite.")
        if (
            isinstance(maximum_cells, bool)
            or not isinstance(maximum_cells, int)
            or maximum_cells < 1
        ):
            raise ValueError("Imported maximum_cells must be a positive integer.")
        selected_interior = tuple(float(value) for value in interior_point_m)
        if len(selected_interior) != 3 or any(
            not math.isfinite(value) for value in selected_interior
        ):
            raise ValueError("Imported interior_point_m requires three finite values.")
        imported_asset = f"geometry/{imported_source.name}"
        # Constructing the domain validates bounds and the explicit interior seed.
        from .geometry import imported_surface_from_inspection

        imported_surface_from_inspection(
            imported_report,
            asset=imported_asset,
            interior_point_m=selected_interior,
        )
        case_template = _imported_internal_flow_template(
            model_name=Path(directory).name or "imported-internal-flow",
            asset=imported_asset,
            roles=normalized_roles,
            interior_point_m=selected_interior,
            inlet_velocity_m_s=selected_velocity,
            inlet_mass_flow_kg_s=selected_mass_flow,
            inlet_total_gauge_pressure_pa=selected_total_pressure,
            base_size_m=float(base_size_m),
            maximum_cells=maximum_cells,
        )
    else:
        case_template = (
            _BAFFLE_CHANNEL_TEMPLATE
            if template == "baffle-channel"
            else _heated_pipe_template(parameter_defaults)
            if template == "heated-pipe"
            else _CASE_TEMPLATE
        )
    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Project directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    pipe_mesh_settings = (
        (
            "cross_section_cells = 16\naxial_cells = 80\n"
            if template == "heated-pipe"
            else "cross_section_cells = 8\naxial_cells = 120\n"
        )
        if template in {"industrial-pipe", "heated-pipe"}
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
timeout_seconds = 3600
'''
    (root / "agentcfd.toml").write_text(manifest, encoding="utf-8")
    (root / "case.py").write_text(case_template, encoding="utf-8")
    if imported_report is not None:
        assert imported_source is not None
        assert imported_asset is not None
        assert normalized_roles is not None
        geometry_directory = root / "geometry"
        geometry_directory.mkdir()
        shutil.copy2(imported_source, root / imported_asset)
        portable_report = json.loads(json.dumps(imported_report))
        portable_report["source"]["path"] = imported_asset
        _write_json_atomic(geometry_directory / "inspection.json", portable_report)
        _write_json_atomic(
            geometry_directory / "boundary-roles.json",
            {
                "schema": "agentcfd.boundary-role-map/0.1",
                # Imported setup above guarantees a normalized mapping here.
                "regions": dict(sorted(normalized_roles.items())),
            },
        )
    (root / "README.md").write_text(
        f"# AgentCFD {template}\n\n"
        + (
            "The owned geometry, explicit units, confirmed boundary roles, and "
            "inspection record live in `geometry/`. `case.py` contains the inlet "
            "vector, fluid, mesh size, and hard cell budget. It defaults to laminar; "
            "set turbulence_model, turbulence_intensity, and "
            "turbulence_length_scale together for the guarded k-omega SST path.\n\n"
            if template == "imported-internal-flow"
            else (
                "This template is the bounded constant-property laminar thermal "
                "slice. Positive wall heat flux enters the fluid. Review the plan's "
                "thermal preflight before running; accepted results require both "
                "pressure and energy closure. It is not a steam or buoyancy model.\n\n"
                if template == "heated-pipe"
                else ""
            )
        )
        + "Edit `case.py`, then run `agentcfd status .` and follow its one recommended "
        "next action. The normal loop is `agentcfd run .` followed by "
        "`agentcfd view .`; `check`, `plan`, and `inspect` remain available for deeper "
        "diagnosis. Use `agentcfd project .` for one combined project, result, output, "
        "and safe-action view. Use `agentcfd params . --output operating-point.json` to freeze "
        "one validated set of editable inputs without changing `case.py`. "
        "Failed transient runs expose identity-gated `agentcfd resume .` "
        "when a complete checkpoint exists. Ordinary runs replace the managed "
        "`output/` directory. Add `--summary-only` for compact OpenFOAM evidence "
        "without permanent fields. Use "
        "`agentcfd run . --campaign` to preserve an immutable run, "
        "`agentcfd storage .` to audit space, or `--keep-workspace` only for expert "
        "solver debugging.\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# Agent instructions\n\n"
        "Treat `case.py` as the modeling source of truth. Begin with "
        "`agentcfd project . --json`, inspect its typed `next_action.operation`, execute "
        "only the corresponding declared command when authorized, and re-read the "
        "snapshot after each action. Do not edit generated OpenFOAM dictionaries to "
        "change scientific intent. Preserve plan, result, XDMF/H5, selected NPZ, and "
        "failed checks together. Use `agentcfd performance . --json` only for "
        "scheduling; it is not scientific evidence. Run `agentcfd verify project "
        ". --json` before a handoff. Follow `resume_after_repair` only after the diagnosed "
        "cause is fixed. Never promote a result whose `accepted` value is false.\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "output/\ncampaigns/\n.agentcfd/\n__pycache__/\n",
        encoding="utf-8",
    )
    return Project(root)


def open_project(start: str | Path = ".") -> Project:
    """Open the nearest readable AgentCFD project from any nested path."""

    return Project.discover(start)


__all__ = [
    "Project",
    "ProjectIssue",
    "ProjectManifest",
    "ProjectRun",
    "init_project",
    "init_project_from_request",
    "open_project",
]
