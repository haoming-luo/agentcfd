import pytest

from agentcfd.verification import GridSolution, grid_convergence_index


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
