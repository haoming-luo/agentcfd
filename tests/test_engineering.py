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


def test_ideal_gas_state_and_mach_screening_for_air():
    density = engineering.ideal_gas_density(
        absolute_pressure=101325.0,
        temperature=300.0,
        specific_gas_constant=287.05,
    )
    sound_speed = engineering.ideal_gas_speed_of_sound(
        temperature=300.0,
        specific_heat_ratio=1.4,
        specific_gas_constant=287.05,
    )

    assert density == pytest.approx(1.176624, rel=1.0e-6)
    assert sound_speed == pytest.approx(347.219, rel=1.0e-6)
    assert engineering.mach_number(velocity=100.0, speed_of_sound=sound_speed) == pytest.approx(
        0.2880027,
        rel=1.0e-6,
    )
    assert engineering.mach_number(velocity=0.0, speed_of_sound=sound_speed) == 0.0


def test_ideal_gas_speed_of_sound_requires_physical_heat_capacity_ratio():
    with pytest.raises(ValueError, match="greater than one"):
        engineering.ideal_gas_speed_of_sound(
            temperature=300.0,
            specific_heat_ratio=1.0,
            specific_gas_constant=287.05,
        )


def test_laminar_entrance_length_screen_is_explicit_and_bounded():
    assert engineering.laminar_hydrodynamic_entrance_length(
        reynolds=1000.0,
        hydraulic_diameter=0.1,
    ) == pytest.approx(5.0)
    assert engineering.laminar_hydrodynamic_entrance_length(
        reynolds=1000.0,
        hydraulic_diameter=0.1,
        coefficient=0.06,
    ) == pytest.approx(6.0)
    with pytest.raises(ValueError, match="Re < 2300"):
        engineering.laminar_hydrodynamic_entrance_length(
            reynolds=2300.0,
            hydraulic_diameter=0.1,
        )


def test_turbulent_pipe_wall_resolution_round_trips_target_y_plus():
    estimate = engineering.turbulent_pipe_wall_resolution(
        density=1000.0,
        dynamic_viscosity=1.0e-3,
        mean_velocity=1.0,
        hydraulic_diameter=0.1,
        target_y_plus=1.0,
    )

    assert estimate.reynolds_number == pytest.approx(100_000.0)
    assert estimate.wall_shear_stress == pytest.approx(
        estimate.darcy_friction_factor * 1000.0 / 8.0
    )
    assert engineering.friction_velocity(
        wall_shear_stress=estimate.wall_shear_stress,
        density=1000.0,
    ) == pytest.approx(estimate.friction_velocity)
    assert engineering.y_plus(
        wall_distance=estimate.first_cell_center_distance,
        friction_velocity=estimate.friction_velocity,
        density=1000.0,
        dynamic_viscosity=1.0e-3,
    ) == pytest.approx(1.0)
    assert estimate.nominal_first_cell_thickness == pytest.approx(
        2.0 * estimate.first_cell_center_distance
    )


def test_turbulent_wall_resolution_rejects_non_turbulent_regime():
    with pytest.raises(ValueError, match="Re >= 4000"):
        engineering.turbulent_pipe_wall_resolution(
            density=1000.0,
            dynamic_viscosity=1.0e-3,
            mean_velocity=0.03,
            hydraulic_diameter=0.1,
            target_y_plus=1.0,
        )


def test_turbulence_inlet_estimate_matches_two_equation_definitions():
    estimate = engineering.turbulence_inlet_from_intensity(
        mean_velocity=10.0,
        intensity=0.05,
        length_scale=0.1,
    )

    expected_k = 1.5 * (0.05 * 10.0) ** 2
    assert estimate.turbulent_kinetic_energy == pytest.approx(expected_k)
    assert estimate.specific_dissipation_rate == pytest.approx(
        math.sqrt(expected_k) / (0.09**0.25 * 0.1)
    )
    assert estimate.dissipation_rate == pytest.approx(
        0.09**0.75 * expected_k**1.5 / 0.1
    )
    assert estimate.to_dict()["intensity"] == 0.05

    with pytest.raises(ValueError, match="fraction below one"):
        engineering.turbulence_inlet_from_intensity(
            mean_velocity=10.0,
            intensity=5.0,
            length_scale=0.1,
        )


def test_pipe_operating_point_inverts_laminar_and_turbulent_losses():
    laminar = engineering.circular_pipe_operating_point(
        pressure_loss=0.064,
        density=1000.0,
        dynamic_viscosity=1.0e-3,
        length=2.0,
        diameter=0.1,
        regime="laminar",
    )
    assert laminar.mean_velocity == pytest.approx(0.01)
    assert laminar.reynolds_number == pytest.approx(1000.0)
    assert laminar.volume_flow_rate == pytest.approx(math.pi * 0.1**2 / 4.0 * 0.01)

    target = engineering.pipe_pressure_loss(
        density=1000.0,
        dynamic_viscosity=1.0e-3,
        mean_velocity=1.0,
        length=10.0,
        hydraulic_diameter=0.1,
        roughness=1.0e-4,
        loss_coefficient=1.5,
    ).total_pressure_loss
    turbulent = engineering.circular_pipe_operating_point(
        pressure_loss=target,
        density=1000.0,
        dynamic_viscosity=1.0e-3,
        length=10.0,
        diameter=0.1,
        regime="turbulent",
        roughness=1.0e-4,
        loss_coefficient=1.5,
    )
    assert turbulent.mean_velocity == pytest.approx(1.0, rel=1.0e-10)
    assert turbulent.pressure_loss == pytest.approx(target, rel=1.0e-10)


def test_pipe_operating_point_refuses_a_declared_regime_mismatch():
    with pytest.raises(ValueError, match="not declared 'laminar'"):
        engineering.circular_pipe_operating_point(
            pressure_loss=0.192,
            density=1000.0,
            dynamic_viscosity=1.0e-3,
            length=2.0,
            diameter=0.1,
            regime="laminar",
        )
    with pytest.raises(ValueError, match="no solution.*turbulent"):
        engineering.circular_pipe_operating_point(
            pressure_loss=0.01,
            density=1000.0,
            dynamic_viscosity=1.0e-3,
            length=2.0,
            diameter=0.1,
            regime="turbulent",
        )
