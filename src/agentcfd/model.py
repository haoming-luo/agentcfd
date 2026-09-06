"""The public model and step lifecycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import boundaries as boundary_types
from . import initialization as initialization_types
from . import meshing as meshing_types
from . import outputs as output_types
from . import procedures as procedure_types
from .errors import ModelValidationError
from .fluids import NewtonianFluid
from .geometry import CircularPipe, Domain, ImportedSurface, RectangularChannel
from .results import SimulationResult
from .studies import Study


@dataclass(slots=True)
class Model:
    study: Study
    domain: Domain
    fluid: NewtonianFluid
    name: str = "model"
    metadata: dict[str, Any] = field(default_factory=dict)
    _boundaries: dict[str, boundary_types.Boundary] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Model name cannot be empty.")
        if not isinstance(self.study, Study):
            raise TypeError("Model study must be an AgentCFD Study.")
        if not isinstance(
            self.domain, (CircularPipe, RectangularChannel, ImportedSurface)
        ):
            raise TypeError("Model domain must be an AgentCFD CFD domain.")
        if not isinstance(self.fluid, NewtonianFluid):
            raise TypeError("Model fluid must be an AgentCFD NewtonianFluid.")
        if not isinstance(self.metadata, dict):
            raise ValueError("Model metadata must be a dictionary.")
        self.metadata = dict(self.metadata)

    def boundaries(self, **named: boundary_types.Boundary) -> "Model":
        """Attach explicitly named engineering boundaries and return this model."""

        for name, condition in named.items():
            if not name.strip():
                raise ValueError("Boundary names cannot be empty.")
            if not isinstance(
                condition,
                (
                    boundary_types.MassFlowInlet,
                    boundary_types.MeanVelocityInlet,
                    boundary_types.FullyDevelopedVelocityInlet,
                    boundary_types.TurbulentMeanVelocityInlet,
                    boundary_types.PressureInlet,
                    boundary_types.PressureOutlet,
                    boundary_types.MassFlowOutlet,
                    boundary_types.NoSlipWall,
                    boundary_types.SlipWall,
                    boundary_types.Symmetry,
                ),
            ):
                raise TypeError(f"Boundary {name!r} has an unsupported condition type.")
            self._boundaries[name] = condition
        return self

    @property
    def boundary_conditions(self) -> dict[str, boundary_types.Boundary]:
        return dict(self._boundaries)

    def validate(self) -> None:
        def require_string_keys(value: object, path: str) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ModelValidationError(
                            f"Model metadata key at {path} must be a string."
                        )
                    require_string_keys(item, f"{path}.{key}")
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    require_string_keys(item, f"{path}[{index}]")

        require_string_keys(self.metadata, "metadata")
        try:
            json.dumps(self.metadata, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ModelValidationError(
                "Model metadata must be finite, JSON-serializable scientific context."
            ) from error
        surface_names = set(self.domain.surface_names)
        supplied_names = set(self._boundaries)
        unknown_names = sorted(supplied_names - surface_names)
        missing_names = sorted(surface_names - supplied_names)
        if unknown_names:
            raise ModelValidationError(
                "Boundary conditions target unknown domain surfaces: "
                + ", ".join(unknown_names)
                + "."
            )
        inlet_types = (
            boundary_types.MassFlowInlet,
            boundary_types.MeanVelocityInlet,
            boundary_types.FullyDevelopedVelocityInlet,
            boundary_types.TurbulentMeanVelocityInlet,
            boundary_types.PressureInlet,
        )
        outlet_types = (boundary_types.PressureOutlet, boundary_types.MassFlowOutlet)
        wall_types = (
            boundary_types.NoSlipWall,
            boundary_types.SlipWall,
            boundary_types.Symmetry,
        )
        region_roles = {item.name: item.role for item in self.domain.regions}
        for name, condition in self._boundaries.items():
            role = region_roles[name]
            expected = (
                inlet_types
                if role == "inlet"
                else outlet_types
                if role == "outlet"
                else wall_types
            )
            if role in {"inlet", "outlet", "wall"} and not isinstance(
                condition, expected
            ):
                raise ModelValidationError(
                    f"Surface {name!r} has role {role!r} but received "
                    f"{type(condition).__name__}."
                )
        inlet_count = sum(
            isinstance(
                value,
                inlet_types,
            )
            for value in self._boundaries.values()
        )
        outlet_count = sum(
            isinstance(value, outlet_types) for value in self._boundaries.values()
        )
        wall_count = sum(
            isinstance(value, wall_types) for value in self._boundaries.values()
        )
        if inlet_count < 1:
            raise ModelValidationError(
                "At least one inlet boundary condition is required."
            )
        if outlet_count < 1:
            raise ModelValidationError(
                "At least one outlet boundary condition is required."
            )
        if wall_count < 1:
            raise ModelValidationError(
                "At least one wall or symmetry boundary is required."
            )
        if missing_names:
            raise ModelValidationError(
                "Every domain surface requires an explicit boundary condition; missing: "
                + ", ".join(missing_names)
                + "."
            )
        if not any(
            isinstance(
                value, (boundary_types.PressureInlet, boundary_types.PressureOutlet)
            )
            for value in self._boundaries.values()
        ):
            raise ModelValidationError(
                "At least one pressure inlet or pressure outlet is required to set the pressure level."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "agentcfd.model/0.1",
            "name": self.name,
            "study": self.study.to_dict(),
            "domain": self.domain.to_dict(),
            "fluid": self.fluid.to_dict(),
            "boundaries": {
                name: condition.to_dict()
                for name, condition in sorted(self._boundaries.items())
            },
            "metadata": self.metadata,
        }

    def fingerprint(self) -> str:
        self.validate()
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def step(
        self,
        *,
        procedure: procedure_types.Procedure | None = None,
        output: output_types.OutputRequest | None = None,
        initialization: initialization_types.Initialization | None = None,
        mesh: meshing_types.MeshIntent | None = None,
    ) -> "Step":
        return Step(
            model=self,
            procedure=procedure or procedure_types.steady(),
            output=output or output_types.standard(),
            initialization=initialization,
            mesh=mesh,
        )


@dataclass(frozen=True, slots=True)
class Step:
    model: Model
    procedure: procedure_types.Procedure
    output: output_types.OutputRequest
    initialization: initialization_types.Initialization | None = None
    mesh: meshing_types.MeshIntent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, Model):
            raise TypeError("Step model must be an AgentCFD Model.")
        if not isinstance(
            self.procedure,
            (procedure_types.SteadyProcedure, procedure_types.TransientProcedure),
        ):
            raise TypeError("Step procedure must be an AgentCFD solution procedure.")
        if not isinstance(self.output, output_types.OutputRequest):
            raise TypeError("Step output must be an AgentCFD OutputRequest.")
        if self.initialization is not None and not isinstance(
            self.initialization,
            (
                initialization_types.UniformInitialization,
                initialization_types.PotentialFlowInitialization,
                initialization_types.PreviousResultInitialization,
            ),
        ):
            raise TypeError("Step initialization must be an AgentCFD initialization.")
        if self.mesh is not None and not isinstance(
            self.mesh, meshing_types.MeshIntent
        ):
            raise TypeError("Step mesh must be an AgentCFD MeshIntent.")
        if self.model.study.steady != isinstance(
            self.procedure,
            procedure_types.SteadyProcedure,
        ):
            raise ValueError(
                "Study and procedure disagree: steady studies require steady procedures, "
                "and transient studies require transient procedures."
            )
        coordinate = "solver-iteration" if self.model.study.steady else "physical-time"
        if (
            self.output.frames.mode == "interval"
            and self.output.frames.coordinate != coordinate
        ):
            raise ValueError(
                f"Field frames must use {coordinate!r} for this procedure."
            )
        if (
            self.output.checkpoints.enabled
            and self.output.checkpoints.coordinate != coordinate
        ):
            raise ValueError(f"Checkpoints must use {coordinate!r} for this procedure.")
        regions = {item.name: item for item in self.model.domain.regions}
        if self.mesh is not None:
            unknown_mesh_regions = sorted(
                set(self.mesh.referenced_regions) - regions.keys()
            )
            if unknown_mesh_regions:
                raise ValueError(
                    "Mesh controls target unknown regions: "
                    + ", ".join(unknown_mesh_regions)
                    + "."
                )
            for controls in self.mesh.boundary_layers:
                invalid = [
                    name
                    for name in controls.regions
                    if regions[name].kind != "surface" or regions[name].role != "wall"
                ]
                if invalid:
                    raise ValueError(
                        "Boundary layers require wall surfaces; invalid: "
                        + ", ".join(invalid)
                        + "."
                    )
        for report in self.output.reports:
            if isinstance(report, output_types.PointProbe):
                continue
            names = (
                (report.region,)
                if isinstance(report, output_types.SurfaceReport)
                else report.regions
            )
            unknown = sorted(set(names) - regions.keys())
            if unknown:
                raise ValueError(
                    f"Output report {report.name!r} targets unknown regions: "
                    + ", ".join(unknown)
                    + "."
                )
            nonsurfaces = [name for name in names if regions[name].kind != "surface"]
            if nonsurfaces:
                raise ValueError(
                    f"Output report {report.name!r} requires surfaces; invalid: "
                    + ", ".join(nonsurfaces)
                    + "."
                )

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "schema": "agentcfd.analysis-request/0.1",
            "model": self.model.to_dict(),
            "procedure": self.procedure.to_dict(),
            "output": self.output.to_dict(),
        }
        if self.initialization is not None:
            record["initialization"] = self.initialization.to_dict()
        if self.mesh is not None:
            record["mesh"] = self.mesh.to_dict()
        return record

    def fingerprint(self) -> str:
        """Content identity for the complete solver-neutral analysis request."""

        self.model.validate()
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def run(self, *, provider: str | object = "reference") -> SimulationResult:
        self.model.validate()
        if isinstance(provider, str):
            if provider != "reference":
                raise ValueError(
                    "The initial release exposes only provider='reference' for execution."
                )
            from .providers import ReferencePipeProvider

            selected = ReferencePipeProvider()
        else:
            selected = provider
        run = getattr(selected, "run", None)
        if run is None:
            raise TypeError("Provider must define run(step).")
        return run(self)
