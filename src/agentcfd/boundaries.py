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
class TurbulentMeanVelocityInlet:
    """Bulk velocity plus explicit two-equation RANS inlet assumptions.

    ``turbulence_intensity`` is a fraction, not a percentage.  The length scale
    is an engineering model input in metres; it is never inferred by a provider.
    """

    velocity: float
    turbulence_intensity: float
    turbulence_length_scale: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "velocity",
            positive_float(self.velocity, name="Inlet velocity"),
        )
        intensity = positive_float(
            self.turbulence_intensity,
            name="Turbulence intensity",
        )
        if intensity >= 1.0:
            raise ValueError("Turbulence intensity must be a fraction below one.")
        object.__setattr__(self, "turbulence_intensity", intensity)
        object.__setattr__(
            self,
            "turbulence_length_scale",
            positive_float(
                self.turbulence_length_scale,
                name="Turbulence length scale",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "turbulent-mean-velocity-inlet", **asdict(self)}


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
class PressureInlet:
    """Total gauge pressure at an inlet, with optional thermodynamic state."""

    total_gauge_pressure: float
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total_gauge_pressure",
            finite_float(self.total_gauge_pressure, name="Inlet total gauge pressure"),
        )
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    def to_dict(self) -> dict[str, object]:
        return {"type": "pressure-inlet", **asdict(self)}


@dataclass(frozen=True, slots=True)
class MassFlowOutlet:
    mass_flow_rate: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mass_flow_rate",
            positive_float(self.mass_flow_rate, name="Outlet mass-flow rate"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "mass-flow-outlet", **asdict(self)}


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


@dataclass(frozen=True, slots=True)
class SlipWall:
    """Impermeable zero-shear wall."""

    def to_dict(self) -> dict[str, object]:
        return {"type": "slip-wall"}


@dataclass(frozen=True, slots=True)
class Symmetry:
    def to_dict(self) -> dict[str, object]:
        return {"type": "symmetry"}


Boundary = (
    MassFlowInlet
    | MeanVelocityInlet
    | FullyDevelopedVelocityInlet
    | TurbulentMeanVelocityInlet
    | PressureInlet
    | PressureOutlet
    | MassFlowOutlet
    | NoSlipWall
    | SlipWall
    | Symmetry
)


def mass_flow_inlet(value: float) -> MassFlowInlet:
    return MassFlowInlet(mass_flow_rate=value)


def mean_velocity_inlet(value: float) -> MeanVelocityInlet:
    return MeanVelocityInlet(velocity=value)


def fully_developed_velocity_inlet(value: float) -> FullyDevelopedVelocityInlet:
    return FullyDevelopedVelocityInlet(velocity=value)


def turbulent_mean_velocity_inlet(
    value: float,
    *,
    intensity: float,
    length_scale: float,
) -> TurbulentMeanVelocityInlet:
    """Declare a flow-rate-constrained RANS inlet using SI values."""

    return TurbulentMeanVelocityInlet(
        velocity=value,
        turbulence_intensity=intensity,
        turbulence_length_scale=length_scale,
    )


def pressure_outlet(value: float = 0.0) -> PressureOutlet:
    return PressureOutlet(gauge_pressure=value)


def pressure_inlet(
    value: float,
    *,
    temperature: float | None = None,
) -> PressureInlet:
    return PressureInlet(total_gauge_pressure=value, temperature=temperature)


def mass_flow_outlet(value: float) -> MassFlowOutlet:
    return MassFlowOutlet(mass_flow_rate=value)


def no_slip_wall(*, roughness: float | None = None) -> NoSlipWall:
    return NoSlipWall(roughness=roughness)


def slip_wall() -> SlipWall:
    return SlipWall()


def symmetry() -> Symmetry:
    return Symmetry()


Inlet = (
    MassFlowInlet
    | MeanVelocityInlet
    | FullyDevelopedVelocityInlet
    | TurbulentMeanVelocityInlet
    | PressureInlet
)
Outlet = PressureOutlet | MassFlowOutlet
Wall = NoSlipWall | SlipWall


__all__ = [
    "Boundary",
    "FullyDevelopedVelocityInlet",
    "Inlet",
    "MassFlowInlet",
    "MassFlowOutlet",
    "MeanVelocityInlet",
    "NoSlipWall",
    "Outlet",
    "PressureInlet",
    "PressureOutlet",
    "SlipWall",
    "Symmetry",
    "TurbulentMeanVelocityInlet",
    "Wall",
    "fully_developed_velocity_inlet",
    "mass_flow_inlet",
    "mass_flow_outlet",
    "mean_velocity_inlet",
    "no_slip_wall",
    "pressure_inlet",
    "pressure_outlet",
    "slip_wall",
    "symmetry",
    "turbulent_mean_velocity_inlet",
]
