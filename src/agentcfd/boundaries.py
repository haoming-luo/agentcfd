"""Engineering boundary conditions."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class MassFlowInlet:
    mass_flow_rate: float

    def __post_init__(self) -> None:
        if self.mass_flow_rate <= 0.0:
            raise ValueError("Mass-flow rate must be positive.")

    def to_dict(self) -> dict[str, object]:
        return {"type": "mass-flow-inlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class MeanVelocityInlet:
    velocity: float

    def __post_init__(self) -> None:
        if self.velocity <= 0.0:
            raise ValueError("Inlet velocity must be positive.")

    def to_dict(self) -> dict[str, object]:
        return {"type": "mean-velocity-inlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class PressureOutlet:
    gauge_pressure: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {"type": "pressure-outlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class NoSlipWall:
    roughness: float | None = None

    def __post_init__(self) -> None:
        if self.roughness is not None and self.roughness < 0.0:
            raise ValueError("Wall roughness cannot be negative.")

    def to_dict(self) -> dict[str, object]:
        return {"type": "no-slip-wall", **asdict(self)}


Boundary = MassFlowInlet | MeanVelocityInlet | PressureOutlet | NoSlipWall


def mass_flow_inlet(value: float) -> MassFlowInlet:
    return MassFlowInlet(mass_flow_rate=value)


def mean_velocity_inlet(value: float) -> MeanVelocityInlet:
    return MeanVelocityInlet(velocity=value)


def pressure_outlet(value: float = 0.0) -> PressureOutlet:
    return PressureOutlet(gauge_pressure=value)


def no_slip_wall(*, roughness: float | None = None) -> NoSlipWall:
    return NoSlipWall(roughness=roughness)
