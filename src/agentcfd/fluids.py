"""Fluid material definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass


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
        if self.density <= 0.0:
            raise ValueError("Density must be positive.")
        if self.dynamic_viscosity <= 0.0:
            raise ValueError("Dynamic viscosity must be positive.")
        if self.specific_heat is not None and self.specific_heat <= 0.0:
            raise ValueError("Specific heat must be positive.")
        if self.thermal_conductivity is not None and self.thermal_conductivity <= 0.0:
            raise ValueError("Thermal conductivity must be positive.")

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
