"""Solver-neutral numerical verification utilities."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class GridSolution:
    """One scalar quantity of interest evaluated on one grid."""

    characteristic_size: float
    value: float
    label: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.characteristic_size) or self.characteristic_size <= 0.0:
            raise ValueError("Grid characteristic_size must be positive and finite.")
        if not math.isfinite(self.value):
            raise ValueError("Grid solution value must be finite.")


@dataclass(frozen=True, slots=True)
class GridConvergenceResult:
    """Three-grid Richardson extrapolation and fine-grid GCI evidence."""

    observed_order: float
    extrapolated_value: float
    fine_grid_absolute_gci: float
    fine_grid_relative_gci: float | None
    medium_grid_absolute_gci: float
    asymptotic_ratio: float
    refinement_ratio_fine_medium: float
    refinement_ratio_medium_coarse: float
    converging: bool
    safety_factor: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _observed_order(
    fine: GridSolution,
    medium: GridSolution,
    coarse: GridSolution,
) -> float:
    epsilon_21 = medium.value - fine.value
    epsilon_32 = coarse.value - medium.value
    if epsilon_21 == 0.0 or epsilon_32 == 0.0:
        raise ValueError("Grid solutions must differ to estimate observed order.")
    if epsilon_21 * epsilon_32 <= 0.0:
        raise ValueError(
            "The three values are oscillatory; monotonic Richardson extrapolation is not valid."
        )

    r21 = medium.characteristic_size / fine.characteristic_size
    r32 = coarse.characteristic_size / medium.characteristic_size
    target = abs(epsilon_32 / epsilon_21)
    if math.isclose(r21, r32, rel_tol=1.0e-10, abs_tol=1.0e-12):
        order = math.log(target) / math.log(r21)
        if order <= 0.0:
            raise ValueError("Grid sequence is not converging with refinement.")
        return order

    def residual(order: float) -> float:
        predicted = r21**order * (r32**order - 1.0) / (r21**order - 1.0)
        return predicted - target

    lower = 1.0e-3
    lower_value = residual(lower)
    bracket: tuple[float, float] | None = None
    for index in range(1, 1201):
        upper = 12.0 * index / 1200.0
        upper_value = residual(upper)
        if lower_value * upper_value <= 0.0:
            bracket = (lower, upper)
            break
        lower, lower_value = upper, upper_value
    if bracket is None:
        raise ValueError("No positive observed order fits this unequal-ratio grid sequence.")

    lower, upper = bracket
    for _ in range(100):
        middle = 0.5 * (lower + upper)
        if residual(lower) * residual(middle) <= 0.0:
            upper = middle
        else:
            lower = middle
    return 0.5 * (lower + upper)


def grid_convergence_index(
    solutions: Iterable[GridSolution],
    *,
    safety_factor: float = 1.25,
) -> GridConvergenceResult:
    """Evaluate a monotonic three-grid Richardson/GCI study.

    Solutions may be supplied in any order; smaller characteristic size is
    treated as finer. Oscillatory or non-converging triples are rejected rather
    than converted into a misleading uncertainty estimate.
    """

    selected = sorted(tuple(solutions), key=lambda item: item.characteristic_size)
    if len(selected) != 3:
        raise ValueError("Exactly three grid solutions are required.")
    if safety_factor < 1.0 or not math.isfinite(safety_factor):
        raise ValueError("GCI safety_factor must be finite and at least one.")
    fine, medium, coarse = selected
    if len({item.characteristic_size for item in selected}) != 3:
        raise ValueError("Grid characteristic sizes must be distinct.")

    order = _observed_order(fine, medium, coarse)
    r21 = medium.characteristic_size / fine.characteristic_size
    r32 = coarse.characteristic_size / medium.characteristic_size
    denominator_21 = r21**order - 1.0
    denominator_32 = r32**order - 1.0
    extrapolated = fine.value + (fine.value - medium.value) / denominator_21
    fine_absolute = safety_factor * abs(fine.value - medium.value) / denominator_21
    medium_absolute = safety_factor * abs(medium.value - coarse.value) / denominator_32
    fine_relative = None if fine.value == 0.0 else fine_absolute / abs(fine.value)
    asymptotic_ratio = medium_absolute / (r21**order * fine_absolute)

    return GridConvergenceResult(
        observed_order=order,
        extrapolated_value=extrapolated,
        fine_grid_absolute_gci=fine_absolute,
        fine_grid_relative_gci=fine_relative,
        medium_grid_absolute_gci=medium_absolute,
        asymptotic_ratio=asymptotic_ratio,
        refinement_ratio_fine_medium=r21,
        refinement_ratio_medium_coarse=r32,
        converging=True,
        safety_factor=safety_factor,
    )
