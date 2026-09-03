"""Engineering boundary conditions."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ._validation import finite_float, nonnegative_float, positive_float


@dataclass(frozen=True, slots=True)
class MassFlowInlet:
    mass_flow_rate: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mass_flow_rate",
            positive_float(self.mass_flow_rate, name="Mass-flow rate"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "mass-flow-inlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class MeanVelocityInlet:
    velocity: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "velocity",
            positive_float(self.velocity, name="Inlet velocity"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "mean-velocity-inlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class FullyDevelopedVelocityInlet:
    """Mean velocity for an analytic fully developed circular-pipe profile."""

    velocity: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "velocity",
            positive_float(self.velocity, name="Inlet velocity"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "fully-developed-velocity-inlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class PressureOutlet:
    gauge_pressure: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gauge_pressure",
            finite_float(self.gauge_pressure, name="Outlet gauge pressure"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "pressure-outlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class NoSlipWall:
    roughness: float | None = None

    def __post_init__(self) -> None:
        if self.roughness is not None:
            object.__setattr__(
                self,
                "roughness",
                nonnegative_float(self.roughness, name="Wall roughness"),
            )

    def to_dict(self) -> dict[str, object]:
        return {"type": "no-slip-wall", **asdict(self)}


Boundary = (
    MassFlowInlet
    | MeanVelocityInlet
    | FullyDevelopedVelocityInlet
    | PressureOutlet
    | NoSlipWall
)


def mass_flow_inlet(value: float) -> MassFlowInlet:
    return MassFlowInlet(mass_flow_rate=value)


def mean_velocity_inlet(value: float) -> MeanVelocityInlet:
    return MeanVelocityInlet(velocity=value)


def fully_developed_velocity_inlet(value: float) -> FullyDevelopedVelocityInlet:
    return FullyDevelopedVelocityInlet(velocity=value)


def pressure_outlet(value: float = 0.0) -> PressureOutlet:
    return PressureOutlet(gauge_pressure=value)


def no_slip_wall(*, roughness: float | None = None) -> NoSlipWall:
    return NoSlipWall(roughness=roughness)
