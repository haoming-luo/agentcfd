"""One readable project lifecycle shared by people, agents, CLIs, and GUIs."""

from __future__ import annotations

import importlib.util
import json
import math
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
from .model import Step
from .provenance import content_fingerprint, file_sha256
from .providers import OpenFOAMMeshControls, OpenFOAMProvider, ReferencePipeProvider
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
    if provider == "openfoam" and hasattr(step.model.domain, "diameter"):
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
        case_directory: Path | None = None,
        container_image: str | None = None,
    ):
        if name == "reference":
            return ReferencePipeProvider()
        if name != "openfoam":
            raise ProjectError(f"Unknown provider {name!r}.")
        settings = self._openfoam_settings()
        selected_image = container_image or settings.get("container_image")
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
            container_image=container_image,
        )
        descriptor = selected.descriptor()
        study = step.model.study
        if selected_name == "reference":
            required_capability = "reference.hagen-poiseuille"
        else:
            required_capability = (
                "openfoam.transient-incompressible-internal-flow"
                if not study.steady
                else "openfoam.steady-laminar-circular-pipe"
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

    def inspect(self) -> dict[str, object]:
        plan = self.plan()
        runs = []
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
        runs.sort(key=lambda record: str(record.get("completed_at", "")), reverse=True)
        return {
            "schema": "agentcfd.project-inspection/0.1",
            "root": str(self.root),
            "model": plan["model"],
            "readiness": plan["readiness"],
            "issues": plan["issues"],
            "run_count": len(runs),
            "latest_run": runs[0] if runs else None,
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
        now = datetime.now(UTC)
        model_sha = step.model.fingerprint()
        run_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{model_sha[:8]}"
        campaign_mode = campaign or self.manifest.run_mode == "campaign"
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
            shutil.rmtree(run_directory)
        run_directory.mkdir(parents=True, exist_ok=not campaign_mode)
        plan_path = run_directory / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        workspace_root = self.root / ".agentcfd" / "work" / run_id
        case_directory = workspace_root / "openfoam"
        selected = self._provider(
            selected_name,
            case_directory=case_directory if selected_name == "openfoam" else None,
            container_image=container_image,
        )
        result = selected.run(step)
        bundle = None
        if selected_name == "openfoam" and result.status == "completed" and bool(
            self._openfoam_settings().get("export_fields", True)
        ):
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
        retained_workspace = keep_workspace or bool(
            self._openfoam_settings().get("keep_workspace", False)
        )
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
        run_record["completed_at"] = datetime.now(UTC).isoformat()
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


def init_project(
    directory: str | Path,
    *,
    provider: str = "reference",
) -> Project:
    """Create a complete, editable industrial-pipe project without overwriting."""

    if provider not in {"reference", "openfoam"}:
        raise ValueError("Project provider must be 'reference' or 'openfoam'.")
    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Project directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    manifest = f'''schema = "agentcfd.project/0.1"
entrypoint = "case.py"
factory = "build"
default_provider = "{provider}"
run_directory = "output"
run_mode = "replace"

[openfoam]
container_image = "opencfd/openfoam-run:2606"
cross_section_cells = 8
axial_cells = 120
export_fields = true
keep_workspace = false
'''
    (root / "agentcfd.toml").write_text(manifest, encoding="utf-8")
    (root / "case.py").write_text(_CASE_TEMPLATE, encoding="utf-8")
    (root / "README.md").write_text(
        "# AgentCFD industrial pipe\n\n"
        "Edit `case.py`, then use `agentcfd check`, `agentcfd plan`, "
        "`agentcfd run`, and `agentcfd inspect`. Ordinary runs replace the managed "
        "`output/` directory. Use `agentcfd run . --campaign` to preserve an "
        "immutable run, or `--keep-workspace` to retain generated OpenFOAM files.\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# Agent instructions\n\n"
        "Treat `case.py` as the modeling source of truth. Run `agentcfd check . --json` "
        "before execution. Do not edit generated OpenFOAM dictionaries to change scientific intent. "
        "Preserve plan, result, XDMF/H5, selected NPZ, and failed checks together.\n",
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
