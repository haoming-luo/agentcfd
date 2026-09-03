"""Optional thermophysical-property providers."""

from __future__ import annotations

import importlib.util
import math
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Callable

from ._validation import positive_float
from .errors import ProviderUnavailableError
from .providers.base import ProviderDescriptor


@dataclass(frozen=True, slots=True)
class ThermophysicalState:
    """One pressure-temperature property evaluation in SI units."""

    fluid: str
    backend: str
    pressure: float
    temperature: float
    phase: str
    density: float
    dynamic_viscosity: float
    specific_heat: float
    thermal_conductivity: float
    speed_of_sound: float
    prandtl_number: float
    provider: str
    provider_version: str
    schema: str = "agentcfd.thermophysical-state"
    schema_version: str = "0.1.0"

    def __post_init__(self) -> None:
        for name in ("fluid", "backend", "phase", "provider", "provider_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Thermophysical state {name} must be a non-empty string.")
        if self.schema != "agentcfd.thermophysical-state" or self.schema_version != "0.1.0":
            raise ValueError("Unsupported thermophysical-state schema or version.")
        for name in (
            "pressure",
            "temperature",
            "density",
            "dynamic_viscosity",
            "specific_heat",
            "thermal_conductivity",
            "speed_of_sound",
            "prandtl_number",
        ):
            object.__setattr__(
                self,
                name,
                positive_float(getattr(self, name), name=f"Thermophysical state {name}"),
            )

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def _coolprop_api() -> tuple[Callable[..., float], Callable[..., str]]:
    try:
        from CoolProp.CoolProp import PhaseSI, PropsSI
    except ImportError as error:
        raise ProviderUnavailableError(
            "CoolProp properties require the optional 'agentcfd[properties]' extra."
        ) from error
    return PropsSI, PhaseSI


class CoolPropPropertyProvider:
    """Evaluate single-phase properties through optional MIT-licensed CoolProp."""

    def descriptor(self) -> ProviderDescriptor:
        available = importlib.util.find_spec("CoolProp") is not None
        try:
            selected_version = version("CoolProp") if available else "not-installed"
        except PackageNotFoundError:
            selected_version = "not-installed"
        return ProviderDescriptor(
            name="coolprop-properties",
            version=selected_version,
            license="MIT",
            available=available,
            execution_boundary="optional-in-process-library",
            capabilities=("properties.pressure-temperature-state",),
        )

    def at_pressure_temperature(
        self,
        fluid: str,
        *,
        pressure: float,
        temperature: float,
    ) -> ThermophysicalState:
        """Evaluate a fluid state from absolute pressure in Pa and temperature in K."""

        if not isinstance(fluid, str) or not fluid.strip():
            raise ValueError("fluid must be a non-empty string.")
        selected_fluid = fluid.strip()
        selected_pressure = positive_float(pressure, name="Absolute pressure")
        selected_temperature = positive_float(temperature, name="Temperature")
        props_si, phase_si = _coolprop_api()

        def evaluate(output: str, label: str) -> float:
            try:
                value = float(
                    props_si(
                        output,
                        "P",
                        selected_pressure,
                        "T",
                        selected_temperature,
                        selected_fluid,
                    )
                )
            except Exception as error:
                raise ValueError(
                    f"CoolProp could not evaluate {label} for {selected_fluid!r} "
                    f"at P={selected_pressure:g} Pa and T={selected_temperature:g} K."
                ) from error
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"CoolProp returned an invalid {label}: {value!r}.")
            return value

        try:
            phase = str(
                phase_si(
                    "P",
                    selected_pressure,
                    "T",
                    selected_temperature,
                    selected_fluid,
                )
            )
        except Exception as error:
            raise ValueError(
                f"CoolProp could not identify the phase for {selected_fluid!r}."
            ) from error

        descriptor = self.descriptor()
        backend = selected_fluid.split("::", maxsplit=1)[0] if "::" in selected_fluid else "default"
        return ThermophysicalState(
            fluid=selected_fluid,
            backend=backend,
            pressure=selected_pressure,
            temperature=selected_temperature,
            phase=phase,
            density=evaluate("D", "density"),
            dynamic_viscosity=evaluate("V", "dynamic viscosity"),
            specific_heat=evaluate("C", "specific heat"),
            thermal_conductivity=evaluate("L", "thermal conductivity"),
            speed_of_sound=evaluate("A", "speed of sound"),
            prandtl_number=evaluate("Prandtl", "Prandtl number"),
            provider=descriptor.name,
            provider_version=descriptor.version,
        )


__all__ = ["CoolPropPropertyProvider", "ThermophysicalState"]
