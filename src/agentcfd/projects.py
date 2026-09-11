"""One readable project lifecycle shared by people, agents, CLIs, and GUIs."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import threading
import tomllib
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Mapping, Sequence

from . import (
    archives,
    boundaries,
    campaign_plotting,
    data_exchange,
    diagnostics,
    engineering,
    geometry_generation,
    geometry_io,
    outputs,
    parameters as parameter_definitions,
    postprocessing,
    templates as project_templates,
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


_RUN_ALLOCATION_LOCK = threading.Lock()
_PERFORMANCE_HISTORY_LOCK = threading.Lock()
_MAX_CAMPAIGN_PARALLELISM = 32


def _project_record_path_matches(
    value: object,
    expected: Path,
    *,
    root: Path,
) -> bool:
    """Match legacy absolute and portable project-relative record paths."""

    if not isinstance(value, str) or not value:
        return False
    recorded = Path(value)
    candidate = recorded if recorded.is_absolute() else root / recorded
    return candidate.resolve() == expected.resolve()


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
    template: str | None
    default_provider: str
    run_directory: str
    run_mode: str
    generated_geometry_spec: str | None
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
            "template",
            "default_provider",
            "run_directory",
            "run_mode",
            "generated_geometry_spec",
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
        template = payload.get("template")
        if template is not None:
            if not isinstance(template, str):
                raise ProjectError("Project template must be a string when present.")
            try:
                project_templates.get(template)
            except ValueError as error:
                raise ProjectError(str(error)) from error
        openfoam = payload.get("openfoam", {})
        if not isinstance(openfoam, dict):
            raise ProjectError("Project [openfoam] settings must be a table.")
        run_mode = payload.get("run_mode", "campaign")
        if run_mode not in {"replace", "campaign"}:
            raise ProjectError("Project run_mode must be 'replace' or 'campaign'.")
        generated_geometry_spec = payload.get("generated_geometry_spec")
        if generated_geometry_spec is not None and generated_geometry_spec != (
            "geometry/spec.json"
        ):
            raise ProjectError(
                "Project generated_geometry_spec currently supports only "
                "'geometry/spec.json'."
            )
        return cls(
            entrypoint=str(strings["entrypoint"]).strip(),
            factory=str(strings["factory"]).strip(),
            template=template,
            default_provider=provider,
            run_directory=str(strings["run_directory"]).strip(),
            run_mode=str(run_mode),
            generated_geometry_spec=generated_geometry_spec,
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
            "summary": str(self.directory / "summary.json"),
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
        mean_velocity = inlet.mass_flow_rate / (step.model.fluid.density * domain.area)
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
            thermal.heat_flux_into_fluid * math.pi * domain.diameter * domain.length
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


_PROJECT_ACTION_POLICIES = {
    "archive": ("maintain", False, False),
    "campaigns": ("observe", False, False),
    "check": ("observe", False, False),
    "clean": ("maintain", False, False),
    "diagnose": ("observe", False, False),
    "geometry-sync": ("maintain", True, False),
    "logs": ("observe", False, False),
    "observations": ("observe", False, False),
    "performance": ("observe", False, False),
    "plan": ("observe", False, False),
    "project": ("observe", False, False),
    "result": ("review", False, False),
    "run": ("execute", True, True),
    "status": ("observe", False, False),
    "storage": ("observe", False, False),
    "verify": ("review", False, False),
    "view": ("review", False, False),
    "watch": ("observe", False, False),
}


_GENERATED_ELBOW_PARAMETER_NAMES = (
    "diameter_m",
    "bend_radius_m",
    "inlet_length_m",
    "outlet_length_m",
    "cross_section_segments",
    "bend_segments",
    "inlet_segments",
    "outlet_segments",
)


def _normalize_generated_elbow_spec(
    payload: Mapping[str, object],
    *,
    target: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate one editable geometry spec and resolve its exact artifact plan."""

    allowed = {"schema", "type", "parameters"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProjectError(
            "Unknown generated geometry specification keys: "
            + ", ".join(unknown)
            + "."
        )
    if payload.get("schema") != "agentcfd.generated-geometry-spec/0.1":
        raise ProjectError("Unsupported generated geometry specification schema.")
    if payload.get("type") != "circular-elbow-90deg":
        raise ProjectError("Only circular-elbow-90deg generated geometry is supported.")
    raw_parameters = payload.get("parameters")
    if not isinstance(raw_parameters, Mapping):
        raise ProjectError("Generated geometry parameters must be an object.")
    parameter_unknown = sorted(
        set(raw_parameters) - set(_GENERATED_ELBOW_PARAMETER_NAMES)
    )
    parameter_missing = sorted(
        set(_GENERATED_ELBOW_PARAMETER_NAMES) - set(raw_parameters)
    )
    if parameter_unknown or parameter_missing:
        details = [
            *(f"unknown parameters: {', '.join(parameter_unknown)}" for _ in [0] if parameter_unknown),
            *(f"missing parameters: {', '.join(parameter_missing)}" for _ in [0] if parameter_missing),
        ]
        raise ProjectError("Generated geometry specification has " + "; ".join(details) + ".")
    try:
        plan = geometry_generation.plan_circular_elbow_stl(
            target,
            **{name: raw_parameters[name] for name in _GENERATED_ELBOW_PARAMETER_NAMES},
        )
    except (TypeError, ValueError) as error:
        raise ProjectError(f"Generated geometry specification is invalid: {error}") from error
    resolved = plan["geometry"]["parameters"]
    assert isinstance(resolved, dict)
    return (
        {
            "schema": "agentcfd.generated-geometry-spec/0.1",
            "type": "circular-elbow-90deg",
            "parameters": dict(resolved),
        },
        plan,
    )


def _portable_generated_geometry_report(
    report: Mapping[str, object],
    *,
    next_command: str,
) -> dict[str, object]:
    """Convert an exact generator report into a movable project record."""

    portable = json.loads(json.dumps(report))
    portable["artifact"]["path"] = "geometry/fluid.stl"
    portable["artifact"]["written"] = True
    portable["artifact"]["already_exists"] = False
    portable["recommendations"]["project_initialization"]["geometry_path"] = (
        "geometry/fluid.stl"
    )
    portable["next_action"] = {
        "command": next_command,
        "reason": "Inspect project readiness before starting the solver.",
    }
    return portable


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
    if operation not in _PROJECT_ACTION_POLICIES:
        raise ProjectError(
            f"Project next action uses unsupported operation {operation!r}."
        )
    kind, mutates_project, starts_solver = _PROJECT_ACTION_POLICIES[operation]
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
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
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
            key: value for key, value in record.items() if key in {"type", "thermal"}
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
            or set(record) != {"schema", "updated_at", "maximum_samples", "samples"}
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


def _valid_optional_number(value: object) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _checkpoint_progress(
    root: Path,
    record: Mapping[str, object],
    plan: Mapping[str, object],
) -> tuple[dict[str, object], int]:
    """Read only the small manifest of an atomically published restart ZIP."""

    decisions = plan.get("decisions", {})
    output_plan = decisions.get("output_plan", {}) if isinstance(decisions, dict) else {}
    channels = output_plan.get("channels", {}) if isinstance(output_plan, dict) else {}
    policy = channels.get("checkpoints", {}) if isinstance(channels, dict) else {}
    enabled = policy.get("enabled") is True if isinstance(policy, dict) else False
    directory = Path(str(record.get("directory", "")))
    if not directory.is_absolute():
        directory = root / directory
    archive = directory / "evidence" / "restart.zip"
    report: dict[str, object] = {
        "status": "awaiting-first-checkpoint" if enabled else "disabled",
        "path": str(archive) if enabled else None,
        "retained_times": [],
        "in_run_publication_count": 0,
        "first_in_run_publication_time": None,
        "latest_in_run_publication_time": None,
        "latest_time": None,
        "unit": "s",
        "size_bytes": None,
        "atomic_publication": None,
        "bounded_memory_streaming": None,
    }
    if not enabled or not archive.is_file():
        return report, 0
    metadata_bytes = 0
    try:
        size = archive.stat().st_size
        report["size_bytes"] = size
        with zipfile.ZipFile(archive) as bundle:
            info = bundle.getinfo("restart.json")
            if info.file_size > 256 * 1024:
                raise ValueError("Restart metadata exceeds the observation bound.")
            payload = bundle.read(info)
        metadata_bytes = len(payload)
        metadata = strict_json_object(
            payload.decode("utf-8"),
            label="AgentCFD rolling checkpoint metadata",
        )
        retained = metadata.get("retained_times")
        publication_count = metadata.get("in_run_publication_count", 0)
        first_publication = metadata.get("first_in_run_publication_time")
        latest_publication = metadata.get("latest_in_run_publication_time")
        latest = metadata.get("latest_time")
        if (
            metadata.get("schema") != "agentcfd.openfoam-restart/0.1"
            or not isinstance(retained, list)
            or not retained
            or isinstance(publication_count, bool)
            or not isinstance(publication_count, int)
            or publication_count < 0
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in retained
            )
            or not _valid_optional_number(first_publication)
            or not _valid_optional_number(latest_publication)
            or (publication_count == 0 and first_publication is not None)
            or (publication_count == 0 and latest_publication is not None)
            or (publication_count > 0 and first_publication is None)
            or (publication_count > 0 and latest_publication is None)
            or (
                publication_count > 0
                and float(first_publication) > float(latest_publication)
            )
            or isinstance(latest, bool)
            or not isinstance(latest, (int, float))
            or not math.isfinite(float(latest))
            or not math.isclose(float(latest), float(retained[-1]))
        ):
            raise ValueError("Restart metadata is malformed.")
    except (KeyError, OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile):
        report["status"] = "invalid"
        return report, metadata_bytes
    report.update(
        {
            "status": "available",
            "retained_times": [float(value) for value in retained],
            "in_run_publication_count": publication_count,
            "first_in_run_publication_time": (
                float(first_publication) if first_publication is not None else None
            ),
            "latest_in_run_publication_time": (
                float(latest_publication) if latest_publication is not None else None
            ),
            "latest_time": float(latest),
            "size_bytes": size,
            "atomic_publication": metadata.get("atomic_publication") is True,
            "bounded_memory_streaming": (
                metadata.get("bounded_memory_streaming") is True
            ),
        }
    )
    return report, metadata_bytes


def _field_export_progress_report(
    record: Mapping[str, object],
) -> dict[str, object] | None:
    raw = record.get("field_export")
    if not isinstance(raw, dict):
        return None
    phase = raw.get("phase")
    completed = raw.get("completed_frames")
    total = raw.get("total_frames")
    fraction = raw.get("fraction")
    batch_index = raw.get("batch_index")
    batch_count = raw.get("batch_count")
    current_batch = raw.get("current_batch_frames")
    maximum_batch = raw.get("maximum_batch_frames")
    updated_at = raw.get("updated_at")
    integer_values = (completed, total, batch_count, maximum_batch)
    optional_integers = (batch_index, current_batch)
    parsed_updated_at = None
    if isinstance(updated_at, str):
        try:
            parsed_updated_at = datetime.fromisoformat(updated_at)
        except ValueError:
            pass
    if (
        raw.get("schema") != "agentcfd.field-export-progress/0.1"
        or phase not in {"preparing", "converting", "writing", "publishing", "complete"}
        or any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values)
        or any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int))
            for value in optional_integers
        )
        or total <= 0
        or completed < 0
        or completed > total
        or maximum_batch != data_exchange.OPENFOAM_CONVERSION_BATCH_FRAMES
        or batch_count != math.ceil(total / maximum_batch)
        or (batch_index is not None and not 1 <= batch_index <= batch_count)
        or (current_batch is not None and not 1 <= current_batch <= maximum_batch)
        or isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(float(fraction))
        or not math.isclose(float(fraction), completed / total)
        or parsed_updated_at is None
        or parsed_updated_at.tzinfo is None
        or (
            phase == "preparing"
            and (completed != 0 or batch_index is not None or current_batch is not None)
        )
        or (
            phase == "converting"
            and (
                batch_index is None
                or current_batch
                != min(maximum_batch, total - (batch_index - 1) * maximum_batch)
                or completed != (batch_index - 1) * maximum_batch
            )
        )
        or (
            phase == "writing"
            and (
                batch_index is None
                or current_batch
                != min(maximum_batch, total - (batch_index - 1) * maximum_batch)
                or completed != (batch_index - 1) * maximum_batch
            )
        )
        or (
            phase in {"publishing", "complete"}
            and (completed != total or batch_index != batch_count or current_batch is not None)
        )
    ):
        return None
    return {
        "schema": "agentcfd.field-export-progress/0.1",
        "phase": phase,
        "completed_frames": completed,
        "total_frames": total,
        "fraction": float(fraction),
        "batch_index": batch_index,
        "batch_count": batch_count,
        "current_batch_frames": current_batch,
        "maximum_batch_frames": maximum_batch,
        "updated_at": updated_at,
    }


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
    field_export = _field_export_progress_report(record)
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
    if field_export is not None:
        try:
            updated_timestamps.append(
                datetime.fromisoformat(str(field_export["updated_at"])).timestamp()
            )
        except ValueError:
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
    if record.get("status") == "exporting":
        # Solver calibration does not predict converter or HDF5 throughput.
        remaining_range = None

    checkpoint, checkpoint_bytes = _checkpoint_progress(root, record, plan)

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
        "field_export": field_export,
        "checkpoint": checkpoint,
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
            "checkpoint_metadata_bytes_read": checkpoint_bytes,
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
                "Multi-view overviews use `agentcfd view . --layout NAME --launch`.",
                "These scripts share `fields/fields.h5` and do not duplicate volume data.",
                "",
            )
        )
    lines.extend(
        (
            "Read `summary.json` first for compact quantities, decisions, and available data.",
            "Read `result.json` only when complete histories and evidence metadata are needed.",
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
        native_solver_field_bytes = (
            requested_frames * estimated_cells * scalar_components * 8
        )
        single_vtu_frame_bytes = (
            math.ceil(entities * scalar_components * 8) + mesh_bytes
        )
        managed_vtu_frames = min(
            requested_frames,
            data_exchange.OPENFOAM_CONVERSION_BATCH_FRAMES,
        )
        maximum_vtu_batch_bytes = managed_vtu_frames * single_vtu_frame_bytes
        # The converter now creates one bounded micro-batch before HDF5
        # consumption, so temporary conversion cost has a fixed ceiling rather
        # than scaling with animation length. Native solver frames still coexist
        # with the growing portable bundle until publication completes.
        raw_staging_bytes = (
            estimated_portable_bytes
            + native_solver_field_bytes
            + maximum_vtu_batch_bytes
            if export_fields
            else native_solver_field_bytes + mesh_bytes
        )
        temporary_peak_safety_factor = 1.25
        estimated_temporary_peak_bytes = math.ceil(
            temporary_peak_safety_factor * raw_staging_bytes
        )
        imported_bound = isinstance(step.model.domain, ImportedSurface)
        estimate_calibration = {
            "method": (
                "snappy-hard-cell-bound-plus-bounded-vtu-staging"
                if imported_bound and export_fields
                else "snappy-hard-cell-bound-native-only"
                if imported_bound
                else "native-plus-bounded-vtu-stream-conservative-measured-headroom"
                if export_fields
                else "native-solver-only-summary-with-conservative-headroom"
            ),
            "uncompressed_requested_field_bytes": field_bytes,
            "native_solver_field_bytes": native_solver_field_bytes,
            "maximum_vtu_batch_bytes": (
                maximum_vtu_batch_bytes if export_fields else 0
            ),
            "maximum_managed_vtu_frames": (
                managed_vtu_frames if export_fields else 0
            ),
            "raw_staging_bytes": raw_staging_bytes,
            "safety_factor": temporary_peak_safety_factor,
            "evidence": (
                "Imported geometry uses maximum_cells/maxGlobalCells as a fail-safe "
                "upper bound; the checked duct resolved 6,400 of 200,000 allowed cells."
                if imported_bound
                else "OpenCFD-v2606 four-frame baffled-channel conversion: one "
                "managed staging directory and four VTU frames observed at peak"
            ),
        }

    return {
        "observation_catalog": step.observation_catalog(),
        "channels": {
            "histories": {
                "names": list(step.output.histories),
                "retention": "all scalar samples",
            },
            "reports": {
                "definitions": [item.to_dict() for item in step.output.reports],
                "retention": "all compact samples",
            },
            "criteria": {
                "definitions": [item.to_dict() for item in step.output.criteria],
                "evaluation": "inclusive bounds after canonical scalar recovery",
            },
            "views": {
                "definitions": [item.to_dict() for item in step.output.views],
                "retention": (
                    "scripts and state only; shared portable fields"
                    if export_fields
                    else "not published in summary-only result profile"
                ),
            },
            "layouts": {
                "definitions": [item.to_dict() for item in step.output.layouts],
                "retention": (
                    "scripts and optional renders only; shared portable fields"
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
                raise ProjectError(f"Project parameter {name!r} must be a JSON scalar.")
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
            overrideable = keyword_overrideable and (
                not has_default or default_is_scalar
            )
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

    def observations(
        self,
        parameters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Resolve the output target catalog without starting a solver."""

        return self.load_step(parameters).observation_catalog()

    def _generated_geometry_state(self) -> dict[str, object] | None:
        """Inspect a project-owned geometry specification without mutating it."""

        if self.manifest.generated_geometry_spec is None:
            return None
        geometry_directory = self.root / "geometry"
        spec_path = _safe_project_path(
            self.root,
            self.manifest.generated_geometry_spec,
            label="generated geometry specification",
        )
        artifact_path = geometry_directory / "fluid.stl"
        generation_path = geometry_directory / "generation.json"
        inspection_path = geometry_directory / "inspection.json"
        project_argument = self._cli_project_argument()
        spec_sha256 = file_sha256(spec_path) if spec_path.is_file() else None
        spec_error: str | None = None
        expected: dict[str, object] | None = None
        if not spec_path.is_file():
            spec_error = f"Generated geometry specification is missing: {spec_path}."
        else:
            try:
                raw_spec = strict_json_object(
                    spec_path.read_text(encoding="utf-8"),
                    label=f"generated geometry specification {spec_path}",
                )
                _normalized_spec, expected = _normalize_generated_elbow_spec(
                    raw_spec,
                    target=artifact_path,
                )
            except (OSError, ProjectError) as error:
                spec_error = str(error)

        expected_sha256 = (
            None
            if expected is None
            else str(expected["artifact"]["sha256"])
        )
        artifact_exists = artifact_path.is_file()
        current_sha256 = (
            "sha256:" + file_sha256(artifact_path) if artifact_exists else None
        )
        size_bytes = artifact_path.stat().st_size if artifact_exists else None

        generation_valid = False
        generation_matches = False
        try:
            generation = strict_json_object(
                generation_path.read_text(encoding="utf-8"),
                label=f"generated geometry record {generation_path}",
            )
        except (OSError, ProjectError):
            generation = None
        if generation is not None:
            generation_valid = generation.get("schema") == "agentcfd.generated-geometry/0.1"
            generation_artifact = generation.get("artifact")
            expected_portable = (
                None
                if expected is None
                else _portable_generated_geometry_report(
                    expected,
                    next_command="agentcfd status .",
                )
            )
            generation_matches = bool(
                generation_valid
                and expected is not None
                and expected_portable is not None
                and generation.get("geometry") == expected.get("geometry")
                and generation.get("recommendations")
                == expected_portable.get("recommendations")
                and isinstance(generation_artifact, Mapping)
                and generation_artifact.get("path") == "geometry/fluid.stl"
                and generation_artifact.get("sha256") == expected_sha256
                and generation_artifact.get("size_bytes")
                == expected_portable["artifact"]["size_bytes"]
                and generation_artifact.get("written") is True
                and generation_artifact.get("already_exists") is False
            )

        inspection_valid = False
        inspection_matches = False
        try:
            inspection = strict_json_object(
                inspection_path.read_text(encoding="utf-8"),
                label=f"generated geometry inspection {inspection_path}",
            )
        except (OSError, ProjectError):
            inspection = None
        if inspection is not None:
            inspection_valid = inspection.get("schema") == "agentcfd.geometry-inspection/0.1"
            inspection_source = inspection.get("source")
            inspection_surface = inspection.get("surface")
            inspection_roles = inspection.get("boundary_roles")
            inspection_readiness = inspection.get("readiness")
            expected_geometry = None if expected is None else expected.get("geometry")
            expected_recommendations = (
                None if expected is None else expected.get("recommendations")
            )
            inspection_matches = bool(
                inspection_valid
                and isinstance(inspection_source, Mapping)
                and inspection_source.get("path") == "geometry/fluid.stl"
                and inspection_source.get("sha256") == current_sha256
                and isinstance(inspection_surface, Mapping)
                and isinstance(expected_geometry, Mapping)
                and inspection_surface.get("bounds_m") == expected_geometry.get("bounds_m")
                and isinstance(inspection_roles, Mapping)
                and isinstance(expected_recommendations, Mapping)
                and inspection_roles.get("confirmed")
                == expected_recommendations.get("boundary_roles")
                and isinstance(inspection_readiness, Mapping)
                and inspection_readiness.get("ready_for_import_setup") is True
            )

        synchronized = bool(
            spec_error is None
            and artifact_exists
            and current_sha256 == expected_sha256
            and generation_matches
            and inspection_matches
        )
        if spec_error is not None:
            next_action = {
                "command": f"agentcfd check {project_argument}",
                "reason": f"Repair geometry/spec.json: {spec_error}",
            }
        elif synchronized:
            next_action = {
                "command": f"agentcfd status {project_argument}",
                "reason": "Generated geometry already matches its editable specification.",
            }
        else:
            next_action = {
                "command": f"agentcfd geometry-sync {project_argument} --apply",
                "reason": (
                    "Regenerate the derived STL and inspection from geometry/spec.json "
                    "before solving."
                ),
            }
        return {
            "schema": "agentcfd.generated-geometry-sync/0.1",
            "root": str(self.root),
            "managed": True,
            "synchronized": synchronized,
            "applied": False,
            "spec": {
                "path": "geometry/spec.json",
                "valid": spec_error is None,
                "sha256": spec_sha256,
                "error": spec_error,
            },
            "artifact": {
                "path": "geometry/fluid.stl",
                "exists": artifact_exists,
                "current_sha256": current_sha256,
                "expected_sha256": expected_sha256,
                "size_bytes": size_bytes,
            },
            "generation_record": {
                "path": "geometry/generation.json",
                "valid": generation_valid,
                "matches_expected": generation_matches,
            },
            "inspection_record": {
                "path": "geometry/inspection.json",
                "valid": inspection_valid,
                "matches_artifact": inspection_matches,
            },
            "next_action": next_action,
        }

    def sync_generated_geometry(self, *, apply: bool = False) -> dict[str, object]:
        """Preview or atomically refresh a project-owned generated fluid volume."""

        if not isinstance(apply, bool):
            raise ProjectError("Generated geometry apply must be boolean.")
        before = self._generated_geometry_state()
        if before is None:
            raise ProjectError(
                "This project has no managed geometry/spec.json. Use geometry-create "
                "for standalone geometry or initialize template='industrial-elbow'."
            )
        if before["spec"]["valid"] is not True:
            raise ProjectError(str(before["spec"]["error"]))
        if before["synchronized"] is True or not apply:
            return before

        for record in self._run_records():
            if record.get("status") in {"preparing", "running", "exporting"} and (
                _process_is_alive(record.get("pid"))
            ):
                raise ProjectError(
                    "Generated geometry cannot change while a project run is active."
                )

        geometry_directory = self.root / "geometry"
        spec_path = geometry_directory / "spec.json"
        raw_spec = strict_json_object(
            spec_path.read_text(encoding="utf-8"),
            label=f"generated geometry specification {spec_path}",
        )
        normalized_spec, _expected = _normalize_generated_elbow_spec(
            raw_spec,
            target=geometry_directory / "fluid.stl",
        )
        parameters = normalized_spec["parameters"]
        assert isinstance(parameters, dict)
        staging = geometry_directory / f".fluid.{os.getpid()}.stl"
        if staging.exists():
            raise ProjectError(f"Generated geometry staging path already exists: {staging}")
        try:
            _, generated = geometry_generation.write_circular_elbow_stl(
                staging,
                **parameters,
            )
            recommendations = generated["recommendations"]
            assert isinstance(recommendations, dict)
            roles = recommendations["boundary_roles"]
            assert isinstance(roles, dict)
            inspection = geometry_io.inspect_geometry(
                staging,
                unit="m",
                boundary_roles=roles,
                internal_flow=True,
            )
            if inspection["readiness"]["ready_for_import_setup"] is not True:
                raise ProjectError(
                    "Regenerated geometry failed the independent import inspection."
                )
            artifact_path = geometry_directory / "fluid.stl"
            portable_inspection = json.loads(json.dumps(inspection))
            portable_inspection["source"]["path"] = "geometry/fluid.stl"
            portable_generation = _portable_generated_geometry_report(
                generated,
                next_command="agentcfd status .",
            )
            staging.replace(artifact_path)
            _write_json_atomic(
                geometry_directory / "inspection.json",
                portable_inspection,
            )
            _write_json_atomic(
                geometry_directory / "generation.json",
                portable_generation,
            )
        finally:
            if staging.exists():
                staging.unlink()

        after = self._generated_geometry_state()
        if after is None or after["synchronized"] is not True:
            raise ProjectError(
                "Generated geometry refresh completed but synchronization evidence is inconsistent."
            )
        after["applied"] = True
        return after

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
                else "openfoam.steady-laminar-circular-pipe"
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
        generated_geometry = self._generated_geometry_state()
        if (
            generated_geometry is not None
            and generated_geometry["synchronized"] is not True
        ):
            input_assets_ready = False
            spec = generated_geometry["spec"]
            assert isinstance(spec, Mapping)
            issue_code = (
                "GENERATED_GEOMETRY_SPEC_INVALID"
                if spec["valid"] is not True
                else "GENERATED_GEOMETRY_OUT_OF_DATE"
            )
            issues.append(
                ProjectIssue(
                    issue_code,
                    "error",
                    str(generated_geometry["next_action"]["reason"]),
                    "geometry/spec.json",
                    str(generated_geometry["next_action"]["reason"]),
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
            "generated_geometry": generated_geometry,
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
                "template": self.manifest.template,
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

    def _select_run_record(self, run_id: str | None = None) -> dict[str, object] | None:
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
            sorted(campaign_root.glob("*/run.json")) if campaign_root.is_dir() else []
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
            "total_display": None if total_bytes is None else _human_bytes(total_bytes),
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
        summary_path = run_directory / "summary.json"
        record: dict[str, object] | None = None
        summary_bytes_read = 0
        if summary_path.is_file():
            summary_bytes_read = summary_path.stat().st_size
            candidate = strict_json_object(
                summary_path.read_text(encoding="utf-8"),
                label=f"AgentCFD result summary {summary_path}",
            )
            source = candidate.get("source_result")
            recorded_result_sha256 = selected_run.get("result_sha256")
            if (
                candidate.get("schema") == "agentcfd.result-summary/0.3"
                and _project_record_path_matches(
                    candidate.get("root"), self.root, root=self.root
                )
                and candidate.get("run_id") == selected_run.get("run_id")
                and _project_record_path_matches(
                    candidate.get("result"), result_path, root=self.root
                )
                and isinstance(source, dict)
                and _project_record_path_matches(
                    source.get("path"), result_path, root=self.root
                )
                and source.get("bytes") == result_path.stat().st_size
                and isinstance(source.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"]))
                and (
                    recorded_result_sha256 is None
                    or source.get("sha256") == recorded_result_sha256
                )
                and isinstance(candidate.get("quantities"), dict)
                and isinstance(candidate.get("available"), dict)
                and isinstance(candidate.get("observation_cost"), dict)
            ):
                record = candidate
        if record is None:
            result_record = read_result_record(result_path, verify_artifacts=False)
            record = self._result_summary_payload(
                selected_run,
                result_path=result_path,
                record=result_record,
                result_json_bytes_read=result_path.stat().st_size,
            )
            summary_bytes_read = 0
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
        report = dict(record)
        # Materialized summaries may use project-relative paths so a restored
        # handoff remains movable.  Public APIs always expose paths resolved in
        # the currently opened project.
        report["root"] = str(self.root)
        report["summary"] = str(summary_path)
        report["result"] = str(result_path)
        source_result = report.get("source_result")
        if isinstance(source_result, Mapping):
            report["source_result"] = {
                **source_result,
                "path": str(result_path),
            }
        report["quantities"] = selected_quantities
        observation_cost = dict(report["observation_cost"])
        observation_cost["summary_json_bytes_read"] = summary_bytes_read
        report["observation_cost"] = observation_cost
        if run_id is not None:
            report["next_action"] = {
                "command": f"agentcfd verify result {shlex.quote(str(result_path))}",
                "reason": "Verify this immutable run before an external handoff.",
            }
        return report

    def _result_summary_payload(
        self,
        selected_run: Mapping[str, object],
        *,
        result_path: Path,
        record: Mapping[str, object],
        result_json_bytes_read: int,
    ) -> dict[str, object]:
        """Build the bounded, field-free result decision artifact."""

        available_quantities = record.get("quantities", {})
        fields = record.get("fields", {})
        histories = record.get("histories", {})
        checks = record.get("checks", [])
        if not isinstance(available_quantities, Mapping):
            raise ProjectError("Result quantities are malformed.")
        if not isinstance(fields, Mapping) or not isinstance(histories, Mapping):
            raise ProjectError("Result field or history metadata is malformed.")
        if not isinstance(checks, Sequence):
            raise ProjectError("Result checks are malformed.")
        requirements = [
            check
            for check in checks
            if isinstance(check, Mapping) and check.get("kind") == "requirement"
        ]
        failed_checks = [
            check
            for check in checks
            if isinstance(check, Mapping) and check.get("passed") is False
        ]
        failed_scientific_checks = [
            check for check in failed_checks if check.get("kind") != "requirement"
        ]
        verification_command = f"agentcfd verify result {shlex.quote(str(result_path))}"
        accepted = record.get("accepted") is True
        return {
            "schema": "agentcfd.result-summary/0.3",
            "root": str(self.root),
            "run_id": selected_run.get("run_id"),
            "summary": str(result_path.with_name("summary.json")),
            "result": str(result_path),
            "source_result": {
                "path": str(result_path),
                "bytes": result_path.stat().st_size,
                "sha256": file_sha256(result_path),
            },
            "status": record.get("status"),
            "converged": record.get("converged"),
            "accepted": accepted,
            "trust_level": record.get("trust_level"),
            "provider": record.get("provider"),
            "parameters": selected_run.get("parameters", {}),
            "quantities": dict(available_quantities),
            "histories": dict(histories),
            "fields": dict(fields),
            "available": {
                "quantities": sorted(available_quantities),
                "histories": sorted(histories),
                "fields": sorted(fields),
            },
            "check_count": len(checks),
            "requirements": requirements,
            "failed_checks": failed_checks,
            "scientific_inputs": record.get("scientific_inputs", {}),
            "provenance": record.get("provenance", {}),
            "artifact_integrity": {
                "verified": False,
                "reason": "External artifacts were not opened by this lightweight view.",
                "command": verification_command,
            },
            "observation_cost": {
                "summary_json_bytes_read": 0,
                "result_json_bytes_read": result_json_bytes_read,
                "field_payloads_opened": 0,
                "artifacts_hashed": 0,
            },
            "next_action": {
                "command": f"agentcfd view {self._cli_project_argument()}",
                "reason": (
                    "Open the accepted result for spatial review."
                    if accepted
                    else "Review the unmet design requirements before selecting or changing the design."
                    if requirements and not failed_scientific_checks
                    else "Review the failed checks before using this result."
                ),
            },
        }

    def scientific_sample(
        self,
        *,
        outputs: Sequence[str],
        inputs: Sequence[str] = (),
        run_id: str | None = None,
        case_id: str | None = None,
    ) -> dict[str, object]:
        """Create one verified scalar sample from a published project run."""

        output_names = tuple(dict.fromkeys(str(name).strip() for name in outputs))
        if not output_names or any(not name for name in output_names):
            raise ProjectError("Scientific-sample outputs require explicit names.")
        input_names = tuple(dict.fromkeys(str(name).strip() for name in inputs))
        if any(not name for name in input_names):
            raise ProjectError("Scientific-sample input names must not be empty.")
        summary = self.result_summary(run_id=run_id, quantities=output_names)
        if summary["accepted"] is not True:
            raise ProjectError(
                "Scientific samples require an accepted result; review failed checks first."
            )
        verification = self.verify(run_id=run_id)
        if verification["verified"] is not True:
            failed = ", ".join(
                str(check["code"])
                for check in verification["checks"]
                if check["passed"] is False
            )
            raise ProjectError(
                "Scientific-sample source verification failed: "
                + (failed or "unknown integrity failure")
                + "."
            )
        selected_run = self._select_run_record(run_id)
        assert selected_run is not None
        selected_parameters = selected_run.get("parameters", {})
        if not isinstance(selected_parameters, Mapping):
            raise ProjectError("Selected run parameters are malformed.")
        parameter_records = self.parameter_contract(selected_parameters)
        numeric_parameters = {
            str(record["name"]): float(record["current"])
            for record in parameter_records
            if isinstance(record.get("current"), (int, float))
            and not isinstance(record.get("current"), bool)
            and math.isfinite(float(record["current"]))
        }
        if not input_names:
            input_names = tuple(sorted(numeric_parameters))
        unknown_inputs = sorted(set(input_names) - set(numeric_parameters))
        if unknown_inputs:
            available = ", ".join(sorted(numeric_parameters)) or "none"
            raise ProjectError(
                "Unknown or non-numeric sample inputs: "
                + ", ".join(unknown_inputs)
                + f". Available numeric project parameters: {available}."
            )
        if not input_names:
            raise ProjectError(
                "Scientific samples require at least one numeric case.py parameter."
            )
        if case_id is not None and (
            not isinstance(case_id, str) or not case_id.strip()
        ):
            raise ProjectError("Scientific-sample case id must be non-empty.")
        quantities = summary["quantities"]
        provenance = dict(summary["provenance"])
        provenance["sample_export"] = {
            "project_verified": True,
            "run_id": summary["run_id"],
            "source_result_sha256": summary["source_result"]["sha256"],
            "input_schema": [
                {
                    "name": record["name"],
                    "value": numeric_parameters[str(record["name"])],
                    "metadata": record["metadata"],
                }
                for record in parameter_records
                if record["name"] in input_names
            ],
        }
        return {
            "schema": "agentcae.scientific-sample",
            "schema_version": "0.1.0",
            "case_id": (
                case_id.strip()
                if case_id is not None
                else f"agentcfd-{summary['run_id']}"
            ),
            "source": {"product": "agentcfd", "provider": summary["provider"]},
            "inputs": {name: numeric_parameters[name] for name in input_names},
            "outputs": {name: float(quantities[name]["value"]) for name in output_names},
            "quantity_schema": [
                {"name": name, "shape": [], **dict(quantities[name])}
                for name in output_names
            ],
            "trust_level": summary["trust_level"],
            "accepted": True,
            "scientific_inputs": summary["scientific_inputs"],
            "provenance": provenance,
            "artifacts": {
                "summary": summary["summary"],
                "result": summary["result"],
            },
        }

    def export_scientific_sample(
        self,
        path: str | Path,
        *,
        outputs: Sequence[str],
        inputs: Sequence[str] = (),
        run_id: str | None = None,
        case_id: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        """Verify and atomically publish one solver-neutral scalar sample."""

        target = Path(path)
        if target.suffix.lower() != ".json":
            raise ProjectError("Scientific-sample output must use the .json suffix.")
        if target.exists():
            raise ProjectError(f"Scientific-sample output already exists: {target}")
        sample = self.scientific_sample(
            outputs=outputs,
            inputs=inputs,
            run_id=run_id,
            case_id=case_id,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(target, sample)
        return target, sample

    def export_campaign_dataset(
        self,
        directory: str | Path,
        *,
        outputs: Sequence[str],
        inputs: Sequence[str] = (),
    ) -> tuple[Path, dict[str, object]]:
        """Publish verified accepted campaign points as one atomic JSONL dataset."""

        target = Path(directory)
        if target.exists():
            raise ProjectError(f"Campaign dataset output already exists: {target}")
        campaign = self.campaign_index()
        accepted_runs = [
            run for run in campaign["runs"] if run.get("accepted") is True
        ]
        excluded = [
            {
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "accepted": run.get("accepted"),
                "reason": "The campaign point is not accepted.",
            }
            for run in campaign["runs"]
            if run.get("accepted") is not True
        ]
        if not accepted_runs:
            raise ProjectError(
                "Campaign dataset export requires at least one accepted campaign run."
            )
        samples: list[dict[str, object]] = []
        sample_rows: list[dict[str, object]] = []
        input_contract: list[dict[str, object]] | None = None
        output_contract: list[dict[str, object]] | None = None
        for line_number, run in enumerate(accepted_runs, start=1):
            run_id = str(run["run_id"])
            sample = self.scientific_sample(
                outputs=outputs,
                inputs=inputs,
                run_id=run_id,
            )
            current_inputs = [
                {
                    "name": record["name"],
                    "metadata": record["metadata"],
                }
                for record in sample["provenance"]["sample_export"]["input_schema"]
            ]
            current_outputs = [
                {
                    name: value
                    for name, value in record.items()
                    if name != "value"
                }
                for record in sample["quantity_schema"]
            ]
            if input_contract is None:
                input_contract = current_inputs
                output_contract = current_outputs
            elif (
                current_inputs != input_contract
                or current_outputs != output_contract
            ):
                raise ProjectError(
                    f"Campaign run {run_id!r} does not share the dataset input/output schema."
                )
            samples.append(sample)
            sample_rows.append(
                {
                    "line": line_number,
                    "run_id": run_id,
                    "case_id": sample["case_id"],
                    "source_result_sha256": sample["provenance"]["sample_export"][
                        "source_result_sha256"
                    ],
                }
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent)
        )
        try:
            samples_path = staging / "samples.jsonl"
            with samples_path.open("w", encoding="utf-8", newline="\n") as stream:
                for sample in samples:
                    stream.write(
                        json.dumps(
                            sample,
                            sort_keys=True,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            manifest = {
                "schema": "agentcfd.scientific-dataset/0.1",
                "root": str(self.root),
                "created_at": datetime.now(UTC).isoformat(),
                "sample_schema": "agentcae.scientific-sample/0.1.0",
                "sample_count": len(samples),
                "excluded_count": len(excluded),
                "inputs": input_contract or [],
                "outputs": output_contract or [],
                "samples": {
                    "path": "samples.jsonl",
                    "media_type": "application/x-ndjson",
                    "bytes": samples_path.stat().st_size,
                    "sha256": file_sha256(samples_path),
                },
                "runs": sample_rows,
                "excluded": excluded,
                "verification": {
                    "mode": "full-project-per-sample",
                    "all_samples_verified": True,
                    "field_payload_policy": (
                        "Full-field runs verify their field bundles; summary-only runs "
                        "carry no permanent spatial payload."
                    ),
                },
            }
            _write_json_atomic(staging / "manifest.json", manifest)
            staging.replace(target)
        except Exception:
            if staging.is_dir():
                shutil.rmtree(staging)
            raise
        return target, manifest

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

    def campaign_operating_map(
        self,
        *,
        x_parameter: str,
        y_quantity: str,
        accepted_only: bool = True,
    ) -> dict[str, object]:
        """Build a field-free engineering curve from compact campaign markers.

        The x axis is deliberately restricted to a declared numeric ``case.py``
        parameter and the y axis to one canonical result quantity.  This keeps
        design intent, units, and scientific acceptance visible without opening
        result manifests or volumetric field payloads.
        """

        if not isinstance(x_parameter, str) or not x_parameter.strip():
            raise ProjectError("Operating-map x parameter must be a non-empty string.")
        if not isinstance(y_quantity, str) or not y_quantity.strip():
            raise ProjectError("Operating-map y quantity must be a non-empty string.")
        if not isinstance(accepted_only, bool):
            raise ProjectError("Operating-map accepted_only must be a boolean.")
        x_parameter = x_parameter.strip()
        y_quantity = y_quantity.strip()

        campaign = self.campaign_index()
        points: list[dict[str, object]] = []
        exclusions: list[dict[str, object]] = []
        known_quantities: set[str] = set()
        known_parameters: set[str] = set()
        selected_units: set[str | None] = set()
        x_axis_metadata: set[tuple[str, str | None]] = set()
        plan_files_opened = 0
        for row in campaign["runs"]:
            quantities = row["quantities"]
            parameters = row["parameters"]
            known_parameters.update(
                name for name in parameters if isinstance(name, str)
            )
            known_quantities.update(
                name for name in quantities if isinstance(name, str)
            )
            reason = None
            if accepted_only and row["accepted"] is not True:
                reason = "result-not-accepted"
            elif x_parameter not in parameters:
                reason = "x-parameter-not-explicit"
            elif y_quantity not in quantities:
                reason = "y-quantity-unavailable"
            else:
                x_value = parameters[x_parameter]
                y_record = quantities[y_quantity]
                y_value = y_record.get("value") if isinstance(y_record, dict) else None
                if (
                    isinstance(x_value, bool)
                    or not isinstance(x_value, (int, float))
                    or not math.isfinite(float(x_value))
                ):
                    reason = "x-parameter-not-finite-numeric"
                elif (
                    isinstance(y_value, bool)
                    or not isinstance(y_value, (int, float))
                    or not math.isfinite(float(y_value))
                ):
                    reason = "y-quantity-not-finite-numeric"
                else:
                    unit = y_record.get("unit")
                    if unit is not None and not isinstance(unit, str):
                        reason = "y-unit-invalid"
                    else:
                        plan_path = Path(str(row["directory"])) / "plan.json"
                        try:
                            plan = strict_json_object(
                                plan_path.read_text(encoding="utf-8"),
                                label=f"campaign plan {plan_path}",
                            )
                        except (OSError, ValueError) as error:
                            raise ProjectError(
                                f"Cannot read immutable parameter metadata for run "
                                f"{row['run_id']}: {error}"
                            ) from error
                        plan_files_opened += 1
                        project_record = plan.get("project")
                        factory_parameters = (
                            project_record.get("factory_parameters")
                            if isinstance(project_record, dict)
                            else None
                        )
                        if not isinstance(factory_parameters, list):
                            raise ProjectError(
                                f"Campaign run {row['run_id']} has no immutable "
                                "factory-parameter contract."
                            )
                        parameter = next(
                            (
                                record
                                for record in factory_parameters
                                if isinstance(record, dict)
                                and record.get("name") == x_parameter
                            ),
                            None,
                        )
                        if parameter is None:
                            raise ProjectError(
                                f"Campaign run {row['run_id']} does not declare "
                                f"parameter {x_parameter!r} in its saved plan."
                            )
                        planned_value = parameter.get("current")
                        if (
                            isinstance(planned_value, bool)
                            or not isinstance(planned_value, (int, float))
                            or not math.isfinite(float(planned_value))
                            or float(planned_value) != float(x_value)
                        ):
                            raise ProjectError(
                                f"Campaign run {row['run_id']} parameter {x_parameter!r} "
                                "does not match its immutable plan."
                            )
                        metadata = parameter.get("metadata")
                        if isinstance(metadata, dict) and metadata.get("kind") not in {
                            "number",
                            "integer",
                        }:
                            raise ProjectError(
                                f"Campaign parameter {x_parameter!r} must be numeric."
                            )
                        x_label = (
                            str(metadata.get("label"))
                            if isinstance(metadata, dict) and metadata.get("label")
                            else x_parameter
                        )
                        x_unit = (
                            metadata.get("unit") if isinstance(metadata, dict) else None
                        )
                        if x_unit is not None and not isinstance(x_unit, str):
                            raise ProjectError(
                                f"Campaign parameter {x_parameter!r} has an invalid unit."
                            )
                        x_axis_metadata.add((x_label, x_unit))
                        selected_units.add(unit)
                        points.append(
                            {
                                "run_id": row["run_id"],
                                "design_point_name": row["design_point_name"],
                                "x": float(x_value),
                                "y": float(y_value),
                                "accepted": row["accepted"] is True,
                                "trust_level": row["trust_level"],
                            }
                        )
            if reason is not None:
                exclusions.append(
                    {
                        "run_id": row["run_id"],
                        "design_point_name": row["design_point_name"],
                        "reason": reason,
                    }
                )

        if y_quantity not in known_quantities:
            available = ", ".join(sorted(known_quantities)) or "none"
            raise ProjectError(
                f"Unknown campaign quantity {y_quantity!r}. Available: {available}."
            )
        if x_parameter not in known_parameters:
            available = ", ".join(sorted(known_parameters)) or "none"
            raise ProjectError(
                f"Unknown project parameter {x_parameter!r}. Available in campaign: "
                f"{available}."
            )
        if not points:
            scope = "accepted " if accepted_only else ""
            raise ProjectError(
                f"No {scope}campaign point has explicit numeric parameter "
                f"{x_parameter!r} and quantity {y_quantity!r}."
            )
        if len(selected_units) != 1:
            raise ProjectError(
                f"Campaign quantity {y_quantity!r} does not use one consistent unit."
            )
        if len(x_axis_metadata) != 1:
            raise ProjectError(
                f"Campaign parameter {x_parameter!r} does not use one consistent label and unit."
            )
        points.sort(key=lambda point: (float(point["x"]), str(point["run_id"])))
        accepted_x = [float(point["x"]) for point in points if point["accepted"]]
        connected = len(accepted_x) >= 2 and len(set(accepted_x)) == len(accepted_x)
        warnings: list[str] = []
        if len(accepted_x) < 2:
            warnings.append(
                "Fewer than two accepted points are available; no decision curve is drawn."
            )
        elif len(set(accepted_x)) != len(accepted_x):
            warnings.append(
                "Accepted points repeat x values; markers are shown without a connecting curve."
            )
        y_unit = next(iter(selected_units))
        x_label, x_unit = next(iter(x_axis_metadata))
        observation_cost = dict(campaign["observation_cost"])
        observation_cost["plan_files_opened"] = (
            int(observation_cost["plan_files_opened"]) + plan_files_opened
        )
        return {
            "schema": "agentcfd.campaign-operating-map/0.1",
            "root": str(self.root),
            "accepted_only": accepted_only,
            "x_axis": {
                "source": "project-parameter",
                "name": x_parameter,
                "label": x_label,
                "unit": x_unit,
            },
            "y_axis": {
                "source": "result-quantity",
                "name": y_quantity,
                "label": y_quantity.rsplit(".", 1)[-1].replace("_", " ").capitalize(),
                "unit": y_unit,
            },
            "point_count": len(points),
            "accepted_count": len(accepted_x),
            "connected_accepted_curve": connected,
            "points": points,
            "exclusions": exclusions,
            "warnings": warnings,
            "artifact_integrity": {
                "verified": False,
                "reason": (
                    "This fast map reads compact campaign markers and immutable plan "
                    "summaries only; verify the selected run before a high-consequence "
                    "decision."
                ),
            },
            "artifact": None,
            "observation_cost": observation_cost,
        }

    def export_campaign_operating_map(
        self,
        path: str | Path,
        *,
        x_parameter: str,
        y_quantity: str,
        accepted_only: bool = True,
        title: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        """Write a deterministic, dependency-free SVG operating map."""

        target = Path(path)
        if target.suffix.lower() != ".svg":
            raise ProjectError(
                "Campaign operating-map output must use the .svg suffix."
            )
        report = self.campaign_operating_map(
            x_parameter=x_parameter,
            y_quantity=y_quantity,
            accepted_only=accepted_only,
        )
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ProjectError(
                "Operating-map title must be a non-empty string or None."
            )
        selected_title = (
            title.strip() if title is not None else "AgentCFD operating map"
        )
        svg = campaign_plotting.render_operating_map_svg(
            report,
            title=selected_title,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(svg, encoding="utf-8")
        temporary.replace(target)
        report["artifact"] = {
            "path": str(target),
            "media_type": "image/svg+xml",
            "bytes": target.stat().st_size,
            "sha256": file_sha256(target),
        }
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

    def _campaign_execution_policy(
        self,
        prepared: Sequence[
            tuple[str, Mapping[str, object], Mapping[str, object], str]
        ],
        reusable: Mapping[str, Mapping[str, object]],
        *,
        provider: str,
        maximum_parallel_runs: int,
    ) -> dict[str, object]:
        """Resolve bounded local concurrency without starting solver processes."""

        if (
            isinstance(maximum_parallel_runs, bool)
            or not isinstance(maximum_parallel_runs, int)
            or maximum_parallel_runs < 1
            or maximum_parallel_runs > _MAX_CAMPAIGN_PARALLELISM
        ):
            raise ProjectError(
                "Maximum parallel runs must be an integer from 1 through "
                f"{_MAX_CAMPAIGN_PARALLELISM}."
            )
        unique_plans: dict[str, Mapping[str, object]] = {}
        for _name, _parameters, plan, identity in prepared:
            if identity not in reusable:
                unique_plans.setdefault(identity, plan)
        logical_cpus = max(1, os.cpu_count() or 1)
        planned_new_runs = len(unique_plans)
        effective_parallel_runs = min(
            maximum_parallel_runs,
            logical_cpus,
            planned_new_runs,
        )
        estimates: list[int] = []
        final_field_estimates: list[int] = []
        estimates_complete = True
        for plan in unique_plans.values():
            decisions = plan.get("decisions", {})
            output_plan = (
                decisions.get("output_plan", {})
                if isinstance(decisions, Mapping)
                else {}
            )
            estimated = (
                output_plan.get("estimated_temporary_peak_bytes")
                if isinstance(output_plan, Mapping)
                else None
            )
            estimated_final = (
                output_plan.get("estimated_portable_bytes")
                if isinstance(output_plan, Mapping)
                else None
            )
            if provider != "openfoam":
                estimates.append(0)
                final_field_estimates.append(0)
            elif (
                isinstance(estimated, int)
                and not isinstance(estimated, bool)
                and isinstance(estimated_final, int)
                and not isinstance(estimated_final, bool)
            ):
                estimates.append(estimated)
                final_field_estimates.append(estimated_final)
            else:
                estimates_complete = False
        concurrent_peak = (
            sum(sorted(estimates, reverse=True)[:effective_parallel_runs])
            if estimates_complete
            else None
        )
        estimated_final_field_growth = (
            sum(final_field_estimates) if estimates_complete else None
        )
        storage_admission_bytes = (
            concurrent_peak + estimated_final_field_growth
            if concurrent_peak is not None
            and estimated_final_field_growth is not None
            else None
        )
        available_bytes = shutil.disk_usage(self.root).free
        storage_within_budget = (
            concurrent_peak <= available_bytes
            if concurrent_peak is not None
            else None
        )
        storage_admission_within_budget = (
            storage_admission_bytes <= available_bytes
            if storage_admission_bytes is not None
            else None
        )
        parallel_ready = (
            effective_parallel_runs <= 1
            if not estimates_complete
            else storage_admission_within_budget is True
        )
        return {
            "requested_parallel_runs": maximum_parallel_runs,
            "effective_parallel_runs": effective_parallel_runs,
            "maximum_supported_parallel_runs": _MAX_CAMPAIGN_PARALLELISM,
            "logical_cpu_count": logical_cpus,
            "cpu_limited": effective_parallel_runs < min(
                maximum_parallel_runs,
                planned_new_runs,
            ),
            "automatic_retries": 0,
            "maximum_attempts_per_identity": 1,
            "memory_estimate_available": False,
            "memory_admission": "explicit-parallel-bound-not-estimated",
            "temporary_storage_estimates_complete": estimates_complete,
            "concurrent_temporary_peak_bytes": concurrent_peak,
            "estimated_final_field_growth_bytes": estimated_final_field_growth,
            "storage_admission_bytes": storage_admission_bytes,
            "available_temporary_bytes": available_bytes,
            "temporary_storage_within_budget": storage_within_budget,
            "storage_admission_within_budget": storage_admission_within_budget,
            "parallel_ready": parallel_ready,
        }

    def plan_campaign(
        self,
        points: Mapping[str, Mapping[str, object]],
        *,
        provider: str | None = None,
        container_image: str | None = None,
        summary_only: bool = False,
        maximum_parallel_runs: int = 1,
    ) -> dict[str, object]:
        """Preview campaign readiness and reuse without starting a solver."""

        if not isinstance(points, Mapping) or not points:
            raise ProjectError("A campaign sweep requires at least one named point.")
        selected_provider = provider or self.manifest.default_provider
        reusable = self._reusable_campaign_records()
        rows = []
        prepared_for_policy: list[
            tuple[str, Mapping[str, object], Mapping[str, object], str]
        ] = []
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
                step = self.load_step(parameters)
                plan = self.plan(
                    provider=selected_provider,
                    container_image=container_image,
                    parameters=parameters,
                    portable_fields=False if summary_only else None,
                    _step=step,
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
                prepared_for_policy.append((name, parameters, plan, identity))
                planned_identities.add(identity)
        all_ready = all(row["ready"] is True for row in rows)
        reusable_count = sum(row["reusable"] is True for row in rows)
        execution_policy = self._campaign_execution_policy(
            prepared_for_policy,
            reusable,
            provider=selected_provider,
            maximum_parallel_runs=maximum_parallel_runs,
        )
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
            "execution_policy": execution_policy,
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
        maximum_parallel_runs: int = 1,
    ) -> dict[str, object]:
        """Preflight and execute named points with bounded, opt-in concurrency."""

        if not isinstance(points, Mapping) or not points:
            raise ProjectError("A campaign sweep requires at least one named point.")
        if maximum_solver_runs is not None and (
            isinstance(maximum_solver_runs, bool)
            or not isinstance(maximum_solver_runs, int)
            or maximum_solver_runs < 0
        ):
            raise ProjectError("Maximum solver runs must be a non-negative integer.")
        selected_provider = provider or self.manifest.default_provider
        prepared: list[
            tuple[str, dict[str, object], dict[str, object], str, Step]
        ] = []
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
            step = self.load_step(parameters)
            plan = self.plan(
                provider=selected_provider,
                container_image=container_image,
                parameters=parameters,
                portable_fields=False if summary_only else None,
                _step=step,
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
            prepared.append((name, parameters, plan, identity, step))

        reusable = self._reusable_campaign_records()
        planned_new_runs = len(
            {
                identity
                for _name, _parameters, _plan, identity, _step in prepared
                if identity not in reusable
            }
        )
        if maximum_solver_runs is not None and planned_new_runs > maximum_solver_runs:
            raise ProjectError(
                f"Campaign would start {planned_new_runs} solver processes, exceeding "
                f"the explicit --max-runs {maximum_solver_runs} budget. No design "
                "point was executed; inspect `agentcfd sweep . REQUEST --plan-only`."
            )
        execution_policy = self._campaign_execution_policy(
            [
                (name, parameters, plan, identity)
                for name, parameters, plan, identity, _step in prepared
            ],
            reusable,
            provider=selected_provider,
            maximum_parallel_runs=maximum_parallel_runs,
        )
        effective_parallel_runs = int(
            execution_policy["effective_parallel_runs"]
        )
        if fail_fast and effective_parallel_runs > 1:
            raise ProjectError(
                "--fail-fast cannot be combined with more than one effective parallel "
                "run because already-started solver processes cannot be truthfully "
                "cancelled. Use --max-parallel 1 or omit --fail-fast."
            )
        if execution_policy["parallel_ready"] is not True:
            if execution_policy["temporary_storage_estimates_complete"] is not True:
                reason = "one or more temporary-storage estimates are unavailable"
            else:
                reason = (
                    "the campaign storage admission (concurrent temporary peak plus "
                    "final field growth) exceeds currently available disk space"
                )
            raise ProjectError(
                f"Parallel campaign execution is not ready because {reason}. "
                "Use --max-parallel 1 or reduce output and mesh demand. No design "
                "point was executed."
            )
        progress_path = self.root / "campaigns" / "last-sweep.json"
        rows: list[dict[str, object]] = []
        request_results: dict[str, dict[str, object]] = {}
        initial_reusable = dict(reusable)
        first_request_index: dict[str, int] = {}
        for index, (_name, _parameters, _plan, identity, _step) in enumerate(prepared):
            first_request_index.setdefault(identity, index)

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
                "execution_policy": execution_policy,
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

        def execute_point(
            name: str,
            parameters: Mapping[str, object],
            plan: Mapping[str, object],
            identity: str,
            step: Step,
        ) -> dict[str, object]:
            try:
                completed = self.run(
                    provider=selected_provider,
                    container_image=container_image,
                    campaign=True,
                    parameters=parameters,
                    design_point_name=name,
                    portable_fields=False if summary_only else None,
                    _step=step,
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
                return {
                    "name": name,
                    "parameters": parameters,
                    "plan_sha256": plan["plan_sha256"],
                    "result_execution_sha256": identity,
                    "execution": "executed",
                    "outcome": "failed",
                    "run_id": failed_run_id,
                    "directory": (
                        None if failed_directory is None else str(failed_directory)
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
            accepted = completed.result.accepted
            outcome = (
                "accepted"
                if accepted
                else "failed"
                if completed.result.status != "completed"
                else "review"
            )
            project_argument = self._cli_project_argument()
            return {
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

        def refresh_rows() -> None:
            refreshed: list[dict[str, object]] = []
            for index, (name, parameters, plan, identity, _step) in enumerate(prepared):
                cached = initial_reusable.get(identity)
                if cached is not None:
                    refreshed.append(
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
                    continue
                source = request_results.get(identity)
                if source is None:
                    continue
                if first_request_index[identity] == index:
                    refreshed.append(source)
                    continue
                duplicate = dict(source)
                duplicate.update(
                    {
                        "name": name,
                        "parameters": parameters,
                        "plan_sha256": plan["plan_sha256"],
                        "result_execution_sha256": identity,
                        "execution": (
                            "reused" if source["accepted"] is True else "deduplicated"
                        ),
                    }
                )
                refreshed.append(duplicate)
            rows[:] = refreshed

        unique_new = [
            item
            for index, item in enumerate(prepared)
            if item[3] not in initial_reusable
            and first_request_index[item[3]] == index
        ]
        refresh_rows()
        write_progress()
        if effective_parallel_runs <= 1:
            for name, parameters, plan, identity, step in unique_new:
                row = execute_point(name, parameters, plan, identity, step)
                request_results[identity] = row
                refresh_rows()
                write_progress()
                if fail_fast and row["outcome"] == "failed":
                    break
        else:
            with ThreadPoolExecutor(
                max_workers=effective_parallel_runs,
                thread_name_prefix="agentcfd-campaign",
            ) as executor:
                futures = {
                    executor.submit(execute_point, *item): item[3]
                    for item in unique_new
                }
                for future in as_completed(futures):
                    identity = futures[future]
                    request_results[identity] = future.result()
                    refresh_rows()
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
            raise ProjectError(
                "Only completed, accepted campaign runs can be compacted."
            )
        if source.get("result_profile", "full-fields") != "full-fields":
            raise ProjectError(f"Run {run_id!r} is already summary-only.")
        if self.manifest.default_provider != "openfoam":
            raise ProjectError(
                "Field compaction currently requires an OpenFOAM project."
            )
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
                "summary.json",
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
        compact_summary = self._result_summary_payload(
            updated_source,
            result_path=result_path,
            record=result,
            result_json_bytes_read=0,
        )
        _write_json_atomic(run_directory / "summary.json", compact_summary)
        updated_source["result_sha256"] = compact_summary["source_result"]["sha256"]
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
        interrupted = status in {
            "preparing",
            "running",
            "exporting",
        } and not _process_is_alive(latest.get("pid"))
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
                # A retained workspace is the authoritative live copy.  Do not
                # let filesystem timestamp resolution decide between it and a
                # published evidence copy (notably on Windows runners).
                key=lambda item: (
                    item[2] == "workspace",
                    modified(item),
                    item[0],
                ),
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
        interrupted = native_status in {
            "preparing",
            "running",
            "exporting",
        } and not _process_is_alive(latest.get("pid"))
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
        identity_match = latest.get("analysis_sha256") == analysis_sha256 and (
            source_resume_execution == expected_resume_execution
            if isinstance(source_resume_execution, str)
            else latest.get("execution_sha256") == expected_execution
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
            workspace_case = self.root / ".agentcfd" / "work" / run_id / "openfoam"
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
        parameters = selected.get("parameters", {}) if selected is not None else {}
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

        if (
            isinstance(lines, bool)
            or not isinstance(lines, int)
            or not 1 <= lines <= 1000
        ):
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
            raise ProjectError(
                "Solver logs disappeared before they could be diagnosed."
            )

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
        with _PERFORMANCE_HISTORY_LOCK:
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
                protected = protected_active or protected_recovery or protected_retained
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
                if (
                    target["protected_active_run"]
                    or target["protected_recovery_checkpoint"]
                    or target["protected_retained_workspace"]
                ):
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
                    changed = latest_result_execution != current_result_execution_sha256
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
            "layouts": [],
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
            if isinstance(recipe_manifest, dict) and isinstance(
                recipe_manifest.get("layouts"), list
            ):
                postprocess["layouts"] = [
                    {
                        **layout,
                        "script": str(run_directory / "postprocess" / layout["script"]),
                    }
                    for layout in recipe_manifest["layouts"]
                    if isinstance(layout, dict)
                    and isinstance(layout.get("script"), str)
                ]
        progress = _run_progress_snapshot(
            self.root,
            latest,
            plan,
            include_storage=include_storage,
        )
        recovery = self._recovery_status(step, plan, latest)
        if state == "blocked":
            generated_outdated = next(
                (
                    issue
                    for issue in error_issues
                    if issue.get("code") == "GENERATED_GEOMETRY_OUT_OF_DATE"
                ),
                None,
            )
            if generated_outdated is not None:
                next_action = {
                    "command": f"agentcfd geometry-sync {project_argument} --apply",
                    "reason": generated_outdated["repair"],
                }
            else:
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
                (
                    "summary",
                    "lightweight-decision-result",
                    run_directory / "summary.json",
                ),
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
                observation_cost = compact_result["observation_cost"]
                result_bytes = int(observation_cost["result_json_bytes_read"]) + int(
                    observation_cost["summary_json_bytes_read"]
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
                "template": self.manifest.template,
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

    def actions(self) -> dict[str, object]:
        """Return a state-aware, side-effect-explicit action catalog for agents."""

        status = self.status()
        state = str(status["state"])
        project_argument = self._cli_project_argument()
        latest = status.get("latest_run")
        run_directory = self._record_directory(
            latest if isinstance(latest, Mapping) else None
        )
        has_result = (
            run_directory is not None and (run_directory / "result.json").is_file()
        )
        has_view = status["postprocess"]["primary"] is not None
        has_logs = bool(
            self._log_candidates(
                latest if isinstance(latest, Mapping) else None,
                run_directory,
            )
        )
        recommended = _structured_project_action(status["next_action"])

        records: list[dict[str, object]] = []

        def add(
            operation: str,
            command: str,
            *,
            available: bool,
            availability_reason: str,
            cost: str,
        ) -> None:
            kind, mutates_project, starts_solver = _PROJECT_ACTION_POLICIES[operation]
            tokens = shlex.split(command)
            records.append(
                {
                    "operation": operation,
                    "kind": kind,
                    "argv": tokens,
                    "command": command,
                    "available": available,
                    "recommended": operation == recommended["operation"],
                    "mutates_project": mutates_project,
                    "starts_solver": starts_solver,
                    "opens_external_app": False,
                    "approval": "solver-execution" if starts_solver else "none",
                    "cost": cost,
                    "availability_reason": availability_reason,
                }
            )

        add(
            "status",
            f"agentcfd status {project_argument} --json",
            available=True,
            availability_reason="Project state is always observable.",
            cost="metadata",
        )
        add(
            "project",
            f"agentcfd project {project_argument} --json",
            available=True,
            availability_reason="The unified compact project view is always available.",
            cost="bounded-io",
        )
        add(
            "observations",
            f"agentcfd observations {project_argument} --json",
            available=state != "running",
            availability_reason=(
                "Readable output intent can be resolved without opening field data."
                if state != "running"
                else "Use the frozen run plan while execution is active."
            ),
            cost="bounded-io",
        )
        add(
            "check",
            f"agentcfd check {project_argument} --json",
            available=True,
            availability_reason="Input and provider readiness can always be checked.",
            cost="bounded-io",
        )
        generated_geometry = self._generated_geometry_state()
        if generated_geometry is not None:
            synchronized = generated_geometry["synchronized"] is True
            add(
                "geometry-sync",
                (
                    f"agentcfd geometry-sync {project_argument} --json"
                    if synchronized
                    else f"agentcfd geometry-sync {project_argument} --apply --json"
                ),
                available=state != "running",
                availability_reason=(
                    "Generated geometry is synchronized; preview remains available."
                    if synchronized
                    else "Refresh the derived geometry before another solver run."
                ),
                cost="bounded-io",
            )
        add(
            "plan",
            f"agentcfd plan {project_argument} --json",
            available=state != "running",
            availability_reason=(
                "A run is active; inspect status instead of resolving changed intent."
                if state == "running"
                else "Current readable intent can be resolved without starting a solver."
            ),
            cost="bounded-io",
        )
        run_available = (
            state != "running" and status["readiness"]["ready_to_run"] is True
        )
        add(
            "run",
            f"agentcfd run {project_argument}",
            available=run_available,
            availability_reason=(
                "The project is ready for deterministic provider execution."
                if run_available
                else "Resolve readiness issues or wait for the active run."
            ),
            cost="solver",
        )
        add(
            "watch",
            f"agentcfd watch {project_argument} --json",
            available=state == "running",
            availability_reason=(
                "A live run can be followed with bounded polling."
                if state == "running"
                else "No live run needs polling."
            ),
            cost="bounded-io",
        )
        add(
            "logs",
            f"agentcfd logs {project_argument} --json",
            available=has_logs,
            availability_reason=(
                "Bounded solver evidence is available."
                if has_logs
                else "No live or published solver log exists."
            ),
            cost="bounded-io",
        )
        add(
            "diagnose",
            f"agentcfd diagnose {project_argument} --json",
            available=has_logs and state in {"failed", "interrupted"},
            availability_reason=(
                "A failed or interrupted run has bounded evidence to classify."
                if has_logs and state in {"failed", "interrupted"}
                else "Diagnosis is reserved for failed or interrupted runs with logs."
            ),
            cost="bounded-io",
        )
        add(
            "result",
            f"agentcfd result {project_argument} --json",
            available=has_result,
            availability_reason=(
                "A compact published result exists."
                if has_result
                else "Run the project before requesting result quantities."
            ),
            cost="bounded-io",
        )
        add(
            "view",
            f"agentcfd view {project_argument}",
            available=has_view,
            availability_reason=(
                "A published result target exists."
                if has_view
                else "No published result target exists."
            ),
            cost="metadata",
        )
        add(
            "verify",
            f"agentcfd verify project {project_argument} --json",
            available=has_result,
            availability_reason=(
                "A result exists for full artifact integrity verification."
                if has_result
                else "No result exists to verify."
            ),
            cost="full-integrity",
        )
        add(
            "archive",
            f"agentcfd archive {project_argument} --plan-only --json",
            available=has_result and state != "running",
            availability_reason=(
                "A completed result can be verified and inventoried for handoff."
                if has_result and state != "running"
                else "Archive planning requires a published result and no active run."
            ),
            cost="full-integrity",
        )
        add(
            "performance",
            f"agentcfd performance {project_argument} --json",
            available=state != "running",
            availability_reason=(
                "Runtime history can be compared with current resolved intent."
                if state != "running"
                else "Use live progress while the solver is active."
            ),
            cost="bounded-io",
        )
        add(
            "campaigns",
            f"agentcfd campaigns {project_argument} --json",
            available=self.manifest.run_mode == "campaign",
            availability_reason=(
                "The project uses immutable campaign mode."
                if self.manifest.run_mode == "campaign"
                else "This project uses replace mode."
            ),
            cost="bounded-io",
        )
        add(
            "storage",
            f"agentcfd storage {project_argument} --json",
            available=True,
            availability_reason="Managed storage can be inventoried explicitly.",
            cost="recursive-io",
        )
        add(
            "clean",
            f"agentcfd clean {project_argument} --json",
            available=state != "running",
            availability_reason=(
                "A non-mutating cleanup preview is available."
                if state != "running"
                else "Do not plan cleanup while a solver is active."
            ),
            cost="recursive-io",
        )
        return {
            "schema": "agentcfd.project-actions/0.1",
            "root": str(self.root),
            "state": state,
            "recommended_operation": recommended["operation"],
            "actions": records,
            "observation_cost": {
                "field_payloads_opened": 0,
                "recursive_storage_scan": False,
            },
        }

    def verify(
        self,
        *,
        run_id: str | None = None,
        verify_fields: bool = True,
    ) -> dict[str, object]:
        """Verify one published project result and its portable field bundle.

        Unlike :meth:`snapshot`, this explicit integrity operation hashes every
        result artifact and opens the standard XDMF/H5 bundle when present.
        """

        if not isinstance(verify_fields, bool):
            raise ValueError("verify_fields must be a boolean.")
        selected = self._select_run_record(run_id)
        if selected is None:
            raise ProjectError("No project result exists; run the project first.")
        run_directory = self._record_directory(selected)
        if run_directory is None:
            raise ProjectError("The selected run directory cannot be resolved.")
        run_path = run_directory / "run.json"
        plan_path = run_directory / "plan.json"
        result_path = run_directory / "result.json"
        summary_path = run_directory / "summary.json"
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
                raise ValueError(
                    "Run marker identity disagrees with project discovery."
                )
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

        summary_record = None
        summary_expected = summary_path.is_file() or (
            isinstance(run_record, Mapping)
            and isinstance(run_record.get("summary"), str)
        )
        if summary_expected:
            try:
                summary_record = strict_json_object(
                    summary_path.read_text(encoding="utf-8"),
                    label=f"AgentCFD result summary {summary_path}",
                )
                summary_schema = summary_record.get("schema")
                if summary_schema not in {
                    "agentcfd.result-summary/0.2",
                    "agentcfd.result-summary/0.3",
                }:
                    raise ValueError("Unsupported AgentCFD result-summary schema.")
                if result_record is None or run_record is None:
                    raise ValueError("Summary source records are unavailable.")
                source_result = summary_record.get("source_result")
                if not isinstance(source_result, Mapping):
                    raise ValueError("Summary source result identity is missing.")
                computed_result_sha256 = file_sha256(result_path)
                expected_failed_checks = [
                    check
                    for check in result_record["checks"]
                    if check.get("passed") is False
                ]
                expected_requirements = [
                    check
                    for check in result_record["checks"]
                    if check.get("kind") == "requirement"
                ]
                comparisons = {
                    "run_id": run_record.get("run_id"),
                    "status": result_record.get("status"),
                    "converged": result_record.get("converged"),
                    "accepted": result_record.get("accepted"),
                    "trust_level": result_record.get("trust_level"),
                    "provider": result_record.get("provider"),
                    "quantities": result_record.get("quantities"),
                    "histories": result_record.get("histories"),
                    "fields": result_record.get("fields"),
                    "check_count": len(result_record["checks"]),
                    "failed_checks": expected_failed_checks,
                    "requirements": expected_requirements,
                    "provenance": result_record.get("provenance"),
                }
                if summary_schema == "agentcfd.result-summary/0.3":
                    comparisons["scientific_inputs"] = result_record.get(
                        "scientific_inputs"
                    )
                disagreements = [
                    name
                    for name, expected in comparisons.items()
                    if summary_record.get(name) != expected
                ]
                if not _project_record_path_matches(
                    summary_record.get("root"), self.root, root=self.root
                ):
                    disagreements.append("root")
                if not _project_record_path_matches(
                    summary_record.get("result"), result_path, root=self.root
                ):
                    disagreements.append("result")
                if not _project_record_path_matches(
                    source_result.get("path"), result_path, root=self.root
                ):
                    disagreements.append("source_result.path")
                if source_result.get("bytes") != result_path.stat().st_size:
                    disagreements.append("source_result.bytes")
                if source_result.get("sha256") != computed_result_sha256:
                    disagreements.append("source_result.sha256")
                if run_record.get("result_sha256") not in {
                    None,
                    computed_result_sha256,
                }:
                    disagreements.append("run.result_sha256")
                if disagreements:
                    raise ValueError(
                        "Summary and verified result disagree: "
                        + ", ".join(disagreements)
                        + "."
                    )
            except (OSError, KeyError, TypeError, ValueError) as error:
                add_check(
                    "SUMMARY_INTEGRITY",
                    False,
                    str(error),
                    summary_path,
                    "Regenerate the managed summary from case.py with `agentcfd run .`.",
                )
            else:
                add_check(
                    "SUMMARY_INTEGRITY",
                    True,
                    "The lightweight decision artifact exactly matches the verified result.",
                    summary_path,
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
        if present_fields and not verify_fields:
            add_check(
                "FIELD_BUNDLE_NOT_SELECTED",
                True,
                (
                    "Portable field structure was not opened because this trust "
                    "operation explicitly excludes field payloads. Registered "
                    "artifact bytes remain covered by result integrity."
                ),
                fields_directory,
                "",
            )
        elif present_fields:
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
                "summary_json_bytes_read": summary_path.stat().st_size
                if summary_path.is_file()
                else 0,
                "control_files_hashed": 1 if summary_record is not None else 0,
                "artifacts_hashed": artifact_count,
                "field_payloads_opened": field_payloads_opened,
                "recursive_storage_scan": False,
            },
            "next_action": next_action,
        }

    def archive_plan(
        self,
        *,
        profile: str = "decision",
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Preview a verified compact handoff without creating another file."""

        return archives.plan_project_archive(self, profile=profile, run_id=run_id)

    def export_archive(
        self,
        destination: str | Path,
        *,
        profile: str = "decision",
        run_id: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        """Atomically export one verified decision or portable project archive."""

        return archives.export_project_archive(
            self,
            destination,
            profile=profile,
            run_id=run_id,
        )

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
            "Review failed scientific checks or unmet design requirements before design or training use.",
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
            int(cells) * nominal_steps * correctors if isinstance(cells, int) else None
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
                "estimated_portable_bytes": output_plan.get("estimated_portable_bytes"),
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
        _step: Step | None = None,
    ) -> ProjectRun:
        selected_name = provider or self.manifest.default_provider
        if (
            design_point_name is not None
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", design_point_name) is None
        ):
            raise ProjectError("Invalid campaign design-point name.")
        selected_parameters = self._parameters(parameters)
        step = _step if _step is not None else self.load_step(selected_parameters)
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
        base_run_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{model_sha[:8]}"
        if campaign_mode:
            with _RUN_ALLOCATION_LOCK:
                run_id = base_run_id
                run_directory = self.root / "campaigns" / run_id
                collision = 1
                while run_directory.exists():
                    collision += 1
                    run_id = f"{base_run_id}-{collision}"
                    run_directory = self.root / "campaigns" / run_id
                run_directory.mkdir(parents=True)
        else:
            run_id = base_run_id
            run_directory = self.run_root
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
        if not campaign_mode:
            run_directory.mkdir(parents=True, exist_ok=True)
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

        def write_workspace_marker(*, retention_reason: str, protected: bool) -> None:
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
        checkpoint_archive = (
            run_directory / "evidence" / "restart.zip"
            if isinstance(selected, OpenFOAMChannelProvider)
            and step.output.checkpoints.enabled
            else None
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
                    expected_analysis_sha256=str(plan["model"]["analysis_sha256"]),
                    checkpoint_archive=checkpoint_archive,
                )
            elif isinstance(selected, OpenFOAMChannelProvider):
                result = selected.run(
                    step,
                    checkpoint_archive=checkpoint_archive,
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

            def update_field_export_progress(
                progress: Mapping[str, object],
            ) -> None:
                progress_record = {
                    **dict(progress),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                write_marker(
                    status="exporting",
                    phase="portable-fields",
                    field_export=progress_record,
                )

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
                            str(case_directory.resolve())
                            if retained_workspace
                            else None
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
                    maximum_frames=step.output.frames.maximum,
                    _progress_callback=update_field_export_progress,
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
            conversion_log = case_directory / "log.foamToVTK"
            if conversion_log.is_file():
                result.artifacts["log_foamToVTK"] = Artifact.from_path(
                    conversion_log,
                    role="field-conversion-log",
                    media_type="text/plain",
                )
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
                        step.output.layouts,
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
        result_record = result.summary()
        result_record["checks"] = [check.as_dict() for check in result.checks]
        result_record["scientific_inputs"] = result.scientific_input_manifest()
        result_summary = self._result_summary_payload(
            run_record,
            result_path=result_path,
            record=result_record,
            result_json_bytes_read=0,
        )
        _write_json_atomic(run_directory / "summary.json", result_summary)
        run_record["result_sha256"] = result_summary["source_result"]["sha256"]
        _write_output_guide(completed, model_name=step.model.name)
        _write_json_atomic(run_directory / "run.json", run_record)
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
            source_workspace = self.root / ".agentcfd" / "work" / _resume_source_run_id
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
            raise ProjectError(
                "The current project is not ready for checkpoint resume."
            )
        native_status = str(latest.get("status", "unknown"))
        interrupted = native_status in {
            "preparing",
            "running",
            "exporting",
        } and not _process_is_alive(latest.get("pid"))
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
                outputs.flow_uniformity("outlet-quality", region="outlet"),
                outputs.force_report("baffle-drag", regions=("baffle",)),
            ),
            views=(
                outputs.slice_view(
                    "midplane-vorticity",
                    field="fluid.vorticity",
                    origin=(0.6, 0.1, 0.05),
                    normal=(0.0, 0.0, 1.0),
                    component="normal",
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
            layouts=(
                outputs.render_layout(
                    "wake-overview",
                    views=("midplane-vorticity", "wake-streamlines"),
                    columns=2,
                    export=outputs.render(size=(1280, 720)),
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
    outlet_target_fractions: Mapping[str, float] | None,
    maximum_fraction_error: float | None,
    base_size_m: float,
    maximum_cells: int,
) -> str:
    default_velocity = inlet_velocity_m_s or (0.0, 0.0, 0.0)
    inlet_name = next(name for name, role in roles.items() if role == "inlet")
    outlet_names = tuple(
        sorted(name for name, role in roles.items() if role == "outlet")
    )
    if len(outlet_names) == 1:
        report_snippets = (
            "outputs.pressure_loss(\n"
            '    "system-loss",\n'
            f"    inlet={inlet_name!r},\n"
            f"    outlet={outlet_names[0]!r},\n"
            "),",
            "outputs.flow_uniformity(\n"
            '    "outlet-quality",\n'
            f"    region={outlet_names[0]!r},\n"
            "),",
        )
    else:
        target_line = (
            f"    targets={dict(outlet_target_fractions)!r},\n"
            if outlet_target_fractions is not None
            else ""
        )
        report_snippets = (
            "outputs.flow_distribution(\n"
            '    "flow-split",\n'
            f"    inlet={inlet_name!r},\n"
            f"    outlets={outlet_names!r},\n"
            f"{target_line}"
            "),",
        )
    report_block = "\n".join(
        "                " + snippet.replace("\n", "\n                ")
        for snippet in report_snippets
    )
    criteria_block = ""
    if maximum_fraction_error is not None:
        criteria_block = f"""
            criteria=(
                outputs.require(
                    "flow-split-target",
                    quantity="report.flow-split.maximum_fraction_error",
                    unit="1",
                    maximum={maximum_fraction_error!r},
                ),
            ),"""
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
    return f'''"""Industrial internal flow: edit engineering intent, not OpenFOAM files."""

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
    generation_path = root / "geometry/generation.json"
    if generation_path.is_file():
        generation = json.loads(generation_path.read_text())
        interior_point_m = tuple(generation["recommendations"]["interior_point_m"])
    else:
        interior_point_m = {interior_point_m!r}
    domain = geometry.imported_surface_from_inspection(
        inspection,
        asset={asset!r},
        interior_point_m=interior_point_m,
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
        output_request = outputs.standard(
            reports=(
{report_block}
            ),{criteria_block}
        )
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
            reports=(
{report_block}
            ),{criteria_block}
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
    ``case.py`` remains the project's physics and operating source of truth;
    managed generated templates keep geometry intent in ``geometry/spec.json``.
    """

    if not isinstance(request, Mapping):
        raise ProjectError("Project creation request must be a JSON object.")
    payload = dict(request)
    allowed = {
        "schema",
        "template",
        "provider",
        "geometry",
        "generated_geometry",
        "interior_point_m",
        "inlet_velocity_m_s",
        "inlet_mass_flow_kg_s",
        "inlet_total_gauge_pressure_pa",
        "mesh",
        "parameters",
        "flow_distribution",
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
        raise ProjectError(
            "Project creation request requires template and provider strings."
        )
    if template == "industrial-elbow":
        unexpected = sorted(
            key
            for key in ("geometry", "interior_point_m", "parameters", "flow_distribution")
            if key in payload
        )
        if unexpected:
            raise ProjectError(
                "Template 'industrial-elbow' does not accept request sections: "
                + ", ".join(unexpected)
                + "."
            )
        missing = sorted(
            key for key in ("generated_geometry", "mesh") if key not in payload
        )
        if missing:
            raise ProjectError(
                "Industrial-elbow project creation request is missing: "
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
                "Industrial-elbow project creation requires exactly one of "
                "inlet_velocity_m_s, inlet_mass_flow_kg_s, or "
                "inlet_total_gauge_pressure_pa."
            )
        generated = payload["generated_geometry"]
        mesh_record = payload["mesh"]
        if not isinstance(generated, Mapping):
            raise ProjectError("Project generated_geometry must be an object.")
        if not isinstance(mesh_record, Mapping):
            raise ProjectError("Project creation mesh must be an object.")
        generated_allowed = {
            "type",
            "diameter_m",
            "bend_radius_m",
            "inlet_length_m",
            "outlet_length_m",
            "cross_section_segments",
            "bend_segments",
            "inlet_segments",
            "outlet_segments",
        }
        generated_unknown = sorted(set(generated) - generated_allowed)
        generated_missing = sorted(
            {"type", "diameter_m", "bend_radius_m", "inlet_length_m", "outlet_length_m"}
            - set(generated)
        )
        mesh_unknown = sorted(set(mesh_record) - {"base_size_m", "maximum_cells"})
        mesh_missing = sorted(
            {"base_size_m", "maximum_cells"} - set(mesh_record)
        )
        if generated_unknown or generated_missing or mesh_unknown or mesh_missing:
            details = [
                *(f"generated_geometry.{name}" for name in generated_missing),
                *(f"unknown generated_geometry.{name}" for name in generated_unknown),
                *(f"mesh.{name}" for name in mesh_missing),
                *(f"unknown mesh.{name}" for name in mesh_unknown),
            ]
            raise ProjectError(
                "Industrial-elbow project creation request is invalid: "
                + ", ".join(details)
                + "."
            )
        if generated["type"] != "circular-elbow-90deg":
            raise ProjectError(
                "Industrial-elbow generated_geometry.type must be "
                "'circular-elbow-90deg'."
            )
        try:
            return init_project(
                directory,
                provider=provider,
                template=template,
                diameter_m=generated["diameter_m"],
                bend_radius_m=generated["bend_radius_m"],
                inlet_length_m=generated["inlet_length_m"],
                outlet_length_m=generated["outlet_length_m"],
                cross_section_segments=generated.get("cross_section_segments"),
                bend_segments=generated.get("bend_segments"),
                inlet_segments=generated.get("inlet_segments"),
                outlet_segments=generated.get("outlet_segments"),
                inlet_velocity_m_s=payload.get("inlet_velocity_m_s"),
                inlet_mass_flow_kg_s=payload.get("inlet_mass_flow_kg_s"),
                inlet_total_gauge_pressure_pa=payload.get(
                    "inlet_total_gauge_pressure_pa"
                ),
                base_size_m=mesh_record["base_size_m"],
                maximum_cells=mesh_record["maximum_cells"],
            )
        except (TypeError, ValueError) as error:
            raise ProjectError(str(error)) from error
    if template == "heated-pipe":
        unexpected = sorted(
            key
            for key in (
                "geometry",
                "generated_geometry",
                "interior_point_m",
                "inlet_velocity_m_s",
                "inlet_mass_flow_kg_s",
                "inlet_total_gauge_pressure_pa",
                "mesh",
                "flow_distribution",
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
                "generated_geometry",
                "interior_point_m",
                "inlet_velocity_m_s",
                "inlet_mass_flow_kg_s",
                "inlet_total_gauge_pressure_pa",
                "mesh",
                "parameters",
                "flow_distribution",
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

    if "generated_geometry" in payload:
        raise ProjectError(
            "Template 'imported-internal-flow' does not accept generated_geometry."
        )
    missing = sorted(
        key for key in ("geometry", "interior_point_m", "mesh") if key not in payload
    )
    if missing:
        raise ProjectError(
            "Imported project creation request is missing: " + ", ".join(missing) + "."
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
    flow_distribution_record = payload.get("flow_distribution")
    if not isinstance(geometry_record, Mapping):
        raise ProjectError("Project creation geometry must be an object.")
    if not isinstance(mesh_record, Mapping):
        raise ProjectError("Project creation mesh must be an object.")
    if flow_distribution_record is not None and not isinstance(
        flow_distribution_record, Mapping
    ):
        raise ProjectError("Project creation flow_distribution must be an object.")
    geometry_unknown = sorted(
        set(geometry_record)
        - {
            "path",
            "unit",
            "boundary_roles",
            "role_confirmation",
            "accept_multiple_components",
        }
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
            "Unknown project creation mesh keys: " + ", ".join(mesh_unknown) + "."
        )
    if flow_distribution_record is not None:
        flow_distribution_unknown = sorted(
            set(flow_distribution_record) - {"targets", "maximum_fraction_error"}
        )
        if flow_distribution_unknown:
            raise ProjectError(
                "Unknown project creation flow_distribution keys: "
                + ", ".join(flow_distribution_unknown)
                + "."
            )
        if "targets" not in flow_distribution_record:
            raise ProjectError("Project creation flow_distribution requires targets.")
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
    accept_multiple_components = geometry_record.get(
        "accept_multiple_components", False
    )
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
        raise ProjectError(
            "Project creation geometry.boundary_roles must be an object."
        )
    if role_confirmation is not None and role_confirmation != "accept-name-suggestions":
        raise ProjectError(
            "Project creation geometry.role_confirmation must be "
            "'accept-name-suggestions'."
        )
    if not isinstance(accept_multiple_components, bool):
        raise ProjectError(
            "Project creation geometry.accept_multiple_components must be boolean."
        )
    source = Path(geometry_path).expanduser()
    if not source.is_absolute():
        base = (
            Path.cwd() if base_directory is None else Path(base_directory).expanduser()
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
        accept_multiple_components=accept_multiple_components,
        interior_point_m=payload["interior_point_m"],
        inlet_velocity_m_s=payload.get("inlet_velocity_m_s"),
        inlet_mass_flow_kg_s=payload.get("inlet_mass_flow_kg_s"),
        inlet_total_gauge_pressure_pa=payload.get("inlet_total_gauge_pressure_pa"),
        base_size_m=mesh_record["base_size_m"],
        maximum_cells=mesh_record["maximum_cells"],
        outlet_target_fractions=(
            flow_distribution_record["targets"]
            if flow_distribution_record is not None
            else None
        ),
        maximum_fraction_error=(
            flow_distribution_record.get("maximum_fraction_error")
            if flow_distribution_record is not None
            else None
        ),
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
    accept_multiple_components: bool = False,
    interior_point_m: tuple[float, float, float] | None = None,
    inlet_velocity_m_s: tuple[float, float, float] | None = None,
    inlet_mass_flow_kg_s: float | None = None,
    inlet_total_gauge_pressure_pa: float | None = None,
    base_size_m: float | None = None,
    maximum_cells: int | None = None,
    outlet_target_fractions: Mapping[str, float] | None = None,
    maximum_fraction_error: float | None = None,
    parameter_defaults: Mapping[str, object] | None = None,
    diameter_m: float | None = None,
    bend_radius_m: float | None = None,
    inlet_length_m: float | None = None,
    outlet_length_m: float | None = None,
    cross_section_segments: int | None = None,
    bend_segments: int | None = None,
    inlet_segments: int | None = None,
    outlet_segments: int | None = None,
) -> Project:
    """Create a complete editable project without overwriting user data."""

    if provider not in {"reference", "openfoam"}:
        raise ValueError("Project provider must be 'reference' or 'openfoam'.")
    template_spec = project_templates.get(template)
    if provider not in template_spec.providers:
        if len(template_spec.providers) == 1:
            raise ValueError(
                f"The {template} template requires "
                f"provider={template_spec.providers[0]!r}."
            )
        raise ValueError(
            f"The {template} template supports providers "
            f"{template_spec.providers}, not {provider!r}."
        )
    generated_options = (
        diameter_m,
        bend_radius_m,
        inlet_length_m,
        outlet_length_m,
        cross_section_segments,
        bend_segments,
        inlet_segments,
        outlet_segments,
    )
    if template == "industrial-elbow":
        forbidden_imported_options = (
            geometry_path,
            geometry_unit,
            boundary_roles,
            accept_name_roles,
            accept_multiple_components,
            interior_point_m,
            outlet_target_fractions,
            maximum_fraction_error,
            parameter_defaults,
        )
        if any(
            value is not None and value is not False
            for value in forbidden_imported_options
        ):
            raise ValueError(
                "The industrial-elbow template generates and owns its geometry; "
                "do not pass imported geometry, roles, interior point, distribution, "
                "or parameter-default options."
            )
        missing_dimensions = [
            name
            for name, value in (
                ("diameter_m", diameter_m),
                ("bend_radius_m", bend_radius_m),
                ("inlet_length_m", inlet_length_m),
                ("outlet_length_m", outlet_length_m),
            )
            if value is None
        ]
        if missing_dimensions:
            raise ValueError(
                "Industrial-elbow initialization requires: "
                + ", ".join(missing_dimensions)
                + "."
            )
        root = Path(directory)
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"Project directory is not empty: {root}")
        assert diameter_m is not None
        assert bend_radius_m is not None
        assert inlet_length_m is not None
        assert outlet_length_m is not None
        with tempfile.TemporaryDirectory(prefix="agentcfd-elbow-") as temporary:
            generated_path = Path(temporary) / "fluid.stl"
            _, generated_report = geometry_generation.write_circular_elbow_stl(
                generated_path,
                diameter_m=diameter_m,
                bend_radius_m=bend_radius_m,
                inlet_length_m=inlet_length_m,
                outlet_length_m=outlet_length_m,
                cross_section_segments=(
                    32 if cross_section_segments is None else cross_section_segments
                ),
                bend_segments=bend_segments,
                inlet_segments=inlet_segments,
                outlet_segments=outlet_segments,
            )
            recommendations = generated_report["recommendations"]
            assert isinstance(recommendations, dict)
            mesh_start = recommendations["mesh_starting_point"]
            assert isinstance(mesh_start, dict)
            project = init_project(
                directory,
                provider="openfoam",
                template="imported-internal-flow",
                geometry_path=generated_path,
                geometry_unit="m",
                boundary_roles={"inlet": "inlet", "outlet": "outlet", "walls": "wall"},
                interior_point_m=tuple(recommendations["interior_point_m"]),
                inlet_velocity_m_s=inlet_velocity_m_s,
                inlet_mass_flow_kg_s=inlet_mass_flow_kg_s,
                inlet_total_gauge_pressure_pa=inlet_total_gauge_pressure_pa,
                base_size_m=(
                    mesh_start["base_size_m"] if base_size_m is None else base_size_m
                ),
                maximum_cells=(
                    mesh_start["maximum_cells"]
                    if maximum_cells is None
                    else maximum_cells
                ),
            )
        manifest_path = project.root / "agentcfd.toml"
        manifest_text = manifest_path.read_text(encoding="utf-8")
        marker = 'run_mode = "replace"\n'
        imported_template_marker = 'template = "imported-internal-flow"\n'
        if marker not in manifest_text or imported_template_marker not in manifest_text:
            raise ProjectError(
                "Generated geometry project requires a typed replace-mode manifest."
            )
        manifest_temporary = manifest_path.with_suffix(".toml.tmp")
        manifest_temporary.write_text(
            manifest_text.replace(
                marker,
                marker + 'generated_geometry_spec = "geometry/spec.json"\n',
                1,
            ).replace(
                imported_template_marker,
                'template = "industrial-elbow"\n',
                1,
            ),
            encoding="utf-8",
        )
        manifest_temporary.replace(manifest_path)
        project = Project(project.root)
        resolved_parameters = generated_report["geometry"]["parameters"]
        assert isinstance(resolved_parameters, dict)
        geometry_directory = project.root / "geometry"
        _write_json_atomic(
            geometry_directory / "spec.json",
            {
                "schema": "agentcfd.generated-geometry-spec/0.1",
                "type": "circular-elbow-90deg",
                "parameters": dict(resolved_parameters),
            },
        )
        _write_json_atomic(
            geometry_directory / "generation.json",
            _portable_generated_geometry_report(
                generated_report,
                next_command="agentcfd status .",
            ),
        )
        existing_readme = (project.root / "README.md").read_text(encoding="utf-8")
        workflow_start = existing_readme.index("Edit `case.py`")
        (project.root / "README.md").write_text(
            "# AgentCFD industrial-elbow\n\n"
            "This project owns a parameterized circular 90-degree elbow. Edit "
            "`geometry/spec.json`, preview the exact derived identity with "
            "`agentcfd geometry-sync . --json`, then apply it with "
            "`agentcfd geometry-sync . --apply`. The managed `fluid.stl`, "
            "`generation.json`, boundary roles, and independent inspection stay in "
            "`geometry/`; stale or inconsistent geometry blocks solver execution. "
            "Mesh defaults are a bounded starting point, not accuracy evidence. "
            "`case.py` owns the operating point, fluid, turbulence choice, mesh "
            "budget, and requested results.\n\n"
            + existing_readme[workflow_start:],
            encoding="utf-8",
        )
        agent_instructions = (project.root / "AGENTS.md").read_text(encoding="utf-8")
        (project.root / "AGENTS.md").write_text(
            agent_instructions.replace(
                "Treat `case.py` as the modeling source of truth.",
                "Treat `case.py` as the physics and operating source of truth, and "
                "`geometry/spec.json` as generated-geometry intent. Never edit "
                "`geometry/fluid.stl`, `generation.json`, or `inspection.json` "
                "directly; preview and apply `agentcfd geometry-sync` instead.",
                1,
            ),
            encoding="utf-8",
        )
        synchronized = project._generated_geometry_state()
        if synchronized is None or synchronized["synchronized"] is not True:
            raise ProjectError(
                "Generated elbow project was created but its geometry evidence is inconsistent."
            )
        return project
    if any(value is not None for value in generated_options):
        raise ValueError(
            "Elbow geometry parameters require template='industrial-elbow'."
        )
    imported_options = (
        geometry_path,
        geometry_unit,
        boundary_roles,
        accept_name_roles,
        accept_multiple_components,
        interior_point_m,
        inlet_velocity_m_s,
        inlet_mass_flow_kg_s,
        inlet_total_gauge_pressure_pa,
        base_size_m,
        maximum_cells,
        outlet_target_fractions,
        maximum_fraction_error,
    )
    if template != "imported-internal-flow" and any(
        value is not None and value is not False for value in imported_options
    ):
        raise ValueError(
            "Imported geometry options require template='imported-internal-flow'."
        )
    if template != "heated-pipe" and parameter_defaults is not None:
        raise ValueError("parameter_defaults currently require template='heated-pipe'.")

    imported_report: dict[str, object] | None = None
    imported_source: Path | None = None
    imported_asset: str | None = None
    normalized_roles: dict[str, str] | None = None
    case_template: str
    if template == "imported-internal-flow":
        if not isinstance(accept_name_roles, bool):
            raise ValueError("accept_name_roles must be a boolean.")
        if not isinstance(accept_multiple_components, bool):
            raise ValueError("accept_multiple_components must be a boolean.")
        if boundary_roles is not None and accept_name_roles:
            raise ValueError(
                "Choose either explicit boundary_roles or accept_name_roles, not both."
            )
        missing = [
            name
            for name, value in (
                ("geometry_path", geometry_path),
                ("geometry_unit", geometry_unit),
                (
                    "boundary_roles or accept_name_roles",
                    boundary_roles or accept_name_roles,
                ),
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
            boundaries.PressureInlet(inlet_total_gauge_pressure_pa).total_gauge_pressure
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
                accept_multiple_components=accept_multiple_components,
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
            accept_multiple_components=accept_multiple_components,
        )
        if not imported_report["readiness"]["ready_for_import_setup"]:
            repairs = [
                str(issue["repair"])
                for issue in imported_report["issues"]
                if issue["severity"] == "error"
            ]
            raise ProjectError("Imported geometry is not ready: " + " ".join(repairs))
        role_values = tuple(normalized_roles.values())
        unsupported = sorted(
            set(role_values) - {"inlet", "outlet", "wall", "symmetry", "empty"}
        )
        if unsupported:
            raise ProjectError(
                "The released imported internal-flow template does not support roles: "
                + ", ".join(unsupported)
                + "."
            )
        if role_values.count("inlet") != 1 or role_values.count("outlet") < 1:
            raise ProjectError(
                "The imported internal-flow template requires exactly one inlet and at least one outlet."
            )
        inlet_name = next(
            name for name, role in normalized_roles.items() if role == "inlet"
        )
        outlet_names = tuple(
            sorted(name for name, role in normalized_roles.items() if role == "outlet")
        )
        normalized_targets: dict[str, float] | None = None
        if outlet_target_fractions is not None:
            if not isinstance(outlet_target_fractions, Mapping):
                raise ValueError("outlet_target_fractions must be a mapping.")
            if not outlet_target_fractions:
                raise ValueError("outlet_target_fractions must not be empty.")
            if len(outlet_names) < 2:
                raise ValueError(
                    "Outlet target fractions require at least two outlet-role surfaces."
                )
            distribution = outputs.flow_distribution(
                "flow-split",
                inlet=inlet_name,
                outlets=outlet_names,
                targets=outlet_target_fractions,
            )
            normalized_targets = dict(distribution.target_fractions)
        if maximum_fraction_error is not None:
            if normalized_targets is None:
                raise ValueError(
                    "maximum_fraction_error requires complete outlet target fractions."
                )
            if (
                isinstance(maximum_fraction_error, bool)
                or not isinstance(maximum_fraction_error, (int, float))
                or not math.isfinite(maximum_fraction_error)
                or not 0.0 <= maximum_fraction_error <= 1.0
            ):
                raise ValueError(
                    "maximum_fraction_error must be a finite fraction from zero to one."
                )
            selected_maximum_fraction_error = float(maximum_fraction_error)
        else:
            selected_maximum_fraction_error = None
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
            outlet_target_fractions=normalized_targets,
            maximum_fraction_error=selected_maximum_fraction_error,
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
template = "{template}"
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
