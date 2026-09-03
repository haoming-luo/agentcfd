import math

import pytest

from agentcfd import engineering


def test_hydraulic_diameter_and_reynolds_number_for_circular_pipe():
    diameter = 0.1
    area = math.pi * diameter**2 / 4.0
    perimeter = math.pi * diameter

    assert engineering.hydraulic_diameter(area, perimeter) == pytest.approx(diameter)
    assert engineering.reynolds_number(
        density=1000.0,
        mean_velocity=0.01,
        hydraulic_diameter=diameter,
        dynamic_viscosity=0.001,
    ) == pytest.approx(1000.0)


def test_laminar_darcy_weisbach_matches_hagen_poiseuille():
    reynolds = 1000.0
    factor = engineering.darcy_friction_factor(reynolds)
    loss = engineering.darcy_weisbach_pressure_loss(
        friction_factor=factor,
        length=2.0,
        hydraulic_diameter=0.1,
        density=1000.0,
        mean_velocity=0.01,
    )

    assert factor == pytest.approx(0.064)
    assert loss == pytest.approx(0.064)


def test_turbulent_friction_factor_satisfies_colebrook_white():
    reynolds = 100_000.0
    relative_roughness = 1.0e-3
    factor = engineering.darcy_friction_factor(
        reynolds,
        relative_roughness=relative_roughness,
    )
    residual = 1.0 / math.sqrt(factor) + 2.0 * math.log10(
        relative_roughness / 3.7 + 2.51 / (reynolds * math.sqrt(factor))
    )

    assert factor == pytest.approx(0.0221745359, rel=1.0e-8)
    assert residual == pytest.approx(0.0, abs=1.0e-9)


def test_transitional_friction_factor_fails_closed():
    with pytest.raises(ValueError, match="transitional"):
        engineering.darcy_friction_factor(3000.0)


def test_minor_loss_uses_dynamic_pressure():
    assert engineering.minor_pressure_loss(
        loss_coefficient=2.0,
        density=1000.0,
        mean_velocity=3.0,
    ) == pytest.approx(9000.0)


def test_pipe_pressure_loss_combines_major_and_minor_losses():
    estimate = engineering.pipe_pressure_loss(
        density=1000.0,
        dynamic_viscosity=1.0e-3,
        mean_velocity=0.01,
        length=2.0,
        hydraulic_diameter=0.1,
        loss_coefficient=2.0,
    )

    assert estimate.regime == "laminar"
    assert estimate.reynolds_number == pytest.approx(1000.0)
    assert estimate.major_pressure_loss == pytest.approx(0.064)
    assert estimate.minor_pressure_loss == pytest.approx(0.1)
    assert estimate.total_pressure_loss == pytest.approx(0.164)
    assert estimate.to_dict()["darcy_friction_factor"] == pytest.approx(0.064)


def test_pipe_pressure_loss_reports_turbulent_rough_pipe_regime():
    estimate = engineering.pipe_pressure_loss(
        density=1000.0,
        dynamic_viscosity=1.0e-3,
        mean_velocity=1.0,
        length=10.0,
        hydraulic_diameter=0.1,
        roughness=1.0e-4,
    )

    assert estimate.regime == "turbulent"
    assert estimate.relative_roughness == pytest.approx(1.0e-3)
    assert estimate.total_pressure_loss == pytest.approx(1108.726795, rel=1.0e-6)
