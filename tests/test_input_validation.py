import math

import pytest

from agentcfd import Model, Step, boundaries, fluids, geometry, outputs, procedures, studies
from agentcfd.errors import ModelValidationError
from agentcfd.providers import OpenFOAMMeshControls, OpenFOAMProvider


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_public_physical_inputs_reject_non_finite_values(invalid):
    constructors = (
        lambda: boundaries.mass_flow_inlet(invalid),
        lambda: boundaries.mean_velocity_inlet(invalid),
        lambda: boundaries.fully_developed_velocity_inlet(invalid),
        lambda: boundaries.pressure_outlet(invalid),
        lambda: boundaries.no_slip_wall(roughness=invalid),
        lambda: geometry.circular_pipe(length=invalid, diameter=0.1),
        lambda: geometry.circular_pipe(length=1.0, diameter=invalid),
        lambda: fluids.newtonian(
            "water",
            density=invalid,
            dynamic_viscosity=1.0e-3,
        ),
        lambda: fluids.newtonian(
            "water",
            density=1000.0,
            dynamic_viscosity=invalid,
        ),
        lambda: procedures.steady(relative_tolerance=invalid),
        lambda: OpenFOAMProvider(timeout_seconds=invalid),
    )
    for constructor in constructors:
        with pytest.raises(ValueError, match="finite|positive"):
            constructor()


@pytest.mark.parametrize("invalid", [True, 2.5, "8"])
def test_discrete_solver_controls_require_actual_integers(invalid):
    with pytest.raises(ValueError, match="integer"):
        procedures.steady(maximum_iterations=invalid)
    with pytest.raises(ValueError, match="integer"):
        OpenFOAMMeshControls(cross_section_cells=invalid)


def test_numeric_inputs_are_normalized_for_stable_serialization():
    pipe = geometry.circular_pipe(length=2, diameter=1, roughness=0)
    fluid = fluids.newtonian("water", density=1000, dynamic_viscosity=1)
    inlet = boundaries.mean_velocity_inlet(2)

    assert pipe.to_dict()["length"] == 2.0
    assert fluid.to_dict()["density"] == 1000.0
    assert inlet.to_dict()["velocity"] == 2.0


def test_model_rejects_unknown_boundaries_and_unstable_metadata():
    model = Model(
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=1.0, diameter=0.1),
        fluid=fluids.newtonian("water", density=1000.0, dynamic_viscosity=0.001),
        metadata={"source": math.nan},
    )
    with pytest.raises(TypeError, match="unsupported condition"):
        model.boundaries(extra=object())
    model.boundaries(
        inlet=boundaries.mean_velocity_inlet(0.01),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    with pytest.raises(ModelValidationError, match="finite, JSON-serializable"):
        model.fingerprint()

    model.metadata = {"nested": {1: "ambiguous"}}
    with pytest.raises(ModelValidationError, match="must be a string"):
        model.fingerprint()


def test_study_flags_and_output_names_are_runtime_validated():
    with pytest.raises(ValueError, match="steady must be a boolean"):
        studies.internal_flow(steady=1)
    with pytest.raises(ValueError, match="duplicates"):
        outputs.OutputRequest(
            fields=("fluid.velocity", "fluid.velocity"),
            histories=(),
        )


@pytest.mark.parametrize("invalid_name", [None, True, 1, ""])
def test_physical_asset_names_require_non_empty_strings(invalid_name):
    with pytest.raises(ValueError, match="Pipe name must be a non-empty string"):
        geometry.circular_pipe(length=1.0, diameter=0.1, name=invalid_name)
    with pytest.raises(ValueError, match="Fluid name must be a non-empty string"):
        fluids.newtonian(
            invalid_name,
            density=1000.0,
            dynamic_viscosity=0.001,
        )


def test_model_and_step_components_are_runtime_typed():
    pipe = geometry.circular_pipe(length=1.0, diameter=0.1)
    water = fluids.newtonian("water", density=1000.0, dynamic_viscosity=0.001)
    study = studies.internal_flow()
    with pytest.raises(TypeError, match="Model study"):
        Model(study={}, domain=pipe, fluid=water)
    model = Model(study=study, domain=pipe, fluid=water)
    with pytest.raises(TypeError, match="Step procedure"):
        Step(model=model, procedure={}, output=outputs.standard())
