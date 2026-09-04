from dataclasses import replace

import pytest

from agentcfd.verification import (
    GridConvergencePolicy,
    GridConvergenceResult,
    GridSolution,
    assess_turbulent_model_study,
    assess_turbulent_model_sweep,
    assess_turbulent_precursor_grid_study,
    assess_turbulent_wall_function_study,
    assess_turbulent_wall_study,
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
    converging_result = grid_convergence_index(
        GridSolution(size, 1.0 + 0.01 * size**2)
        for size in (0.4, 0.2, 0.1)
    )
    accepted = assess_grid_convergence(converging_result)
    rejected = assess_grid_convergence(
        grid_convergence_index(
            GridSolution(size, 1.0 + 10.0 * size**2)
            for size in (0.4, 0.2, 0.1)
        )
    )

    assert accepted["accepted"] is True
    assert rejected["accepted"] is False
    assert rejected["policy"] == GridConvergencePolicy().to_dict()
    forged_nonconverging = assess_grid_convergence(
        replace(converging_result, converging=False)
    )
    assert forged_nonconverging["accepted"] is False
    assert forged_nonconverging["checks"][-1]["name"] == "monotonic-convergence"


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


def test_gci_result_records_reject_forged_numeric_state():
    valid = dict(
        observed_order=2.0,
        extrapolated_value=1.0,
        fine_grid_absolute_gci=0.01,
        fine_grid_relative_gci=0.01,
        medium_grid_absolute_gci=0.04,
        asymptotic_ratio=1.0,
        refinement_ratio_fine_medium=2.0,
        refinement_ratio_medium_coarse=2.0,
        converging=True,
        safety_factor=1.25,
    )
    with pytest.raises(ValueError, match="converging must be a boolean"):
        GridConvergenceResult(**{**valid, "converging": 1})
    with pytest.raises(ValueError, match="greater than one"):
        GridConvergenceResult(
            **{**valid, "refinement_ratio_fine_medium": 1.0}
        )
    with pytest.raises(ValueError, match="finite number"):
        GridConvergenceResult(**{**valid, "extrapolated_value": float("nan")})


def _result_record(
    *,
    cells: int,
    value: float,
    model: str = "a" * 64,
    analysis: str | None = None,
):
    provenance = {"model_sha256": model}
    if analysis is not None:
        provenance["analysis_sha256"] = analysis
    return {
        "status": "completed",
        "converged": True,
        "provenance": provenance,
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


def test_result_record_grid_convergence_rejects_mixed_analysis_inputs():
    records = [
        _result_record(cells=64, value=3.56, analysis="c" * 64),
        _result_record(cells=512, value=1.64, analysis="c" * 64),
        _result_record(cells=4096, value=1.16, analysis="d" * 64),
    ]
    with pytest.raises(ValueError, match="share one analysis procedure"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")

    records[2] = _result_record(cells=4096, value=1.16)
    with pytest.raises(ValueError, match="all provide an analysis"):
        grid_convergence_from_result_records(records, quantity="flow.pressure_drop")


def _wall_study_record(*, cross: int, gradient: float, y_plus: float):
    return {
        "status": "completed",
        "converged": True,
        "accepted": True,
        "trust_level": "verified",
        "provider": "openfoam-periodic-precursor",
        "provenance": {"model_sha256": "a" * 64},
        "scientific_inputs": {
            "record": {
                "precursor": {
                    "cross_section_cells": cross,
                    "nominal_wall_cell_fraction": 0.0625,
                    "nut_wall_function": "nutUBlendedWallFunction",
                },
                "validation_policy": {"minimum_precursor_steady_samples": 50},
            }
        },
        "checks": [
            {
                "name": "pressure-gradient-tail-stability",
                "passed": True,
                "value": 1.0e-4,
            }
        ],
        "quantities": {
            "flow.pressure_gradient": {"value": gradient, "unit": "Pa/m"},
            "flow.darcy_friction_factor_relative_error": {
                "value": 0.07,
                "unit": "1",
            },
            "mesh.cell_count": {"value": 5 * cross**2, "unit": "1"},
            "mesh.maximum_aspect_ratio": {"value": 12.0, "unit": "1"},
            "mesh.nominal_wall_cell_height": {"value": 0.00165, "unit": "m"},
            "runtime.total_wall_seconds": {"value": float(cross), "unit": "s"},
            "wall.y_plus.minimum": {"value": y_plus - 5.0, "unit": "1"},
            "wall.y_plus.average": {"value": y_plus, "unit": "1"},
            "wall.y_plus.maximum": {"value": y_plus + 5.0, "unit": "1"},
        },
    }


def test_fixed_wall_cell_study_separates_wall_control_from_gci_promotion():
    records = [
        _wall_study_record(cross=8, gradient=84.76, y_plus=43.6),
        _wall_study_record(cross=16, gradient=82.81, y_plus=43.9),
        _wall_study_record(cross=32, gradient=83.16, y_plus=44.2),
    ]
    result = assess_turbulent_wall_study(records)

    assert result["acceptance"]["wall_strategy_accepted"] is True
    assert result["acceptance"]["discretization_plateau"] is True
    assert result["acceptance"]["uncertainty_promotion_accepted"] is False
    assert result["metrics"]["monotonic"] is False
    assert result["gci"]["applicable"] is False
    assert "not geometrically similar" in result["gci"]["reason"]

    records[2]["scientific_inputs"]["record"]["precursor"][
        "nominal_wall_cell_fraction"
    ] = 0.125
    with pytest.raises(ValueError, match="share one nominal"):
        assess_turbulent_wall_study(records)


def test_wall_function_study_is_identical_mesh_screening_not_default_promotion():
    functions_and_errors = (
        ("nutUBlendedWallFunction", 0.0530),
        ("nutUSpaldingWallFunction", 0.0159),
        ("nutkWallFunction", 0.0583),
    )
    records = []
    for wall_function, error in functions_and_errors:
        record = _wall_study_record(cross=16, gradient=88.0, y_plus=44.0)
        record["provenance"]["mesh_sha256"] = "b" * 64
        record["scientific_inputs"]["record"]["precursor"][
            "nut_wall_function"
        ] = wall_function
        record["quantities"].update(
            {
                "flow.reynolds_number": {
                    "value": 998.2 * 0.1 / 0.001002,
                    "unit": "1",
                },
                "flow.darcy_friction_factor": {"value": 0.018, "unit": "1"},
                "reference.flow.darcy_friction_factor": {
                    "value": 0.0183,
                    "unit": "1",
                },
            }
        )
        record["quantities"]["flow.darcy_friction_factor_relative_error"][
            "value"
        ] = error
        records.append(record)

    result = assess_turbulent_wall_function_study(records)

    assert result["recommendation"]["candidate"] == "nutUSpaldingWallFunction"
    assert result["acceptance"]["screening_accepted"] is True
    assert result["acceptance"]["default_promotion_accepted"] is False

    records[2]["provenance"]["mesh_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="share one mesh identity"):
        assess_turbulent_wall_function_study(records)


def test_turbulence_model_study_holds_non_model_inputs_and_mesh_constant():
    records = []
    for index, (model, wall_function, error, runtime) in enumerate(
        (
            ("k-omega-sst", "nutUSpaldingWallFunction", 0.0185, 18.0),
            ("k-epsilon", "nutkWallFunction", 0.0329, 17.0),
        )
    ):
        record = _wall_study_record(cross=16, gradient=88.0, y_plus=44.0)
        record["provenance"].update(
            {"model_sha256": chr(ord("a") + index) * 64, "mesh_sha256": "c" * 64}
        )
        record["scientific_inputs"]["record"].update(
            {
                "model": {
                    "name": "pipe",
                    "domain": {"diameter": 0.1, "length": 3.0},
                    "fluid": {"density": 998.2, "dynamic_viscosity": 0.001002},
                    "boundaries": {"inlet": {"velocity": 1.0}},
                    "study": {
                        "family": "internal-flow",
                        "steady": True,
                        "turbulence": model,
                        "wall_treatment": "blended-wall-functions",
                    },
                },
                "procedure": {"relative_tolerance": 1.0e-4},
            }
        )
        record["scientific_inputs"]["record"]["precursor"].update(
            {
                "turbulence_model": model,
                "nut_wall_function": wall_function,
                "maximum_iterations": 4000,
            }
        )
        record["quantities"].update(
            {
                "flow.reynolds_number": {
                    "value": 998.2 * 0.1 / 0.001002,
                    "unit": "1",
                },
                "flow.darcy_friction_factor": {"value": 0.018, "unit": "1"},
                "reference.flow.darcy_friction_factor": {
                    "value": 0.0183,
                    "unit": "1",
                },
            }
        )
        record["quantities"]["flow.darcy_friction_factor_relative_error"][
            "value"
        ] = error
        record["quantities"]["runtime.total_wall_seconds"]["value"] = runtime
        records.append(record)

    result = assess_turbulent_model_study(records)

    assert result["recommendation"]["candidate_turbulence_model"] == "k-omega-sst"
    assert result["acceptance"]["screening_accepted"] is True
    assert result["acceptance"]["default_promotion_accepted"] is False
    assert result["cases"][0]["relative_runtime_to_fastest"] == pytest.approx(18 / 17)

    records[1]["scientific_inputs"]["record"]["model"]["domain"]["diameter"] = 0.2
    with pytest.raises(ValueError, match="Reynolds number disagrees with model inputs"):
        assess_turbulent_model_study(records)


def _model_study_assessment(reynolds, sst_error, k_epsilon_error):
    return {
        "schema": "agentcfd.turbulent-model-study/0.1",
        "mesh_sha256": "c" * 64,
        "cross_section_cells": 16,
        "nominal_wall_cell_fraction": 0.0625,
        "maximum_iterations": 4000,
        "reynolds_number": reynolds,
        "cases": [
            {
                "turbulence_model": "k-omega-sst",
                "nut_wall_function": "nutUSpaldingWallFunction",
                "reynolds_number": reynolds,
                "colebrook_relative_error": sst_error,
                "runtime_wall_seconds": 18.0,
                "wall_y_plus": {"minimum": 38.0, "average": 44.0, "maximum": 48.0},
                "source_accepted": True,
            },
            {
                "turbulence_model": "k-epsilon",
                "nut_wall_function": "nutkWallFunction",
                "reynolds_number": reynolds,
                "colebrook_relative_error": k_epsilon_error,
                "runtime_wall_seconds": 16.0,
                "wall_y_plus": {"minimum": 38.0, "average": 44.0, "maximum": 48.0},
                "source_accepted": True,
            },
        ],
        "acceptance": {
            "source_results_accepted": True,
            "identical_mesh": True,
            "non_model_inputs_identical": True,
            "wall_function_y_plus_range": True,
            "screening_accepted": min(sst_error, k_epsilon_error) <= 0.02,
        },
    }


def test_turbulence_model_sweep_promotes_only_a_sampled_range_candidate():
    studies = [
        _model_study_assessment(49810.0, 0.014, 0.035),
        _model_study_assessment(99621.0, 0.0185, 0.0329),
        _model_study_assessment(199242.0, 0.028, 0.041),
        _model_study_assessment(498104.0, 0.032, 0.046),
    ]
    for index, study in enumerate(studies):
        study["mesh_sha256"] = f"{index + 1:x}" * 64
        study["nominal_wall_cell_fraction"] = (0.15, 0.08, 0.043, 0.019)[index]

    result = assess_turbulent_model_sweep(studies)

    assert result["recommendation"]["candidate_turbulence_model"] == "k-omega-sst"
    assert result["acceptance"]["consistent_candidate"] is True
    assert result["acceptance"]["range_candidate_accepted"] is True
    assert result["acceptance"]["evidence_matrix_accepted"] is True
    assert result["acceptance"]["default_promotion_accepted"] is False
    assert result["reynolds_range"]["point_count"] == 4
    assert result["mesh_strategy"] == {
        "pairwise_identical_model_mesh": True,
        "adaptive_wall_cell_fraction": True,
        "unique_mesh_count": 4,
    }

    studies[3]["reynolds_number"] = 199242.0
    with pytest.raises(ValueError, match="Reynolds numbers must be distinct"):
        assess_turbulent_model_sweep(studies)


def test_uniform_precursor_grid_study_rejects_oscillation_and_can_compute_gci():
    records = [
        _wall_study_record(cross=8, gradient=85.54, y_plus=87.0),
        _wall_study_record(cross=12, gradient=85.04, y_plus=58.0),
        _wall_study_record(cross=18, gradient=85.22, y_plus=39.0),
    ]
    for record in records:
        record["scientific_inputs"]["record"]["precursor"][
            "nominal_wall_cell_fraction"
        ] = None
    rejected = assess_turbulent_precursor_grid_study(records)
    assert rejected["acceptance"]["wall_model_consistent"] is True
    assert rejected["acceptance"]["discretization_plateau"] is True
    assert rejected["acceptance"]["uncertainty_promotion_accepted"] is False
    assert rejected["gci"]["applicable"] is False
    assert "oscillatory" in rejected["gci"]["reason"]

    for record, gradient in zip(records, (85.0, 84.4444444444, 84.1975308642)):
        record["quantities"]["flow.pressure_gradient"]["value"] = gradient
    accepted = assess_turbulent_precursor_grid_study(records)
    assert accepted["gci"]["applicable"] is True
    assert accepted["metrics"]["monotonic"] is True
