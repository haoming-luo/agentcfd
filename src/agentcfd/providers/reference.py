"""Deterministic reference solutions used to anchor validation."""

from __future__ import annotations

from .. import boundaries, engineering
from .._version import __version__
from ..errors import UnsupportedCaseError
from ..results import Check, History, Quantity, SimulationResult
from .base import ProviderDescriptor


class ReferencePipeProvider:
    """Hagen--Poiseuille solution for a fully developed circular pipe."""

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            name="reference-pipe",
            version=__version__,
            license="Apache-2.0",
            available=True,
            execution_boundary="in-process",
            capabilities=("reference.hagen-poiseuille",),
        )

    def run(self, step) -> SimulationResult:
        model = step.model
        study = model.study
        if not study.steady or study.compressible or study.energy or study.reacting or not study.laminar:
            raise UnsupportedCaseError(
                "The reference provider supports steady, incompressible, isothermal laminar internal flow only."
            )

        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                ),
            )
        )
        diameter = model.domain.diameter
        length = model.domain.length
        area = model.domain.area
        rho = model.fluid.density
        mu = model.fluid.dynamic_viscosity

        if isinstance(inlet, boundaries.MassFlowInlet):
            mass_flow = inlet.mass_flow_rate
            mean_velocity = mass_flow / (rho * area)
        else:
            mean_velocity = inlet.velocity
            mass_flow = rho * area * mean_velocity

        volume_flow = mean_velocity * area
        reynolds = engineering.reynolds_number(
            density=rho,
            mean_velocity=mean_velocity,
            hydraulic_diameter=diameter,
            dynamic_viscosity=mu,
        )
        if reynolds >= 2300.0:
            raise UnsupportedCaseError(
                f"Re={reynolds:.6g} is outside the declared laminar reference range Re < 2300."
            )

        loss = engineering.pipe_pressure_loss(
            density=rho,
            dynamic_viscosity=mu,
            mean_velocity=mean_velocity,
            hydraulic_diameter=diameter,
            length=length,
            roughness=model.domain.roughness,
        )
        darcy_friction = loss.darcy_friction_factor
        pressure_drop = loss.total_pressure_loss
        wall_shear = pressure_drop * diameter / (4.0 * length)
        radius = tuple(diameter * index / 80.0 for index in range(41))
        axial_velocity = tuple(
            2.0 * mean_velocity * (1.0 - (2.0 * position / diameter) ** 2)
            for position in radius
        )

        reconstructed = pressure_drop / (0.5 * rho * mean_velocity**2)
        expected = darcy_friction * length / diameter
        identity_error = abs(reconstructed - expected) / max(abs(expected), 1.0e-30)

        return SimulationResult(
            status="completed",
            converged=True,
            provider="reference-pipe",
            quantities={
                "flow.reynolds_number": Quantity(reynolds, "1"),
                "flow.mean_velocity": Quantity(mean_velocity, "m/s"),
                "flow.mass_flow_rate": Quantity(mass_flow, "kg/s"),
                "flow.volumetric_flow_rate": Quantity(volume_flow, "m^3/s"),
                "flow.pressure_drop": Quantity(pressure_drop, "Pa"),
                "flow.darcy_friction_factor": Quantity(darcy_friction, "1"),
                "wall.shear_stress": Quantity(wall_shear, "Pa"),
            },
            checks=(
                Check(
                    name="laminar-applicability",
                    passed=reynolds < 2300.0,
                    value=reynolds,
                    limit="Re < 2300",
                    kind="runtime",
                    observable="flow.reynolds_number",
                ),
                Check(
                    name="darcy-weisbach-identity",
                    passed=identity_error < 1.0e-12,
                    value=identity_error,
                    limit=1.0e-12,
                    kind="verification",
                    observable="flow.pressure_drop",
                ),
                Check(
                    name="mass-balance",
                    passed=True,
                    value=0.0,
                    limit=1.0e-12,
                    message="Fully developed reference flow has identical inlet and outlet mass flow.",
                    kind="verification",
                    observable="flow.mass_balance",
                ),
            ),
            arrays={
                "profile.radius": list(radius),
                "profile.axial_velocity": list(axial_velocity),
            },
            histories={
                "profile.axial_velocity": History(
                    abscissa=tuple(radius),
                    values=tuple(axial_velocity),
                    unit="m/s",
                    abscissa_name="radius",
                    abscissa_unit="m",
                    description="Fully developed axial-velocity profile from the pipe centreline to the wall.",
                ),
            },
            scientific_inputs={
                "model": model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
            },
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": model.fingerprint(),
                "provider": self.descriptor().name,
                "formulation": "Hagen-Poiseuille circular-pipe solution",
            },
        )
