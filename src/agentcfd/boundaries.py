"""Engineering boundary conditions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from ._validation import finite_float, nonnegative_float, positive_float


@dataclass(frozen=True, slots=True)
class MassFlowInlet:
    mass_flow_rate: float
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mass_flow_rate",
            positive_float(self.mass_flow_rate, name="Mass-flow rate"),
        )
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "mass-flow-inlet",
            "mass_flow_rate": self.mass_flow_rate,
        }
        if self.temperature is not None:
            record["temperature"] = self.temperature
        return record


@dataclass(frozen=True, slots=True)
class MeanVelocityInlet:
    velocity: float
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "velocity",
            positive_float(self.velocity, name="Inlet velocity"),
        )
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "mean-velocity-inlet",
            "velocity": self.velocity,
        }
        if self.temperature is not None:
            record["temperature"] = self.temperature
        return record


@dataclass(frozen=True, slots=True)
class VelocityInlet:
    """Explicit Cartesian inlet velocity vector in metres per second."""

    velocity: tuple[float, float, float]
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.velocity, (tuple, list)) or len(self.velocity) != 3:
            raise ValueError("Velocity inlet requires exactly three components.")
        selected = tuple(
            finite_float(value, name="Velocity inlet component")
            for value in self.velocity
        )
        if math.sqrt(sum(value * value for value in selected)) == 0.0:
            raise ValueError("Velocity inlet vector cannot be zero.")
        object.__setattr__(self, "velocity", selected)
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    @property
    def magnitude(self) -> float:
        return math.sqrt(sum(value * value for value in self.velocity))

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "velocity-inlet",
            "velocity": list(self.velocity),
        }
        if self.temperature is not None:
            record["temperature"] = self.temperature
        return record


@dataclass(frozen=True, slots=True)
class FullyDevelopedVelocityInlet:
    """Mean velocity for an analytic fully developed circular-pipe profile."""

    velocity: float
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "velocity",
            positive_float(self.velocity, name="Inlet velocity"),
        )
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "fully-developed-velocity-inlet",
            "velocity": self.velocity,
        }
        if self.temperature is not None:
            record["temperature"] = self.temperature
        return record


@dataclass(frozen=True, slots=True)
class TurbulentMeanVelocityInlet:
    """Bulk velocity plus explicit two-equation RANS inlet assumptions.

    ``turbulence_intensity`` is a fraction, not a percentage.  The length scale
    is an engineering model input in metres; it is never inferred by a provider.
    """

    velocity: float
    turbulence_intensity: float
    turbulence_length_scale: float
    temperature: float | None = None

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
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "turbulent-mean-velocity-inlet",
            "velocity": self.velocity,
            "turbulence_intensity": self.turbulence_intensity,
            "turbulence_length_scale": self.turbulence_length_scale,
        }
        if self.temperature is not None:
            record["temperature"] = self.temperature
        return record


@dataclass(frozen=True, slots=True)
class TurbulentVelocityInlet:
    """Cartesian RANS inlet velocity plus explicit turbulence assumptions."""

    velocity: tuple[float, float, float]
    turbulence_intensity: float
    turbulence_length_scale: float
    temperature: float | None = None

    def __post_init__(self) -> None:
        selected = VelocityInlet(self.velocity).velocity
        object.__setattr__(self, "velocity", selected)
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
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Inlet temperature"),
            )

    @property
    def magnitude(self) -> float:
        return math.sqrt(sum(value * value for value in self.velocity))

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "turbulent-velocity-inlet",
            "velocity": list(self.velocity),
            "turbulence_intensity": self.turbulence_intensity,
            "turbulence_length_scale": self.turbulence_length_scale,
            **(
                {"temperature": self.temperature}
                if self.temperature is not None
                else {}
            ),
        }


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
class AdiabaticWall:
    """Zero heat flux at a wall."""

    def to_dict(self) -> dict[str, object]:
        return {"type": "adiabatic"}


@dataclass(frozen=True, slots=True)
class FixedTemperatureWall:
    """Fixed absolute wall temperature in kelvin."""

    temperature: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "temperature",
            positive_float(self.temperature, name="Wall temperature"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "fixed-temperature", "temperature": self.temperature}


@dataclass(frozen=True, slots=True)
class HeatFluxWall:
    """Signed heat flux into the fluid in watts per square metre."""

    heat_flux_into_fluid: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "heat_flux_into_fluid",
            finite_float(
                self.heat_flux_into_fluid,
                name="Wall heat flux into fluid",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "heat-flux",
            "heat_flux_into_fluid": self.heat_flux_into_fluid,
        }


ThermalWall = AdiabaticWall | FixedTemperatureWall | HeatFluxWall


@dataclass(frozen=True, slots=True)
class NoSlipWall:
    roughness: float | None = None
    thermal: ThermalWall | None = None

    def __post_init__(self) -> None:
        if self.roughness is not None:
            object.__setattr__(
                self,
                "roughness",
                nonnegative_float(self.roughness, name="Wall roughness"),
            )
        if self.thermal is not None and not isinstance(
            self.thermal,
            (AdiabaticWall, FixedTemperatureWall, HeatFluxWall),
        ):
            raise TypeError("Wall thermal condition must be an AgentCFD thermal wall.")

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "no-slip-wall",
            "roughness": self.roughness,
        }
        if self.thermal is not None:
            record["thermal"] = self.thermal.to_dict()
        return record


@dataclass(frozen=True, slots=True)
class SlipWall:
    """Impermeable zero-shear wall."""

    thermal: ThermalWall | None = None

    def __post_init__(self) -> None:
        if self.thermal is not None and not isinstance(
            self.thermal,
            (AdiabaticWall, FixedTemperatureWall, HeatFluxWall),
        ):
            raise TypeError("Wall thermal condition must be an AgentCFD thermal wall.")

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {"type": "slip-wall"}
        if self.thermal is not None:
            record["thermal"] = self.thermal.to_dict()
        return record


@dataclass(frozen=True, slots=True)
class Symmetry:
    def to_dict(self) -> dict[str, object]:
        return {"type": "symmetry"}


Boundary = (
    MassFlowInlet
    | MeanVelocityInlet
    | VelocityInlet
    | FullyDevelopedVelocityInlet
    | TurbulentMeanVelocityInlet
    | TurbulentVelocityInlet
    | PressureInlet
    | PressureOutlet
    | MassFlowOutlet
    | NoSlipWall
    | SlipWall
    | Symmetry
)


def mass_flow_inlet(
    value: float, *, temperature: float | None = None
) -> MassFlowInlet:
    return MassFlowInlet(mass_flow_rate=value, temperature=temperature)


def mean_velocity_inlet(
    value: float, *, temperature: float | None = None
) -> MeanVelocityInlet:
    return MeanVelocityInlet(velocity=value, temperature=temperature)


def velocity_inlet(
    value: tuple[float, float, float], *, temperature: float | None = None
) -> VelocityInlet:
    return VelocityInlet(velocity=value, temperature=temperature)


def fully_developed_velocity_inlet(
    value: float, *, temperature: float | None = None
) -> FullyDevelopedVelocityInlet:
    return FullyDevelopedVelocityInlet(velocity=value, temperature=temperature)


def turbulent_mean_velocity_inlet(
    value: float,
    *,
    intensity: float,
    length_scale: float,
    temperature: float | None = None,
) -> TurbulentMeanVelocityInlet:
    """Declare a flow-rate-constrained RANS inlet using SI values."""

    return TurbulentMeanVelocityInlet(
        velocity=value,
        turbulence_intensity=intensity,
        turbulence_length_scale=length_scale,
        temperature=temperature,
    )


def turbulent_velocity_inlet(
    value: tuple[float, float, float],
    *,
    intensity: float,
    length_scale: float,
    temperature: float | None = None,
) -> TurbulentVelocityInlet:
    """Declare a Cartesian RANS inlet using explicit SI assumptions."""

    return TurbulentVelocityInlet(
        velocity=value,
        turbulence_intensity=intensity,
        turbulence_length_scale=length_scale,
        temperature=temperature,
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


def adiabatic() -> AdiabaticWall:
    return AdiabaticWall()


def fixed_temperature(value: float) -> FixedTemperatureWall:
    return FixedTemperatureWall(temperature=value)


def heat_flux_into_fluid(value: float) -> HeatFluxWall:
    return HeatFluxWall(heat_flux_into_fluid=value)


def no_slip_wall(
    *,
    roughness: float | None = None,
    thermal: ThermalWall | None = None,
) -> NoSlipWall:
    return NoSlipWall(roughness=roughness, thermal=thermal)


def slip_wall(*, thermal: ThermalWall | None = None) -> SlipWall:
    return SlipWall(thermal=thermal)


def symmetry() -> Symmetry:
    return Symmetry()


Inlet = (
    MassFlowInlet
    | MeanVelocityInlet
    | VelocityInlet
    | FullyDevelopedVelocityInlet
    | TurbulentMeanVelocityInlet
    | TurbulentVelocityInlet
    | PressureInlet
)
Outlet = PressureOutlet | MassFlowOutlet
Wall = NoSlipWall | SlipWall


__all__ = [
    "AdiabaticWall",
    "Boundary",
    "FixedTemperatureWall",
    "FullyDevelopedVelocityInlet",
    "HeatFluxWall",
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
    "ThermalWall",
    "TurbulentMeanVelocityInlet",
    "TurbulentVelocityInlet",
    "VelocityInlet",
    "Wall",
    "adiabatic",
    "fixed_temperature",
    "fully_developed_velocity_inlet",
    "heat_flux_into_fluid",
    "mass_flow_inlet",
    "mass_flow_outlet",
    "mean_velocity_inlet",
    "no_slip_wall",
    "pressure_inlet",
    "pressure_outlet",
    "slip_wall",
    "symmetry",
    "turbulent_mean_velocity_inlet",
    "turbulent_velocity_inlet",
    "velocity_inlet",
]
