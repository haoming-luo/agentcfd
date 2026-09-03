import math

import pytest

from agentcfd import Model, boundaries, fluids, geometry, studies
from agentcfd.errors import ModelValidationError, UnsupportedCaseError


def pipe_model(*, velocity: float = 0.02) -> Model:
    return Model(
        name="test-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=10.0, diameter=0.05),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(velocity),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def test_laminar_pipe_is_accepted_and_matches_closed_form():
    model = pipe_model()
    result = model.step().run()

    expected = 32.0 * 1.002e-3 * 10.0 * 0.02 / 0.05**2
    assert result.accepted
    assert result.quantities["flow.pressure_drop"].value == pytest.approx(expected)
    assert result.quantities["flow.darcy_friction_factor"].value == pytest.approx(
        64.0 / result.quantities["flow.reynolds_number"].value
    )
    assert max(result.arrays["profile.axial_velocity"]) == pytest.approx(0.04)
    assert result.arrays["profile.axial_velocity"][-1] == pytest.approx(0.0)
    assert len(result.provenance["model_sha256"]) == 64


def test_mass_flow_and_velocity_inlets_are_equivalent():
    velocity_model = pipe_model()
    mass_flow = velocity_model.fluid.density * velocity_model.domain.area * 0.02
    mass_model = Model(
        study=studies.internal_flow(),
        domain=velocity_model.domain,
        fluid=velocity_model.fluid,
    ).boundaries(
        inlet=boundaries.mass_flow_inlet(mass_flow),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    assert mass_model.step().run().quantities["flow.pressure_drop"].value == pytest.approx(
        velocity_model.step().run().quantities["flow.pressure_drop"].value
    )


def test_reference_provider_fails_closed_outside_laminar_scope():
    with pytest.raises(UnsupportedCaseError, match="laminar reference range"):
        pipe_model(velocity=0.1).step().run()


def test_model_requires_engineering_boundaries():
    model = Model(
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=1.0, diameter=0.1),
        fluid=fluids.newtonian("test", density=1.0, dynamic_viscosity=1.0),
    )
    with pytest.raises(ModelValidationError, match="Exactly one"):
        model.step().run()


def test_pipe_area():
    domain = geometry.circular_pipe(length=1.0, diameter=0.2)
    assert domain.area == pytest.approx(math.pi * 0.01)
