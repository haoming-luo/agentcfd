"""The public model and step lifecycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import boundaries as boundary_types
from . import outputs as output_types
from . import procedures as procedure_types
from .errors import ModelValidationError
from .fluids import NewtonianFluid
from .geometry import CircularPipe
from .results import SimulationResult
from .studies import Study


@dataclass(slots=True)
class Model:
    study: Study
    domain: CircularPipe
    fluid: NewtonianFluid
    name: str = "model"
    metadata: dict[str, Any] = field(default_factory=dict)
    _boundaries: dict[str, boundary_types.Boundary] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Model name cannot be empty.")
        if not isinstance(self.study, Study):
            raise TypeError("Model study must be an AgentCFD Study.")
        if not isinstance(self.domain, CircularPipe):
            raise TypeError("Model domain must be an AgentCFD CircularPipe.")
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
                    boundary_types.PressureOutlet,
                    boundary_types.NoSlipWall,
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
        inlet_count = sum(
            isinstance(
                value,
                (
                    boundary_types.MassFlowInlet,
                    boundary_types.MeanVelocityInlet,
                    boundary_types.FullyDevelopedVelocityInlet,
                    boundary_types.TurbulentMeanVelocityInlet,
                ),
            )
            for value in self._boundaries.values()
        )
        outlet_count = sum(isinstance(value, boundary_types.PressureOutlet) for value in self._boundaries.values())
        wall_count = sum(isinstance(value, boundary_types.NoSlipWall) for value in self._boundaries.values())
        if inlet_count != 1:
            raise ModelValidationError(
                "Exactly one mass-flow, mean-velocity, or turbulent mean-velocity inlet is required."
            )
        if outlet_count != 1:
            raise ModelValidationError("Exactly one pressure outlet is required.")
        if wall_count < 1:
            raise ModelValidationError("At least one no-slip wall is required.")

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
        procedure: procedure_types.SteadyProcedure | None = None,
        output: output_types.OutputRequest | None = None,
    ) -> "Step":
        return Step(
            model=self,
            procedure=procedure or procedure_types.steady(),
            output=output or output_types.standard(),
        )


@dataclass(frozen=True, slots=True)
class Step:
    model: Model
    procedure: procedure_types.SteadyProcedure
    output: output_types.OutputRequest

    def __post_init__(self) -> None:
        if not isinstance(self.model, Model):
            raise TypeError("Step model must be an AgentCFD Model.")
        if not isinstance(self.procedure, procedure_types.SteadyProcedure):
            raise TypeError("Step procedure must be an AgentCFD SteadyProcedure.")
        if not isinstance(self.output, output_types.OutputRequest):
            raise TypeError("Step output must be an AgentCFD OutputRequest.")

    def run(self, *, provider: str | object = "reference") -> SimulationResult:
        self.model.validate()
        if isinstance(provider, str):
            if provider != "reference":
                raise ValueError("The initial release exposes only provider='reference' for execution.")
            from .providers import ReferencePipeProvider

            selected = ReferencePipeProvider()
        else:
            selected = provider
        run = getattr(selected, "run", None)
        if run is None:
            raise TypeError("Provider must define run(step).")
        return run(self)
