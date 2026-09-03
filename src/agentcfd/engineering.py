"""Small, dependency-free engineering calculations for CFD setup and checks."""

from __future__ import annotations

import math


def _positive(value: float, name: str) -> float:
    selected = float(value)
    if not math.isfinite(selected) or selected <= 0.0:
        raise ValueError(f"{name} must be positive and finite.")
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
