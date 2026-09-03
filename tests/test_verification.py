import pytest

from agentcfd.verification import (
    GridSolution,
    grid_convergence_from_result_records,
    grid_convergence_index,
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
