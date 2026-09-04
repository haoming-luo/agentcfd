"""One readable project lifecycle shared by people, agents, CLIs, and GUIs."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Mapping

from . import boundaries, data_exchange, engineering
from .errors import ModelValidationError, ProjectError
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
        return cls(
            entrypoint=str(strings["entrypoint"]).strip(),
            factory=str(strings["factory"]).strip(),
            default_provider=provider,
            run_directory=str(strings["run_directory"]).strip(),
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

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.project-run/0.1",
            "run_id": self.run_id,
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
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise ProjectError(f"Project entrypoint failed to import: {error}") from error
    finally:
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
        hydraulic_diameter=step.model.domain.diameter,
        dynamic_viscosity=step.model.fluid.dynamic_viscosity,
    )


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
            provider_compatible = (
                model_valid
                and study.steady
                and not study.compressible
                and not study.energy
                and not study.reacting
                and study.laminar
            )
        else:
            required_capability = (
                "openfoam.steady-laminar-circular-pipe"
                if study.laminar
                else "openfoam.steady-rans-smooth-circular-pipe"
            )
            provider_compatible = model_valid and required_capability in descriptor.capabilities
        if not provider_compatible:
            issues.append(
                ProjectIssue(
                    "PROVIDER_INCOMPATIBLE",
                    "error",
                    f"Provider {selected_name!r} does not support the resolved Study.",
                    "case.py:study",
                    "Choose a compatible provider or change the explicit physical Study.",
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
                    "Standard XDMF/H5/NPZ output dependencies are unavailable.",
                    "runtime:io",
                    "Install `agentcfd[io]` in the execution environment.",
                )
            )
        ready_to_run = model_valid and provider_compatible and runtime_available and io_ready
        decisions = {
            "study": study.to_dict(),
            "procedure": step.procedure.to_dict(),
            "outputs": step.output.to_dict(),
            "provider": asdict(descriptor),
            "required_capability": required_capability,
            "mesh_strategy": "structured-circular-pipe-o-grid" if selected_name == "openfoam" else "analytical",
            "solver": "simpleFoam" if selected_name == "openfoam" else "Hagen-Poiseuille",
            "portable_field_bundle": export_fields,
            "portable_formats": ["xdmf", "hdf5", "npz"] if export_fields else [],
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
        if self.run_root.is_dir():
            for path in sorted(self.run_root.glob("*/run.json"), reverse=True):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    runs.append(record)
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
        run_directory = self.run_root / run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        plan_path = run_directory / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        case_directory = run_directory / "openfoam-case"
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
            )
            for name, path, media_type in (
                ("fields.xdmf", bundle.xdmf, "application/x-xdmf+xml"),
                ("fields.hdf5", bundle.hdf5, "application/x-hdf5"),
                ("fields.npz", bundle.npz, "application/x-npz"),
                ("fields.manifest", bundle.manifest, "application/json"),
            ):
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
        result_path = result.write(run_directory / "result.json")
        completed = ProjectRun(
            run_id=run_id,
            directory=run_directory,
            result=result,
            result_path=result_path,
            plan_path=plan_path,
            field_bundle=bundle,
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
run_directory = "runs"

[openfoam]
container_image = "opencfd/openfoam-run:2606"
cross_section_cells = 8
axial_cells = 120
export_fields = true
'''
    (root / "agentcfd.toml").write_text(manifest, encoding="utf-8")
    (root / "case.py").write_text(_CASE_TEMPLATE, encoding="utf-8")
    (root / "README.md").write_text(
        "# AgentCFD industrial pipe\n\n"
        "Edit `case.py`, then use `agentcfd check`, `agentcfd plan`, "
        "`agentcfd run`, and `agentcfd inspect`.\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# Agent instructions\n\n"
        "Treat `case.py` as the modeling source of truth. Run `agentcfd check . --json` "
        "before execution. Do not edit generated OpenFOAM dictionaries to change scientific intent. "
        "Preserve plan, result, XDMF/H5/NPZ, and failed checks together.\n",
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
