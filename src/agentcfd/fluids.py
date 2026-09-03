"""Fluid material definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ._validation import positive_float


@dataclass(frozen=True, slots=True)
class NewtonianFluid:
    name: str
    density: float
    dynamic_viscosity: float
    specific_heat: float | None = None
    thermal_conductivity: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Fluid name cannot be empty.")
        object.__setattr__(
            self,
            "density",
            positive_float(self.density, name="Density"),
        )
        object.__setattr__(
            self,
            "dynamic_viscosity",
            positive_float(self.dynamic_viscosity, name="Dynamic viscosity"),
        )
        if self.specific_heat is not None:
            object.__setattr__(
                self,
                "specific_heat",
                positive_float(self.specific_heat, name="Specific heat"),
            )
        if self.thermal_conductivity is not None:
            object.__setattr__(
                self,
                "thermal_conductivity",
                positive_float(
                    self.thermal_conductivity,
                    name="Thermal conductivity",
                ),
            )

    @property
    def kinematic_viscosity(self) -> float:
        return self.dynamic_viscosity / self.density

    def to_dict(self) -> dict[str, object]:
        return {"type": "newtonian", **asdict(self)}


def newtonian(
    name: str,
    *,
    density: float,
    dynamic_viscosity: float,
    specific_heat: float | None = None,
    thermal_conductivity: float | None = None,
) -> NewtonianFluid:
    """Define a constant-property Newtonian fluid in SI units."""

    return NewtonianFluid(
        name=name,
        density=density,
        dynamic_viscosity=dynamic_viscosity,
        specific_heat=specific_heat,
        thermal_conductivity=thermal_conductivity,
    )
