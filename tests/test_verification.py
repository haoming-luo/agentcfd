import pytest

from agentcfd.verification import (
    GridConvergencePolicy,
    GridSolution,
    assess_validation_point,
    assess_grid_convergence,
    grid_convergence_from_result_records,
    grid_convergence_index,
)


def test_validation_point_combines_declared_uncertainties_transparently():
    accepted = assess_validation_point(
        101.0,
        100.0,
        numerical_standard_uncertainty=0.3,
        input_standard_uncertainty=0.4,
        experimental_standard_uncertainty=0.5,
        coverage_factor=2.0,
    )
    rejected = assess_validation_point(
        102.0,
        100.0,
        numerical_standard_uncertainty=0.3,
        input_standard_uncertainty=0.4,
        experimental_standard_uncertainty=0.5,
        coverage_factor=2.0,
    )

    assert accepted.combined_standard_uncertainty == pytest.approx(2**-0.5)
    assert accepted.expanded_validation_uncertainty == pytest.approx(2**0.5)
    assert accepted.normalized_error == pytest.approx(2**-0.5)
    assert accepted.accepted is True
    assert rejected.accepted is False
    assert rejected.to_dict()["relative_error"] == pytest.approx(0.02)


def test_validation_point_rejects_invalid_uncertainty_and_handles_zero_reference():
    exact = assess_validation_point(
        0.0,
        0.0,
        numerical_standard_uncertainty=0.0,
        input_standard_uncertainty=0.0,
        experimental_standard_uncertainty=0.0,
    )
    assert exact.accepted is True
    assert exact.relative_error is None
    assert exact.normalized_error == 0.0
    with pytest.raises(ValueError, match="non-negative"):
        assess_validation_point(
            1.0,
            1.0,
            numerical_standard_uncertainty=-0.1,
            input_standard_uncertainty=0.0,
            experimental_standard_uncertainty=0.0,
        )
    with pytest.raises(ValueError, match="finite number"):
        assess_validation_point(
            True,
            1.0,
            numerical_standard_uncertainty=0.0,
            input_standard_uncertainty=0.0,
            experimental_standard_uncertainty=0.0,
        )


def test_grid_convergence_recovers_second_order_sequence():
    result = grid_convergence_index(
        (
            GridSolution(0.4, 1.08, "coarse"),
            GridSolution(0.1, 1.005, "fine"),
            GridSolution(0.2, 1.02, "medium"),
        )
    )

    assert result.observed_order == pytest.approx(2.0)
    assert result.extrapolated_value == pytest.approx(1.0)
    assert result.fine_grid_absolute_gci == pytest.approx(0.00625)
    assert result.fine_grid_relative_gci == pytest.approx(0.00625 / 1.005)
    assert result.asymptotic_ratio == pytest.approx(1.0)


def test_grid_convergence_promotion_policy_is_explicit():
    accepted = assess_grid_convergence(
        grid_convergence_index(
            GridSolution(size, 1.0 + 0.01 * size**2)
            for size in (0.4, 0.2, 0.1)
        )
    )
    rejected = assess_grid_convergence(
        grid_convergence_index(
            GridSolution(size, 1.0 + 10.0 * size**2)
            for size in (0.4, 0.2, 0.1)
        )
    )

    assert accepted["accepted"] is True
    assert rejected["accepted"] is False
    assert rejected["policy"] == GridConvergencePolicy().to_dict()


def test_grid_convergence_supports_unequal_refinement_ratios():
    result = grid_convergence_index(
        GridSolution(size, 3.0 + 0.25 * size**1.7)
        for size in (0.30, 0.15, 0.10)
    )

    assert result.observed_order == pytest.approx(1.7)
    assert result.extrapolated_value == pytest.approx(3.0)


@pytest.mark.parametrize(
    "solutions, message",
    [
        (
            (GridSolution(0.1, 1.0), GridSolution(0.2, 1.2)),
            "Exactly three",
        ),
        (
            (
                GridSolution(0.1, 1.0),
                GridSolution(0.2, 0.9),
                GridSolution(0.4, 1.2),
            ),
            "oscillatory",
        ),
    ],
)
def test_grid_convergence_rejects_invalid_studies(solutions, message):
    with pytest.raises(ValueError, match=message):
        grid_convergence_index(solutions)


def test_grid_verification_rejects_boolean_numeric_inputs():
    with pytest.raises(ValueError, match="finite number"):
        GridSolution(True, 1.0)
    with pytest.raises(ValueError, match="finite number"):
        GridConvergencePolicy(maximum_fine_relative_gci=True)
    with pytest.raises(ValueError, match="finite number"):
        grid_convergence_index(
            (
                GridSolution(0.1, 1.0),
                GridSolution(0.2, 1.1),
                GridSolution(0.4, 1.3),
            ),
            safety_factor=True,
        )
    with pytest.raises(TypeError, match="GridSolution records"):
        grid_convergence_index((GridSolution(0.1, 1.0), object(), object()))
    with pytest.raises(ValueError, match="label must be a string"):
        GridSolution(0.1, 1.0, label=True)


def _result_record(*, cells: int, value: float, model: str = "a" * 64):
    return {
        "status": "completed",
        "converged": True,
        "provenance": {"model_sha256": model},
        "quantities": {
            "mesh.cell_count": {"value": cells, "unit": "1"},
            "flow.pressure_drop": {"value": value, "unit": "Pa"},
        },
    }


def test_grid_convergence_can_be_built_from_agentcfd_result_records():
    result = grid_convergence_from_result_records(
        [
            _result_record(cells=64, value=3.56),
            _result_record(cells=512, value=1.64),
            _result_record(cells=4096, value=1.16),
        ],
        quantity="flow.pressure_drop",
    )

    assert result.observed_order == pytest.approx(2.0)
    assert result.extrapolated_value == pytest.approx(1.0)


def test_result_record_grid_convergence_rejects_mixed_models_and_unconverged_runs():
    records = [
        _result_record(cells=64, value=3.56),
        _result_record(cells=512, value=1.64),
        _result_record(cells=4096, value=1.16, model="b" * 64),
    ]
    with pytest.raises(ValueError, match="share one model"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")

    records[2] = _result_record(cells=4096, value=1.16)
    records[2]["converged"] = False
    with pytest.raises(ValueError, match="completed and converged"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")

    records[2] = _result_record(cells=4096, value=1.16)
    records[2]["quantities"]["flow.pressure_drop"]["value"] = True
    with pytest.raises(ValueError, match="must be numeric"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")

    records[2] = _result_record(cells=4096, value=1.16)
    records[2]["quantities"]["flow.pressure_drop"]["unit"] = "kPa"
    with pytest.raises(ValueError, match="units must match"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")

    records[2] = _result_record(cells=4096, value=1.16)
    records[2]["quantities"]["mesh.cell_count"]["unit"] = "cells"
    with pytest.raises(ValueError, match="must use unit '1'"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")

    with pytest.raises(ValueError, match="quantity must be a non-empty string"):
        grid_convergence_from_result_records(records, quantity=True)
    with pytest.raises(ValueError, match="must be a mapping"):
        grid_convergence_from_result_records(
            (records[0], records[1], object()),
            quantity="flow.pressure_drop",
        )
