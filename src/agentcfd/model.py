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

    def boundaries(self, **named: boundary_types.Boundary) -> "Model":
        """Attach explicitly named engineering boundaries and return this model."""

        for name, condition in named.items():
            if not name.strip():
                raise ValueError("Boundary names cannot be empty.")
            self._boundaries[name] = condition
        return self

    @property
    def boundary_conditions(self) -> dict[str, boundary_types.Boundary]:
        return dict(self._boundaries)

    def validate(self) -> None:
        inlet_count = sum(
            isinstance(
                value,
                (
                    boundary_types.MassFlowInlet,
                    boundary_types.MeanVelocityInlet,
                    boundary_types.FullyDevelopedVelocityInlet,
                ),
            )
            for value in self._boundaries.values()
        )
        outlet_count = sum(isinstance(value, boundary_types.PressureOutlet) for value in self._boundaries.values())
        wall_count = sum(isinstance(value, boundary_types.NoSlipWall) for value in self._boundaries.values())
        if inlet_count != 1:
            raise ModelValidationError("Exactly one mass-flow or mean-velocity inlet is required.")
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
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
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
