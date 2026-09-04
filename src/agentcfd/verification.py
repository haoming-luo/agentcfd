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


def assess_turbulent_wall_study(
    records: Iterable[Mapping[str, object]],
    *,
    maximum_mean_y_plus_spread: float = 0.05,
    maximum_fine_relative_change: float = 0.02,
) -> dict[str, object]:
    """Assess fixed-wall-cell turbulent precursor results without misusing GCI.

    A constant wall-cell height deliberately breaks geometric similarity as
    the interior count changes.  The resulting family can certify consistent
    wall-function sampling and reveal a pressure-gradient plateau, but it
    cannot supply Richardson/GCI uncertainty.  That limitation is emitted as
    data rather than hidden in prose.
    """

    selected = tuple(records)
    if len(selected) < 3:
        raise ValueError("At least three turbulent precursor results are required.")
    y_spread_limit = positive_float(
        maximum_mean_y_plus_spread,
        name="maximum_mean_y_plus_spread",
    )
    fine_change_limit = positive_float(
        maximum_fine_relative_change,
        name="maximum_fine_relative_change",
    )
    model_identities: set[str] = set()
    fractions: set[float] = set()
    wall_functions: set[str] = set()
    stability_windows: set[int] = set()
    rows: list[dict[str, object]] = []
    all_sources_accepted = True
    for index, record in enumerate(selected, start=1):
        if not isinstance(record, Mapping):
            raise ValueError(f"Result {index} must be a mapping.")
        source_accepted = bool(
            record.get("status") == "completed"
            and record.get("converged") is True
            and record.get("accepted") is True
            and record.get("trust_level") in {"verified", "validated"}
            and record.get("provider") == "openfoam-periodic-precursor"
        )
        all_sources_accepted = all_sources_accepted and source_accepted
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"Result {index} is missing provenance.")
        model_identity = provenance.get("model_sha256")
        if (
            not isinstance(model_identity, str)
            or len(model_identity) != 64
            or any(
                character not in "0123456789abcdef"
                for character in model_identity.lower()
            )
        ):
            raise ValueError(f"Result {index} is missing a model SHA-256 identity.")
        model_identities.add(model_identity)

        scientific = record.get("scientific_inputs")
        if not isinstance(scientific, Mapping):
            raise ValueError(f"Result {index} is missing scientific inputs.")
        scientific_record = scientific.get("record", scientific)
        precursor = (
            scientific_record.get("precursor")
            if isinstance(scientific_record, Mapping)
            else None
        )
        if not isinstance(precursor, Mapping):
            raise ValueError(f"Result {index} has no precursor controls.")
        validation_policy = scientific_record.get("validation_policy")
        if not isinstance(validation_policy, Mapping):
            raise ValueError(f"Result {index} has no precursor validation policy.")
        stability_window = validation_policy.get("minimum_precursor_steady_samples")
        if (
            isinstance(stability_window, bool)
            or not isinstance(stability_window, int)
            or stability_window < 10
        ):
            raise ValueError(f"Result {index} has an invalid precursor stability window.")
        stability_windows.add(stability_window)
        cross_cells = precursor.get("cross_section_cells")
        if isinstance(cross_cells, bool) or not isinstance(cross_cells, int):
            raise ValueError(f"Result {index} has an invalid cross-section cell count.")
        fraction = precursor.get("nominal_wall_cell_fraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError(
                f"Result {index} must declare a numeric nominal wall-cell fraction."
            )
        normalized_fraction = positive_float(
            fraction,
            name=f"Result {index} nominal wall-cell fraction",
        )
        if normalized_fraction >= 1.0:
            raise ValueError(f"Result {index} nominal wall-cell fraction must be below one.")
        fractions.add(normalized_fraction)
        wall_function = precursor.get("nut_wall_function")
        if not isinstance(wall_function, str) or not wall_function:
            raise ValueError(f"Result {index} has no momentum wall-function identity.")
        wall_functions.add(wall_function)

        quantities = record.get("quantities")
        if not isinstance(quantities, Mapping):
            raise ValueError(f"Result {index} is missing quantities.")
        required = {
            "flow.pressure_gradient": "Pa/m",
            "flow.darcy_friction_factor_relative_error": "1",
            "mesh.cell_count": "1",
            "mesh.maximum_aspect_ratio": "1",
            "mesh.nominal_wall_cell_height": "m",
            "runtime.total_wall_seconds": "s",
            "wall.y_plus.minimum": "1",
            "wall.y_plus.average": "1",
            "wall.y_plus.maximum": "1",
        }
        values: dict[str, float] = {}
        for name, unit in required.items():
            values[name] = _record_quantity_value(quantities, name, index=index)
            entry = quantities[name]
            assert isinstance(entry, Mapping)
            if entry.get("unit") != unit:
                raise ValueError(f"Result {index} quantity {name!r} must use unit {unit!r}.")
        if values["mesh.cell_count"] <= 0.0 or not values["mesh.cell_count"].is_integer():
            raise ValueError(f"Result {index} mesh cell count must be a positive integer.")
        for name in (
            "flow.pressure_gradient",
            "mesh.nominal_wall_cell_height",
        ):
            if values[name] <= 0.0:
                raise ValueError(f"Result {index} quantity {name!r} must be positive.")
        for name in (
            "flow.darcy_friction_factor_relative_error",
            "mesh.maximum_aspect_ratio",
            "runtime.total_wall_seconds",
            "wall.y_plus.minimum",
            "wall.y_plus.average",
            "wall.y_plus.maximum",
        ):
            if values[name] < 0.0:
                raise ValueError(f"Result {index} quantity {name!r} must be non-negative.")
        if not (
            values["wall.y_plus.minimum"]
            <= values["wall.y_plus.average"]
            <= values["wall.y_plus.maximum"]
        ):
            raise ValueError(f"Result {index} wall y-plus statistics are unordered.")
        checks = record.get("checks")
        if not isinstance(checks, list):
            raise ValueError(f"Result {index} is missing checks.")
        gradient_stability = next(
            (
                check
                for check in checks
                if isinstance(check, Mapping)
                and check.get("name") == "pressure-gradient-tail-stability"
            ),
            None,
        )
        if not isinstance(gradient_stability, Mapping):
            raise ValueError(f"Result {index} has no pressure-gradient stability check.")
        gradient_tail_range = gradient_stability.get("value")
        if (
            isinstance(gradient_tail_range, bool)
            or not isinstance(gradient_tail_range, (int, float))
            or not math.isfinite(float(gradient_tail_range))
            or float(gradient_tail_range) < 0.0
        ):
            raise ValueError(f"Result {index} has invalid pressure-gradient stability evidence.")
        rows.append(
            {
                "cross_section_cells": cross_cells,
                "cell_count": int(values["mesh.cell_count"]),
                "nominal_wall_cell_height_m": values[
                    "mesh.nominal_wall_cell_height"
                ],
                "pressure_gradient_pa_per_m": values["flow.pressure_gradient"],
                "pressure_gradient_tail_relative_range": float(gradient_tail_range),
                "stability_window_samples": stability_window,
                "colebrook_relative_error": values[
                    "flow.darcy_friction_factor_relative_error"
                ],
                "wall_y_plus": {
                    "minimum": values["wall.y_plus.minimum"],
                    "average": values["wall.y_plus.average"],
                    "maximum": values["wall.y_plus.maximum"],
                },
                "maximum_aspect_ratio": values["mesh.maximum_aspect_ratio"],
                "runtime_wall_seconds": values["runtime.total_wall_seconds"],
                "source_accepted": source_accepted,
            }
        )

    if len(model_identities) != 1:
        raise ValueError("Wall-study results must share one model SHA-256 identity.")
    if len(fractions) != 1:
        raise ValueError("Wall-study results must share one nominal wall-cell fraction.")
    if len(wall_functions) != 1:
        raise ValueError("Wall-study results must share one momentum wall function.")
    if len(stability_windows) != 1:
        raise ValueError("Wall-study results must share one precursor stability window.")
    rows.sort(key=lambda item: int(item["cross_section_cells"]))
    cross_counts = [int(item["cross_section_cells"]) for item in rows]
    if len(set(cross_counts)) != len(cross_counts):
        raise ValueError("Wall-study cross-section cell counts must be distinct.")

    wall_heights = [float(item["nominal_wall_cell_height_m"]) for item in rows]
    fixed_wall_height = all(
        math.isclose(value, wall_heights[0], rel_tol=1.0e-12, abs_tol=1.0e-15)
        for value in wall_heights[1:]
    )
    y_averages = [float(item["wall_y_plus"]["average"]) for item in rows]  # type: ignore[index]
    mean_y_plus_spread = (max(y_averages) - min(y_averages)) / max(
        abs(sum(y_averages) / len(y_averages)),
        1.0e-300,
    )
    y_plus_in_wall_function_range = all(
        float(item["wall_y_plus"]["minimum"]) >= 30.0  # type: ignore[index]
        and float(item["wall_y_plus"]["maximum"]) <= 300.0  # type: ignore[index]
        for item in rows
    )
    gradients = [float(item["pressure_gradient_pa_per_m"]) for item in rows]
    differences = [right - left for left, right in zip(gradients, gradients[1:])]
    monotonic = bool(
        all(value > 0.0 for value in differences)
        or all(value < 0.0 for value in differences)
    )
    fine_relative_change = abs(gradients[-1] - gradients[-2]) / max(
        abs(gradients[-1]),
        1.0e-300,
    )
    pressure_gradient_envelope = (max(gradients) - min(gradients)) / max(
        abs(gradients[-1]),
        1.0e-300,
    )
    wall_strategy_accepted = bool(
        all_sources_accepted
        and fixed_wall_height
        and y_plus_in_wall_function_range
        and mean_y_plus_spread <= y_spread_limit
    )
    discretization_plateau = fine_relative_change <= fine_change_limit
    checks = [
        {
            "name": "accepted-source-results",
            "passed": all_sources_accepted,
            "value": all_sources_accepted,
            "limit": True,
        },
        {
            "name": "fixed-nominal-wall-cell-height",
            "passed": fixed_wall_height,
            "value": max(wall_heights) - min(wall_heights),
            "limit": "equal within 1e-12 relative tolerance",
        },
        {
            "name": "wall-function-y-plus-range",
            "passed": y_plus_in_wall_function_range,
            "value": {
                "minimum": min(
                    float(item["wall_y_plus"]["minimum"]) for item in rows  # type: ignore[index]
                ),
                "maximum": max(
                    float(item["wall_y_plus"]["maximum"]) for item in rows  # type: ignore[index]
                ),
            },
            "limit": {"minimum": 30.0, "maximum": 300.0},
        },
        {
            "name": "mean-y-plus-spread",
            "passed": mean_y_plus_spread <= y_spread_limit,
            "value": mean_y_plus_spread,
            "limit": y_spread_limit,
        },
        {
            "name": "fine-pair-pressure-gradient-change",
            "passed": discretization_plateau,
            "value": fine_relative_change,
            "limit": fine_change_limit,
        },
        {
            "name": "monotonic-pressure-gradient",
            "passed": monotonic,
            "value": monotonic,
            "limit": True,
        },
    ]
    gci_reason = (
        "The fixed physical wall-cell height changes radial grading with resolution, "
        "so the family is not geometrically similar."
    )
    if not monotonic:
        gci_reason += " The pressure-gradient sequence is also oscillatory."
    return {
        "schema": "agentcfd.turbulent-wall-study/0.1",
        "model_sha256": next(iter(model_identities)),
        "quantity": "flow.pressure_gradient",
        "nominal_wall_cell_fraction": next(iter(fractions)),
        "nut_wall_function": next(iter(wall_functions)),
        "cases": rows,
        "metrics": {
            "mean_y_plus_relative_spread": mean_y_plus_spread,
            "fine_pair_pressure_gradient_relative_change": fine_relative_change,
            "pressure_gradient_relative_envelope": pressure_gradient_envelope,
            "monotonic": monotonic,
        },
        "gci": {
            "applicable": False,
            "reason": gci_reason,
        },
        "acceptance": {
            "wall_strategy_accepted": wall_strategy_accepted,
            "discretization_plateau": discretization_plateau,
            "uncertainty_promotion_accepted": False,
            "checks": checks,
        },
    }


def assess_turbulent_precursor_grid_study(
    records: Iterable[Mapping[str, object]],
    *,
    maximum_fine_relative_change: float = 0.02,
) -> dict[str, object]:
    """Assess a geometrically similar, uniform precursor grid candidate.

    The periodic precursor has one axial cell, so its cross-section resolution
    supplies the characteristic size ``h/D = 1/N``.  Using total cell count
    with a three-dimensional exponent would be incorrect for this extruded 2-D
    convergence problem.
    """

    selected = tuple(records)
    if len(selected) != 3:
        raise ValueError("Exactly three turbulent precursor results are required.")
    fine_change_limit = positive_float(
        maximum_fine_relative_change,
        name="maximum_fine_relative_change",
    )
    model_identities: set[str] = set()
    stability_windows: set[int] = set()
    rows: list[dict[str, object]] = []
    all_sources_accepted = True
    for index, record in enumerate(selected, start=1):
        if not isinstance(record, Mapping):
            raise ValueError(f"Result {index} must be a mapping.")
        source_accepted = bool(
            record.get("status") == "completed"
            and record.get("converged") is True
            and record.get("accepted") is True
            and record.get("trust_level") in {"verified", "validated"}
            and record.get("provider") == "openfoam-periodic-precursor"
        )
        all_sources_accepted = all_sources_accepted and source_accepted
        provenance = record.get("provenance")
        identity = provenance.get("model_sha256") if isinstance(provenance, Mapping) else None
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity.lower())
        ):
            raise ValueError(f"Result {index} is missing a model SHA-256 identity.")
        model_identities.add(identity)
        scientific = record.get("scientific_inputs")
        scientific_record = scientific.get("record", scientific) if isinstance(scientific, Mapping) else None
        precursor = scientific_record.get("precursor") if isinstance(scientific_record, Mapping) else None
        policy = scientific_record.get("validation_policy") if isinstance(scientific_record, Mapping) else None
        if not isinstance(precursor, Mapping) or not isinstance(policy, Mapping):
            raise ValueError(f"Result {index} has incomplete precursor controls.")
        cross_cells = precursor.get("cross_section_cells")
        if isinstance(cross_cells, bool) or not isinstance(cross_cells, int) or cross_cells < 2:
            raise ValueError(f"Result {index} has an invalid cross-section cell count.")
        if precursor.get("nominal_wall_cell_fraction") is not None:
            raise ValueError(
                "Geometrically similar precursor grids must use uniform wall-normal spacing."
            )
        stability_window = policy.get("minimum_precursor_steady_samples")
        if (
            isinstance(stability_window, bool)
            or not isinstance(stability_window, int)
            or stability_window < 10
        ):
            raise ValueError(f"Result {index} has an invalid precursor stability window.")
        stability_windows.add(stability_window)
        quantities = record.get("quantities")
        if not isinstance(quantities, Mapping):
            raise ValueError(f"Result {index} is missing quantities.")
        names = (
            "flow.pressure_gradient",
            "flow.darcy_friction_factor_relative_error",
            "mesh.maximum_aspect_ratio",
            "runtime.total_wall_seconds",
            "wall.y_plus.minimum",
            "wall.y_plus.average",
            "wall.y_plus.maximum",
        )
        units = ("Pa/m", "1", "1", "s", "1", "1", "1")
        values: dict[str, float] = {}
        for name, unit in zip(names, units):
            values[name] = _record_quantity_value(quantities, name, index=index)
            entry = quantities[name]
            assert isinstance(entry, Mapping)
            if entry.get("unit") != unit:
                raise ValueError(f"Result {index} quantity {name!r} must use unit {unit!r}.")
        if values["flow.pressure_gradient"] <= 0.0:
            raise ValueError(f"Result {index} pressure gradient must be positive.")
        for name in (
            "flow.darcy_friction_factor_relative_error",
            "mesh.maximum_aspect_ratio",
            "runtime.total_wall_seconds",
        ):
            if values[name] < 0.0:
                raise ValueError(f"Result {index} quantity {name!r} must be non-negative.")
        if not (
            0.0 <= values["wall.y_plus.minimum"]
            <= values["wall.y_plus.average"]
            <= values["wall.y_plus.maximum"]
        ):
            raise ValueError(f"Result {index} wall y-plus statistics are unordered.")
        checks = record.get("checks")
        stability = next(
            (
                check
                for check in checks
                if isinstance(check, Mapping)
                and check.get("name") == "pressure-gradient-tail-stability"
            ),
            None,
        ) if isinstance(checks, list) else None
        if not isinstance(stability, Mapping) or stability.get("passed") is not True:
            raise ValueError(f"Result {index} lacks passed gradient-stability evidence.")
        drift = stability.get("value")
        if (
            isinstance(drift, bool)
            or not isinstance(drift, (int, float))
            or not math.isfinite(float(drift))
            or float(drift) < 0.0
        ):
            raise ValueError(f"Result {index} has invalid gradient-stability evidence.")
        rows.append(
            {
                "cross_section_cells": cross_cells,
                "characteristic_size_over_diameter": 1.0 / cross_cells,
                "pressure_gradient_pa_per_m": values["flow.pressure_gradient"],
                "pressure_gradient_tail_relative_range": float(drift),
                "stability_window_samples": stability_window,
                "colebrook_relative_error": values[
                    "flow.darcy_friction_factor_relative_error"
                ],
                "wall_y_plus": {
                    "minimum": values["wall.y_plus.minimum"],
                    "average": values["wall.y_plus.average"],
                    "maximum": values["wall.y_plus.maximum"],
                },
                "maximum_aspect_ratio": values["mesh.maximum_aspect_ratio"],
                "runtime_wall_seconds": values["runtime.total_wall_seconds"],
                "source_accepted": source_accepted,
            }
        )
    if len(model_identities) != 1:
        raise ValueError("Precursor grid-study results must share one model identity.")
    if len(stability_windows) != 1:
        raise ValueError("Precursor grid-study results must share one stability window.")
    rows.sort(key=lambda item: int(item["cross_section_cells"]))
    counts = [int(item["cross_section_cells"]) for item in rows]
    if len(set(counts)) != 3:
        raise ValueError("Precursor grid-study resolutions must be distinct.")
    ratio_21 = counts[1] / counts[0]
    ratio_32 = counts[2] / counts[1]
    geometrically_similar = math.isclose(ratio_21, ratio_32, rel_tol=1.0e-12)
    if not geometrically_similar:
        raise ValueError("Precursor cross-section counts must use one refinement ratio.")
    y_plus_consistent = all(
        float(item["wall_y_plus"]["minimum"]) >= 30.0  # type: ignore[index]
        and float(item["wall_y_plus"]["maximum"]) <= 300.0  # type: ignore[index]
        for item in rows
    )
    gradients = [float(item["pressure_gradient_pa_per_m"]) for item in rows]
    fine_relative_change = abs(gradients[-1] - gradients[-2]) / abs(gradients[-1])
    envelope = (max(gradients) - min(gradients)) / abs(gradients[-1])
    try:
        gci_result = grid_convergence_index(
            GridSolution(
                characteristic_size=float(item["characteristic_size_over_diameter"]),
                value=float(item["pressure_gradient_pa_per_m"]),
                label=f"c{item['cross_section_cells']}",
            )
            for item in rows
        )
    except ValueError as error:
        gci: dict[str, object] = {"applicable": False, "reason": str(error)}
        uncertainty_accepted = False
        monotonic = False
    else:
        gci_acceptance = assess_grid_convergence(gci_result)
        gci = {
            "applicable": True,
            "result": gci_result.to_dict(),
            "acceptance": gci_acceptance,
        }
        uncertainty_accepted = bool(gci_acceptance["accepted"] and y_plus_consistent)
        monotonic = True
    return {
        "schema": "agentcfd.turbulent-precursor-grid-study/0.1",
        "model_sha256": next(iter(model_identities)),
        "quantity": "flow.pressure_gradient",
        "refinement_ratio": ratio_21,
        "characteristic_size_definition": "h/D = 1/cross_section_cells",
        "cases": rows,
        "metrics": {
            "fine_pair_pressure_gradient_relative_change": fine_relative_change,
            "pressure_gradient_relative_envelope": envelope,
            "monotonic": monotonic,
        },
        "gci": gci,
        "acceptance": {
            "source_results_accepted": all_sources_accepted,
            "wall_model_consistent": y_plus_consistent,
            "discretization_plateau": fine_relative_change <= fine_change_limit,
            "uncertainty_promotion_accepted": uncertainty_accepted,
        },
    }


def assess_turbulent_wall_function_study(
    records: Iterable[Mapping[str, object]],
    *,
    maximum_correlation_relative_error: float = 0.02,
) -> dict[str, object]:
    """Screen supported SST momentum wall functions on one identical mesh.

    This is a controlled model-form sensitivity study, not a validation
    certificate.  It can identify a candidate for broader validation while
    explicitly refusing to promote a project default from one Reynolds-number
    point and one engineering correlation.
    """

    selected = tuple(records)
    expected_functions = {
        "nutUBlendedWallFunction",
        "nutUSpaldingWallFunction",
        "nutkWallFunction",
    }
    if len(selected) != len(expected_functions):
        raise ValueError("Exactly three turbulent wall-function results are required.")
    error_limit = positive_float(
        maximum_correlation_relative_error,
        name="maximum_correlation_relative_error",
    )
    model_identities: set[str] = set()
    mesh_identities: set[str] = set()
    cross_counts: set[int] = set()
    wall_fractions: set[float | None] = set()
    stability_windows: set[int] = set()
    wall_functions: set[str] = set()
    rows: list[dict[str, object]] = []
    all_sources_accepted = True
    for index, record in enumerate(selected, start=1):
        if not isinstance(record, Mapping):
            raise ValueError(f"Result {index} must be a mapping.")
        source_accepted = bool(
            record.get("status") == "completed"
            and record.get("converged") is True
            and record.get("accepted") is True
            and record.get("trust_level") in {"verified", "validated"}
            and record.get("provider") == "openfoam-periodic-precursor"
        )
        all_sources_accepted = all_sources_accepted and source_accepted
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"Result {index} is missing provenance.")
        model_identity = provenance.get("model_sha256")
        mesh_identity = provenance.get("mesh_sha256")
        for value, label in (
            (model_identity, "model"),
            (mesh_identity, "mesh"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value.lower())
            ):
                raise ValueError(f"Result {index} is missing a {label} SHA-256 identity.")
        model_identities.add(model_identity)
        mesh_identities.add(mesh_identity)

        scientific = record.get("scientific_inputs")
        scientific_record = scientific.get("record", scientific) if isinstance(scientific, Mapping) else None
        precursor = scientific_record.get("precursor") if isinstance(scientific_record, Mapping) else None
        policy = scientific_record.get("validation_policy") if isinstance(scientific_record, Mapping) else None
        if not isinstance(precursor, Mapping) or not isinstance(policy, Mapping):
            raise ValueError(f"Result {index} has incomplete precursor controls.")
        wall_function = precursor.get("nut_wall_function")
        if not isinstance(wall_function, str) or wall_function not in expected_functions:
            raise ValueError(f"Result {index} has an unsupported momentum wall function.")
        wall_functions.add(wall_function)
        cross_cells = precursor.get("cross_section_cells")
        if isinstance(cross_cells, bool) or not isinstance(cross_cells, int) or cross_cells < 2:
            raise ValueError(f"Result {index} has an invalid cross-section cell count.")
        cross_counts.add(cross_cells)
        fraction = precursor.get("nominal_wall_cell_fraction")
        if fraction is None:
            wall_fractions.add(None)
        elif isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError(f"Result {index} has an invalid wall-cell fraction.")
        else:
            normalized_fraction = positive_float(
                fraction,
                name=f"Result {index} nominal wall-cell fraction",
            )
            if normalized_fraction >= 1.0:
                raise ValueError(f"Result {index} wall-cell fraction must be below one.")
            wall_fractions.add(normalized_fraction)
        stability_window = policy.get("minimum_precursor_steady_samples")
        if (
            isinstance(stability_window, bool)
            or not isinstance(stability_window, int)
            or stability_window < 10
        ):
            raise ValueError(f"Result {index} has an invalid stability window.")
        stability_windows.add(stability_window)

        quantities = record.get("quantities")
        if not isinstance(quantities, Mapping):
            raise ValueError(f"Result {index} is missing quantities.")
        required = {
            "flow.pressure_gradient": "Pa/m",
            "flow.darcy_friction_factor": "1",
            "reference.flow.darcy_friction_factor": "1",
            "flow.darcy_friction_factor_relative_error": "1",
            "runtime.total_wall_seconds": "s",
            "wall.y_plus.minimum": "1",
            "wall.y_plus.average": "1",
            "wall.y_plus.maximum": "1",
        }
        values: dict[str, float] = {}
        for name, unit in required.items():
            values[name] = _record_quantity_value(quantities, name, index=index)
            entry = quantities[name]
            assert isinstance(entry, Mapping)
            if entry.get("unit") != unit:
                raise ValueError(f"Result {index} quantity {name!r} must use unit {unit!r}.")
        if any(value < 0.0 for value in values.values()):
            raise ValueError(f"Result {index} contains a negative comparison quantity.")
        if not (
            values["wall.y_plus.minimum"]
            <= values["wall.y_plus.average"]
            <= values["wall.y_plus.maximum"]
        ):
            raise ValueError(f"Result {index} wall y-plus statistics are unordered.")
        rows.append(
            {
                "nut_wall_function": wall_function,
                "pressure_gradient_pa_per_m": values["flow.pressure_gradient"],
                "darcy_friction_factor": values["flow.darcy_friction_factor"],
                "reference_darcy_friction_factor": values[
                    "reference.flow.darcy_friction_factor"
                ],
                "colebrook_relative_error": values[
                    "flow.darcy_friction_factor_relative_error"
                ],
                "wall_y_plus": {
                    "minimum": values["wall.y_plus.minimum"],
                    "average": values["wall.y_plus.average"],
                    "maximum": values["wall.y_plus.maximum"],
                },
                "runtime_wall_seconds": values["runtime.total_wall_seconds"],
                "source_accepted": source_accepted,
            }
        )

    if len(model_identities) != 1:
        raise ValueError("Wall-function results must share one model identity.")
    if len(mesh_identities) != 1:
        raise ValueError("Wall-function results must share one mesh identity.")
    if len(cross_counts) != 1 or len(wall_fractions) != 1:
        raise ValueError("Wall-function results must share identical mesh controls.")
    if len(stability_windows) != 1:
        raise ValueError("Wall-function results must share one stability window.")
    if wall_functions != expected_functions:
        raise ValueError("Wall-function results must cover each supported implementation exactly once.")
    rows.sort(key=lambda item: float(item["colebrook_relative_error"]))
    y_plus_consistent = all(
        float(item["wall_y_plus"]["minimum"]) >= 30.0  # type: ignore[index]
        and float(item["wall_y_plus"]["maximum"]) <= 300.0  # type: ignore[index]
        for item in rows
    )
    best = rows[0]
    best_error = float(best["colebrook_relative_error"])
    screening_accepted = bool(
        all_sources_accepted and y_plus_consistent and best_error <= error_limit
    )
    return {
        "schema": "agentcfd.turbulent-wall-function-study/0.1",
        "model_sha256": next(iter(model_identities)),
        "mesh_sha256": next(iter(mesh_identities)),
        "cross_section_cells": next(iter(cross_counts)),
        "nominal_wall_cell_fraction": next(iter(wall_fractions)),
        "quantity": "flow.darcy_friction_factor",
        "cases": rows,
        "recommendation": {
            "candidate": best["nut_wall_function"],
            "basis": "minimum absolute relative difference from the Colebrook smooth-pipe correlation",
            "benchmark_specific": True,
        },
        "acceptance": {
            "source_results_accepted": all_sources_accepted,
            "identical_mesh": True,
            "wall_function_y_plus_range": y_plus_consistent,
            "screening_accepted": screening_accepted,
            "maximum_correlation_relative_error": error_limit,
            "default_promotion_accepted": False,
            "default_promotion_reason": (
                "One Reynolds-number point and one engineering correlation cannot "
                "establish a general industrial default."
            ),
        },
    }


__all__ = [
    "GridConvergencePolicy",
    "GridConvergenceResult",
    "GridSolution",
    "ValidationPointAssessment",
    "assess_validation_point",
    "assess_grid_convergence",
    "assess_turbulent_wall_study",
    "assess_turbulent_precursor_grid_study",
    "assess_turbulent_wall_function_study",
    "grid_convergence_from_result_records",
    "grid_convergence_index",
]
