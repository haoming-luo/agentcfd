"""Small, dependency-free engineering calculations for CFD setup and checks."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def _positive(value: float, name: str) -> float:
    selected = float(value)
    if not math.isfinite(selected) or selected <= 0.0:
        raise ValueError(f"{name} must be positive and finite.")
    return selected


def _nonnegative(value: float, name: str) -> float:
    selected = float(value)
    if not math.isfinite(selected) or selected < 0.0:
        raise ValueError(f"{name} must be non-negative and finite.")
    return selected


def hydraulic_diameter(area: float, wetted_perimeter: float) -> float:
    """Return ``4A/P`` for a fully wetted duct cross-section."""

    return 4.0 * _positive(area, "area") / _positive(
        wetted_perimeter,
        "wetted_perimeter",
    )


def reynolds_number(
    *,
    density: float,
    mean_velocity: float,
    hydraulic_diameter: float,
    dynamic_viscosity: float,
) -> float:
    """Return the bulk Reynolds number using SI-compatible inputs."""

    return (
        _positive(density, "density")
        * _positive(mean_velocity, "mean_velocity")
        * _positive(hydraulic_diameter, "hydraulic_diameter")
        / _positive(dynamic_viscosity, "dynamic_viscosity")
    )


def darcy_friction_factor(
    reynolds: float,
    *,
    relative_roughness: float = 0.0,
    tolerance: float = 1.0e-12,
) -> float:
    """Return the Darcy friction factor.

    Laminar flow uses ``64/Re``. Turbulent flow solves the implicit
    Colebrook--White equation by a bracketed iteration. The transitional range
    is rejected because a unique correlation would hide engineering judgment.
    """

    reynolds = _positive(reynolds, "reynolds")
    relative_roughness = float(relative_roughness)
    if not math.isfinite(relative_roughness) or relative_roughness < 0.0:
        raise ValueError("relative_roughness must be non-negative and finite.")
    tolerance = _positive(tolerance, "tolerance")
    if reynolds < 2300.0:
        return 64.0 / reynolds
    if reynolds < 4000.0:
        raise ValueError(
            "Reynolds numbers from 2300 through 4000 are transitional; "
            "select a regime-specific model explicitly."
        )

    def residual(factor: float) -> float:
        root = math.sqrt(factor)
        return 1.0 / root + 2.0 * math.log10(
            relative_roughness / 3.7 + 2.51 / (reynolds * root)
        )

    lower, upper = 1.0e-4, 1.0
    if residual(lower) * residual(upper) > 0.0:
        raise ValueError("Colebrook--White root is outside the physical bracket.")
    for _ in range(200):
        middle = 0.5 * (lower + upper)
        if residual(lower) * residual(middle) <= 0.0:
            upper = middle
        else:
            lower = middle
        if upper - lower <= tolerance * max(middle, 1.0):
            break
    return 0.5 * (lower + upper)


def darcy_weisbach_pressure_loss(
    *,
    friction_factor: float,
    length: float,
    hydraulic_diameter: float,
    density: float,
    mean_velocity: float,
) -> float:
    """Return straight-run friction pressure loss in Pa for SI inputs."""

    factor = _positive(friction_factor, "friction_factor")
    return (
        factor
        * _positive(length, "length")
        / _positive(hydraulic_diameter, "hydraulic_diameter")
        * 0.5
        * _positive(density, "density")
        * _positive(mean_velocity, "mean_velocity") ** 2
    )


def minor_pressure_loss(
    *,
    loss_coefficient: float,
    density: float,
    mean_velocity: float,
) -> float:
    """Return a fitting/component pressure loss ``K rho U²/2`` in Pa."""

    coefficient = float(loss_coefficient)
    if not math.isfinite(coefficient) or coefficient < 0.0:
        raise ValueError("loss_coefficient must be non-negative and finite.")
    return (
        coefficient
        * 0.5
        * _positive(density, "density")
        * _positive(mean_velocity, "mean_velocity") ** 2
    )


@dataclass(frozen=True, slots=True)
class PipeLossEstimate:
    """Auditable straight-pipe and fitting loss estimate."""

    reynolds_number: float
    regime: str
    relative_roughness: float
    darcy_friction_factor: float
    major_pressure_loss: float
    minor_pressure_loss: float
    total_pressure_loss: float

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def pipe_pressure_loss(
    *,
    density: float,
    dynamic_viscosity: float,
    mean_velocity: float,
    length: float,
    hydraulic_diameter: float,
    roughness: float = 0.0,
    loss_coefficient: float = 0.0,
) -> PipeLossEstimate:
    """Return a complete incompressible pipe-loss screening calculation.

    The function combines the bulk Reynolds number, Darcy friction factor,
    distributed loss, and an optional sum of local loss coefficients. The
    transitional regime remains an explicit error through
    :func:`darcy_friction_factor`.
    """

    diameter = _positive(hydraulic_diameter, "hydraulic_diameter")
    selected_roughness = float(roughness)
    if not math.isfinite(selected_roughness) or selected_roughness < 0.0:
        raise ValueError("roughness must be non-negative and finite.")
    reynolds = reynolds_number(
        density=density,
        mean_velocity=mean_velocity,
        hydraulic_diameter=diameter,
        dynamic_viscosity=dynamic_viscosity,
    )
    relative_roughness = selected_roughness / diameter
    friction = darcy_friction_factor(
        reynolds,
        relative_roughness=relative_roughness,
    )
    major = darcy_weisbach_pressure_loss(
        friction_factor=friction,
        length=length,
        hydraulic_diameter=diameter,
        density=density,
        mean_velocity=mean_velocity,
    )
    minor = minor_pressure_loss(
        loss_coefficient=loss_coefficient,
        density=density,
        mean_velocity=mean_velocity,
    )
    return PipeLossEstimate(
        reynolds_number=reynolds,
        regime="laminar" if reynolds < 2300.0 else "turbulent",
        relative_roughness=relative_roughness,
        darcy_friction_factor=friction,
        major_pressure_loss=major,
        minor_pressure_loss=minor,
        total_pressure_loss=major + minor,
    )


def ideal_gas_density(
    *,
    absolute_pressure: float,
    temperature: float,
    specific_gas_constant: float,
) -> float:
    """Return ideal-gas density ``rho = p/(R T)`` in SI-compatible units."""

    return _positive(absolute_pressure, "absolute_pressure") / (
        _positive(specific_gas_constant, "specific_gas_constant")
        * _positive(temperature, "temperature")
    )


def ideal_gas_speed_of_sound(
    *,
    temperature: float,
    specific_heat_ratio: float,
    specific_gas_constant: float,
) -> float:
    """Return calorically perfect-gas sound speed ``sqrt(gamma R T)``."""

    gamma = _positive(specific_heat_ratio, "specific_heat_ratio")
    if gamma <= 1.0:
        raise ValueError("specific_heat_ratio must be greater than one.")
    return math.sqrt(
        gamma
        * _positive(specific_gas_constant, "specific_gas_constant")
        * _positive(temperature, "temperature")
    )


def mach_number(*, velocity: float, speed_of_sound: float) -> float:
    """Return flow speed divided by local thermodynamic speed of sound."""

    return _nonnegative(velocity, "velocity") / _positive(
        speed_of_sound,
        "speed_of_sound",
    )


def laminar_hydrodynamic_entrance_length(
    *,
    reynolds: float,
    hydraulic_diameter: float,
    coefficient: float = 0.05,
) -> float:
    """Estimate uniform-inlet laminar development length as ``C Re D_h``.

    This is a screening correlation, not an exact boundary between developing
    and fully developed flow. The coefficient is explicit because definitions
    in the literature commonly vary from about 0.05 to 0.06.
    """

    selected_reynolds = _positive(reynolds, "reynolds")
    if selected_reynolds >= 2300.0:
        raise ValueError("Laminar entrance-length screening requires Re < 2300.")
    return (
        _positive(coefficient, "coefficient")
        * selected_reynolds
        * _positive(hydraulic_diameter, "hydraulic_diameter")
    )


def friction_velocity(*, wall_shear_stress: float, density: float) -> float:
    """Return wall friction velocity ``sqrt(tau_w / rho)``."""

    return math.sqrt(
        _positive(wall_shear_stress, "wall_shear_stress")
        / _positive(density, "density")
    )


def y_plus(
    *,
    wall_distance: float,
    friction_velocity: float,
    density: float,
    dynamic_viscosity: float,
) -> float:
    """Return the first-cell-centre wall coordinate ``y u_tau / nu``."""

    return (
        _positive(wall_distance, "wall_distance")
        * _positive(friction_velocity, "friction_velocity")
        * _positive(density, "density")
        / _positive(dynamic_viscosity, "dynamic_viscosity")
    )


def wall_distance_for_y_plus(
    *,
    target_y_plus: float,
    friction_velocity: float,
    density: float,
    dynamic_viscosity: float,
) -> float:
    """Invert the wall-coordinate definition for cell-centre distance."""

    return (
        _positive(target_y_plus, "target_y_plus")
        * _positive(dynamic_viscosity, "dynamic_viscosity")
        / (
            _positive(density, "density")
            * _positive(friction_velocity, "friction_velocity")
        )
    )


@dataclass(frozen=True, slots=True)
class WallResolutionEstimate:
    """Auditable turbulent-pipe near-wall mesh screening estimate."""

    reynolds_number: float
    darcy_friction_factor: float
    wall_shear_stress: float
    friction_velocity: float
    target_y_plus: float
    first_cell_center_distance: float
    nominal_first_cell_thickness: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def turbulent_pipe_wall_resolution(
    *,
    density: float,
    dynamic_viscosity: float,
    mean_velocity: float,
    hydraulic_diameter: float,
    target_y_plus: float,
    roughness: float = 0.0,
) -> WallResolutionEstimate:
    """Estimate turbulent-pipe first-cell distance from a target ``y+``.

    Bulk Darcy friction provides a screening wall stress. The nominal cell
    thickness assumes its centroid lies halfway between two radial faces; the
    achieved local ``y+`` must still be checked from the solved field.
    """

    diameter = _positive(hydraulic_diameter, "hydraulic_diameter")
    selected_density = _positive(density, "density")
    selected_velocity = _positive(mean_velocity, "mean_velocity")
    selected_roughness = _nonnegative(roughness, "roughness")
    reynolds = reynolds_number(
        density=selected_density,
        mean_velocity=selected_velocity,
        hydraulic_diameter=diameter,
        dynamic_viscosity=dynamic_viscosity,
    )
    if reynolds < 4000.0:
        raise ValueError("Turbulent wall-resolution screening requires Re >= 4000.")
    friction = darcy_friction_factor(
        reynolds,
        relative_roughness=selected_roughness / diameter,
    )
    wall_shear = friction * selected_density * selected_velocity**2 / 8.0
    friction_speed = friction_velocity(
        wall_shear_stress=wall_shear,
        density=selected_density,
    )
    distance = wall_distance_for_y_plus(
        target_y_plus=target_y_plus,
        friction_velocity=friction_speed,
        density=selected_density,
        dynamic_viscosity=dynamic_viscosity,
    )
    return WallResolutionEstimate(
        reynolds_number=reynolds,
        darcy_friction_factor=friction,
        wall_shear_stress=wall_shear,
        friction_velocity=friction_speed,
        target_y_plus=_positive(target_y_plus, "target_y_plus"),
        first_cell_center_distance=distance,
        nominal_first_cell_thickness=2.0 * distance,
    )


@dataclass(frozen=True, slots=True)
class TurbulenceInletEstimate:
    """Two-equation RANS inlet scalars derived from intensity and length scale."""

    intensity: float
    length_scale: float
    c_mu: float
    turbulent_kinetic_energy: float
    specific_dissipation_rate: float
    dissipation_rate: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def turbulence_inlet_from_intensity(
    *,
    mean_velocity: float,
    intensity: float,
    length_scale: float,
    c_mu: float = 0.09,
) -> TurbulenceInletEstimate:
    """Return ``k``, ``omega``, and ``epsilon`` inlet screening values.

    Intensity is a fraction, not a percentage. The returned values initialize
    common two-equation RANS models but do not choose a turbulence model or
    hide the engineering choice of length scale.
    """

    velocity = _positive(mean_velocity, "mean_velocity")
    selected_intensity = _positive(intensity, "intensity")
    if selected_intensity >= 1.0:
        raise ValueError("intensity must be a fraction below one.")
    selected_length = _positive(length_scale, "length_scale")
    selected_c_mu = _positive(c_mu, "c_mu")
    if selected_c_mu >= 1.0:
        raise ValueError("c_mu must be below one.")
    kinetic_energy = 1.5 * (selected_intensity * velocity) ** 2
    omega = math.sqrt(kinetic_energy) / (selected_c_mu**0.25 * selected_length)
    epsilon = selected_c_mu**0.75 * kinetic_energy**1.5 / selected_length
    return TurbulenceInletEstimate(
        intensity=selected_intensity,
        length_scale=selected_length,
        c_mu=selected_c_mu,
        turbulent_kinetic_energy=kinetic_energy,
        specific_dissipation_rate=omega,
        dissipation_rate=epsilon,
    )


@dataclass(frozen=True, slots=True)
class PipeOperatingPoint:
    """Pressure-driven circular-pipe screening operating point."""

    regime: str
    pressure_loss: float
    mean_velocity: float
    volume_flow_rate: float
    reynolds_number: float
    darcy_friction_factor: float

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def circular_pipe_operating_point(
    *,
    pressure_loss: float,
    density: float,
    dynamic_viscosity: float,
    length: float,
    diameter: float,
    regime: str,
    roughness: float = 0.0,
    loss_coefficient: float = 0.0,
) -> PipeOperatingPoint:
    """Invert distributed and local loss for one declared flow regime.

    Laminar flow uses the exact linear-plus-quadratic pressure relation.
    Turbulent flow uses a bracketed solve around the implicit Colebrook factor.
    Solutions landing in the transitional range fail instead of being blended.
    """

    target = _positive(pressure_loss, "pressure_loss")
    selected_density = _positive(density, "density")
    viscosity = _positive(dynamic_viscosity, "dynamic_viscosity")
    selected_length = _positive(length, "length")
    selected_diameter = _positive(diameter, "diameter")
    selected_roughness = _nonnegative(roughness, "roughness")
    selected_loss = _nonnegative(loss_coefficient, "loss_coefficient")
    if selected_roughness >= selected_diameter:
        raise ValueError("roughness must be smaller than diameter.")
    if regime not in {"laminar", "turbulent"}:
        raise ValueError("regime must be 'laminar' or 'turbulent'.")

    if regime == "laminar":
        linear = 32.0 * viscosity * selected_length / selected_diameter**2
        quadratic = 0.5 * selected_loss * selected_density
        velocity = (
            target / linear
            if quadratic == 0.0
            else 2.0 * target / (linear + math.sqrt(linear**2 + 4.0 * quadratic * target))
        )
    else:
        lower = 4000.0 * viscosity / (selected_density * selected_diameter)

        def loss(velocity: float) -> float:
            return pipe_pressure_loss(
                density=selected_density,
                dynamic_viscosity=viscosity,
                mean_velocity=velocity,
                length=selected_length,
                hydraulic_diameter=selected_diameter,
                roughness=selected_roughness,
                loss_coefficient=selected_loss,
            ).total_pressure_loss

        if target < loss(lower):
            raise ValueError(
                "Requested pressure loss has no solution in the declared turbulent regime."
            )
        upper = 2.0 * lower
        for _ in range(100):
            if loss(upper) >= target:
                break
            upper *= 2.0
        else:
            raise ValueError("Could not bracket the turbulent pipe operating point.")
        for _ in range(100):
            middle = 0.5 * (lower + upper)
            if loss(middle) < target:
                lower = middle
            else:
                upper = middle
        velocity = 0.5 * (lower + upper)

    solution_reynolds = reynolds_number(
        density=selected_density,
        mean_velocity=velocity,
        hydraulic_diameter=selected_diameter,
        dynamic_viscosity=viscosity,
    )
    if regime == "laminar" and solution_reynolds >= 2300.0:
        raise ValueError(
            "Pressure-driven solution is transitional or turbulent, not declared 'laminar'."
        )
    estimate = pipe_pressure_loss(
        density=selected_density,
        dynamic_viscosity=viscosity,
        mean_velocity=velocity,
        length=selected_length,
        hydraulic_diameter=selected_diameter,
        roughness=selected_roughness,
        loss_coefficient=selected_loss,
    )
    if estimate.regime != regime:
        raise ValueError(
            f"Pressure-driven solution falls in {estimate.regime!r}, not declared {regime!r}."
        )
    return PipeOperatingPoint(
        regime=regime,
        pressure_loss=estimate.total_pressure_loss,
        mean_velocity=velocity,
        volume_flow_rate=math.pi * selected_diameter**2 / 4.0 * velocity,
        reynolds_number=estimate.reynolds_number,
        darcy_friction_factor=estimate.darcy_friction_factor,
    )
