"""Solver-neutral numerical verification utilities."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from ._validation import finite_float, nonnegative_float, positive_float


@dataclass(frozen=True, slots=True)
class GridSolution:
    """One scalar quantity of interest evaluated on one grid."""

    characteristic_size: float
    value: float
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "characteristic_size",
            positive_float(self.characteristic_size, name="Grid characteristic_size"),
        )
        object.__setattr__(
            self,
            "value",
            finite_float(self.value, name="Grid solution value"),
        )
        if not isinstance(self.label, str):
            raise ValueError("Grid solution label must be a string.")


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

    def __post_init__(self) -> None:
        for name in ("observed_order", "asymptotic_ratio"):
            object.__setattr__(
                self,
                name,
                positive_float(getattr(self, name), name=f"GCI {name}"),
            )
        object.__setattr__(
            self,
            "extrapolated_value",
            finite_float(self.extrapolated_value, name="GCI extrapolated_value"),
        )
        for name in ("fine_grid_absolute_gci", "medium_grid_absolute_gci"):
            object.__setattr__(
                self,
                name,
                nonnegative_float(getattr(self, name), name=f"GCI {name}"),
            )
        if self.fine_grid_relative_gci is not None:
            object.__setattr__(
                self,
                "fine_grid_relative_gci",
                nonnegative_float(
                    self.fine_grid_relative_gci,
                    name="GCI fine_grid_relative_gci",
                ),
            )
        for name in (
            "refinement_ratio_fine_medium",
            "refinement_ratio_medium_coarse",
        ):
            value = positive_float(getattr(self, name), name=f"GCI {name}")
            if value <= 1.0:
                raise ValueError(f"GCI {name} must be greater than one.")
            object.__setattr__(self, name, value)
        factor = positive_float(self.safety_factor, name="GCI safety_factor")
        if factor < 1.0:
            raise ValueError("GCI safety_factor must be at least one.")
        object.__setattr__(self, "safety_factor", factor)
        if not isinstance(self.converging, bool):
            raise ValueError("GCI converging must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GridConvergencePolicy:
    """Explicit promotion limits for a three-grid convergence result."""

    maximum_fine_relative_gci: float = 0.02
    maximum_asymptotic_ratio_deviation: float = 0.10

    def __post_init__(self) -> None:
        for name in (
            "maximum_fine_relative_gci",
            "maximum_asymptotic_ratio_deviation",
        ):
            value = positive_float(getattr(self, name), name=name)
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ValidationPointAssessment:
    """Transparent comparison of one simulation observable with reference data."""

    simulation_value: float
    reference_value: float
    absolute_error: float
    relative_error: float | None
    numerical_standard_uncertainty: float
    input_standard_uncertainty: float
    experimental_standard_uncertainty: float
    combined_standard_uncertainty: float
    coverage_factor: float
    expanded_validation_uncertainty: float
    normalized_error: float | None
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def assess_validation_point(
    simulation_value: float,
    reference_value: float,
    *,
    numerical_standard_uncertainty: float,
    input_standard_uncertainty: float,
    experimental_standard_uncertainty: float,
    coverage_factor: float = 2.0,
) -> ValidationPointAssessment:
    """Combine declared independent standard uncertainties by root-sum-square.

    This is a solver-neutral screening calculation, not a claim of complete
    ASME V&V 20 conformity. Correlations and model-form uncertainty require a
    study-specific treatment.
    """

    simulation = finite_float(simulation_value, name="simulation_value")
    reference = finite_float(reference_value, name="reference_value")
    uncertainties: dict[str, float] = {}
    for name, value in (
        ("numerical_standard_uncertainty", numerical_standard_uncertainty),
        ("input_standard_uncertainty", input_standard_uncertainty),
        ("experimental_standard_uncertainty", experimental_standard_uncertainty),
    ):
        selected = finite_float(value, name=name)
        if selected < 0.0:
            raise ValueError(f"{name} must be non-negative.")
        uncertainties[name] = selected
    factor = positive_float(coverage_factor, name="coverage_factor")
    combined = math.sqrt(sum(value**2 for value in uncertainties.values()))
    expanded = factor * combined
    absolute_error = abs(simulation - reference)
    relative_error = None if reference == 0.0 else absolute_error / abs(reference)
    normalized_error = (
        absolute_error / expanded
        if expanded > 0.0
        else 0.0
        if absolute_error == 0.0
        else None
    )
    return ValidationPointAssessment(
        simulation_value=simulation,
        reference_value=reference,
        absolute_error=absolute_error,
        relative_error=relative_error,
        numerical_standard_uncertainty=uncertainties["numerical_standard_uncertainty"],
        input_standard_uncertainty=uncertainties["input_standard_uncertainty"],
        experimental_standard_uncertainty=uncertainties[
            "experimental_standard_uncertainty"
        ],
        combined_standard_uncertainty=combined,
        coverage_factor=factor,
        expanded_validation_uncertainty=expanded,
        normalized_error=normalized_error,
        accepted=absolute_error <= expanded,
    )


def assess_grid_convergence(
    result: GridConvergenceResult,
    *,
    policy: GridConvergencePolicy | None = None,
) -> dict[str, object]:
    """Apply explicit uncertainty and asymptotic-range promotion gates."""

    if not isinstance(result, GridConvergenceResult):
        raise TypeError("result must be a GridConvergenceResult.")
    if policy is not None and not isinstance(policy, GridConvergencePolicy):
        raise TypeError("policy must be a GridConvergencePolicy.")
    selected = policy or GridConvergencePolicy()
    relative_gci = result.fine_grid_relative_gci
    checks = [
        {
            "name": "fine-grid-relative-gci",
            "passed": bool(
                relative_gci is not None
                and relative_gci <= selected.maximum_fine_relative_gci
            ),
            "value": relative_gci,
            "limit": selected.maximum_fine_relative_gci,
        },
        {
            "name": "asymptotic-ratio",
            "passed": bool(
                abs(result.asymptotic_ratio - 1.0)
                <= selected.maximum_asymptotic_ratio_deviation
            ),
            "value": result.asymptotic_ratio,
            "limit": selected.maximum_asymptotic_ratio_deviation,
        },
        {
            "name": "monotonic-convergence",
            "passed": result.converging,
            "value": result.converging,
            "limit": True,
        },
    ]
    return {
        "accepted": all(check["passed"] for check in checks),
        "policy": selected.to_dict(),
        "checks": checks,
    }


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

    supplied = tuple(solutions)
    if len(supplied) != 3:
        raise ValueError("Exactly three grid solutions are required.")
    if any(not isinstance(item, GridSolution) for item in supplied):
        raise TypeError("Grid convergence inputs must be GridSolution records.")
    selected = sorted(supplied, key=lambda item: item.characteristic_size)
    safety_factor = positive_float(safety_factor, name="GCI safety_factor")
    if safety_factor < 1.0:
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


def grid_convergence_from_result_records(
    records: Iterable[Mapping[str, object]],
    *,
    quantity: str,
    cell_count_quantity: str = "mesh.cell_count",
    safety_factor: float = 1.25,
) -> GridConvergenceResult:
    """Build a three-grid GCI study from serialized AgentCFD results.

    Each result must be a completed, converged run of the same public model and
    contain both the requested scalar quantity and a positive cell count.
    ``N**(-1/3)`` is used as relative characteristic grid size; for one fixed
    three-dimensional domain this preserves all refinement ratios.
    """

    selected = tuple(records)
    if len(selected) != 3:
        raise ValueError("Exactly three AgentCFD result records are required.")
    if not isinstance(quantity, str) or not quantity.strip():
        raise ValueError("quantity must be a non-empty string.")
    if not isinstance(cell_count_quantity, str) or not cell_count_quantity.strip():
        raise ValueError("cell_count_quantity must be a non-empty string.")

    identities: set[str] = set()
    analysis_identities: set[str] = set()
    analysis_identity_count = 0
    quantity_units: set[str | None] = set()
    solutions: list[GridSolution] = []
    for index, record in enumerate(selected, start=1):
        if not isinstance(record, Mapping):
            raise ValueError(f"Result {index} must be a mapping.")
        if record.get("status") != "completed" or record.get("converged") is not True:
            raise ValueError(f"Result {index} must be completed and converged.")
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"Result {index} is missing provenance.")
        identity = provenance.get("model_sha256")
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity.lower())
        ):
            raise ValueError(f"Result {index} is missing a model SHA-256 identity.")
        identities.add(identity)
        analysis_identity = provenance.get("analysis_sha256")
        if analysis_identity is not None:
            if (
                not isinstance(analysis_identity, str)
                or len(analysis_identity) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in analysis_identity.lower()
                )
            ):
                raise ValueError(f"Result {index} has an invalid analysis SHA-256 identity.")
            analysis_identities.add(analysis_identity)
            analysis_identity_count += 1

        quantities = record.get("quantities")
        if not isinstance(quantities, Mapping):
            raise ValueError(f"Result {index} is missing quantities.")
        value = _record_quantity_value(quantities, quantity, index=index)
        quantity_entry = quantities[quantity]
        assert isinstance(quantity_entry, Mapping)
        quantity_unit = quantity_entry.get("unit")
        if quantity_unit is not None and not isinstance(quantity_unit, str):
            raise ValueError(f"Result {index} quantity {quantity!r} has an invalid unit.")
        quantity_units.add(quantity_unit)
        cell_count = _record_quantity_value(
            quantities,
            cell_count_quantity,
            index=index,
        )
        cell_count_entry = quantities[cell_count_quantity]
        assert isinstance(cell_count_entry, Mapping)
        if cell_count_entry.get("unit") != "1":
            raise ValueError(f"Result {index} cell count must use unit '1'.")
        if cell_count <= 0.0:
            raise ValueError(f"Result {index} cell count must be positive.")
        if not cell_count.is_integer():
            raise ValueError(f"Result {index} cell count must be an integer value.")
        solutions.append(
            GridSolution(
                characteristic_size=cell_count ** (-1.0 / 3.0),
                value=value,
                label=f"result-{index}",
            )
        )

    if len(identities) != 1:
        raise ValueError("Grid convergence results must share one model SHA-256 identity.")
    if analysis_identity_count not in (0, len(selected)):
        raise ValueError(
            "Grid convergence results must all provide an analysis SHA-256 identity or none."
        )
    if len(analysis_identities) > 1:
        raise ValueError(
            "Grid convergence results must share one analysis procedure and output identity."
        )
    if len(quantity_units) != 1:
        raise ValueError("Grid convergence quantity units must match exactly.")
    return grid_convergence_index(solutions, safety_factor=safety_factor)


def _record_quantity_value(
    quantities: Mapping[object, object],
    name: str,
    *,
    index: int,
) -> float:
    entry = quantities.get(name)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ValueError(f"Result {index} is missing quantity {name!r}.")
    if isinstance(entry["value"], bool):
        raise ValueError(f"Result {index} quantity {name!r} must be numeric.")
    try:
        value = float(entry["value"])
    except (TypeError, ValueError) as error:
        raise ValueError(f"Result {index} quantity {name!r} must be numeric.") from error
    if not math.isfinite(value):
        raise ValueError(f"Result {index} quantity {name!r} must be finite.")
    return value


__all__ = [
    "GridConvergencePolicy",
    "GridConvergenceResult",
    "GridSolution",
    "ValidationPointAssessment",
    "assess_validation_point",
    "assess_grid_convergence",
    "grid_convergence_from_result_records",
    "grid_convergence_index",
]
