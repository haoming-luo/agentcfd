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
        lambda: boundaries.turbulent_mean_velocity_inlet(
            1.0,
            intensity=invalid,
            length_scale=0.01,
        ),
        lambda: boundaries.turbulent_velocity_inlet(
            (1.0, 0.0, 0.0),
            intensity=invalid,
            length_scale=0.01,
        ),
        lambda: boundaries.pressure_outlet(invalid),
        lambda: boundaries.no_slip_wall(roughness=invalid),
        lambda: boundaries.fixed_temperature(invalid),
        lambda: boundaries.heat_flux_into_fluid(invalid),
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


@pytest.mark.parametrize("invalid", [0.0, -0.1, 1.0, math.nan, math.inf, True])
def test_nominal_wall_cell_fraction_is_finite_and_fractional(invalid):
    with pytest.raises(ValueError, match="positive|finite|below one|boolean"):
        OpenFOAMMeshControls(nominal_wall_cell_fraction=invalid)


def test_numeric_inputs_are_normalized_for_stable_serialization():
    pipe = geometry.circular_pipe(length=2, diameter=1, roughness=0)
    fluid = fluids.newtonian("water", density=1000, dynamic_viscosity=1)
    inlet = boundaries.mean_velocity_inlet(2)

    assert pipe.to_dict()["length"] == 2.0
    assert fluid.to_dict()["density"] == 1000.0
    assert inlet.to_dict()["velocity"] == 2.0


def test_turbulent_inlet_is_explicit_and_fractional():
    inlet = boundaries.turbulent_mean_velocity_inlet(
        2,
        intensity=0.05,
        length_scale=0.01,
    )
    assert inlet.to_dict() == {
        "type": "turbulent-mean-velocity-inlet",
        "velocity": 2.0,
        "turbulence_intensity": 0.05,
        "turbulence_length_scale": 0.01,
    }
    with pytest.raises(ValueError, match="fraction below one"):
        boundaries.turbulent_mean_velocity_inlet(
            1.0,
            intensity=5.0,
            length_scale=0.01,
        )

    vector = boundaries.turbulent_velocity_inlet(
        (2, 0, 0),
        intensity=0.05,
        length_scale=0.01,
    )
    assert vector.to_dict() == {
        "type": "turbulent-velocity-inlet",
        "velocity": [2.0, 0.0, 0.0],
        "turbulence_intensity": 0.05,
        "turbulence_length_scale": 0.01,
    }
    assert vector.magnitude == 2.0


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
    with pytest.raises(ValueError, match="portable_profile"):
        outputs.OutputRequest(fields=("fluid.velocity",), histories=(), portable_profile="huge")
    with pytest.raises(ValueError, match="portable_formats"):
        outputs.OutputRequest(
            fields=("fluid.velocity",), histories=(), portable_formats=("npz",)
        )
    with pytest.raises(ValueError, match="requires wall_treatment"):
        studies.internal_flow(turbulence="k-omega-sst")
    with pytest.raises(ValueError, match="requires a turbulence model"):
        studies.internal_flow(wall_treatment="blended-wall-functions")
    turbulent = studies.internal_flow(
        turbulence="k-omega-sst",
        wall_treatment="blended-wall-functions",
    )
    assert turbulent.to_dict()["wall_treatment"] == "blended-wall-functions"


def test_turbulent_output_request_tracks_model_specific_dissipation_field():
    sst = outputs.turbulent_internal_flow()
    k_epsilon = outputs.turbulent_internal_flow(turbulence_model="k-epsilon")

    assert "turbulence.specific_dissipation_rate" in sst.fields
    assert "turbulence.dissipation_rate" not in sst.fields
    assert "turbulence.dissipation_rate" in k_epsilon.fields
    assert "turbulence.specific_dissipation_rate" not in k_epsilon.fields
    with pytest.raises(ValueError, match="turbulence_model"):
        outputs.turbulent_internal_flow(turbulence_model="invented")


def test_energy_intent_requires_complete_properties_boundaries_and_output():
    domain = geometry.circular_pipe(length=1.0, diameter=0.1)
    incomplete_fluid = fluids.newtonian(
        "water", density=998.2, dynamic_viscosity=0.001002
    )
    complete_fluid = fluids.newtonian(
        "water",
        density=998.2,
        dynamic_viscosity=0.001002,
        specific_heat=4180.0,
        thermal_conductivity=0.6,
    )

    missing_properties = Model(
        study=studies.internal_flow(energy=True),
        domain=domain,
        fluid=incomplete_fluid,
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.1, temperature=300.0),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(thermal=boundaries.adiabatic()),
    )
    with pytest.raises(ModelValidationError, match="specific_heat"):
        missing_properties.validate()

    missing_inlet_temperature = Model(
        study=studies.internal_flow(energy=True),
        domain=domain,
        fluid=complete_fluid,
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.1),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(thermal=boundaries.adiabatic()),
    )
    with pytest.raises(ModelValidationError, match="temperature on every inlet"):
        missing_inlet_temperature.validate()

    missing_wall_thermal = Model(
        study=studies.internal_flow(energy=True),
        domain=domain,
        fluid=complete_fluid,
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.1, temperature=300.0),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    with pytest.raises(ModelValidationError, match="thermal condition on every wall"):
        missing_wall_thermal.validate()

    model = Model(
        study=studies.internal_flow(energy=True),
        domain=domain,
        fluid=complete_fluid,
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.1, temperature=300.0),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(
            thermal=boundaries.heat_flux_into_fluid(5000.0)
        ),
    )
    model.validate()
    with pytest.raises(ValueError, match="thermal.temperature"):
        model.step(output=outputs.standard())
    step = model.step(output=outputs.thermal_internal_flow())

    assert "thermal.temperature" in step.output.fields
    assert step.model.to_dict()["boundaries"]["inlet"]["temperature"] == 300.0
    assert step.model.to_dict()["boundaries"]["wall"]["thermal"] == {
        "type": "heat-flux",
        "heat_flux_into_fluid": 5000.0,
    }


def test_thermal_boundary_intent_cannot_be_silently_ignored():
    model = Model(
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=1.0, diameter=0.1),
        fluid=fluids.newtonian(
            "water", density=998.2, dynamic_viscosity=0.001002
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.1, temperature=300.0),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(
            thermal=boundaries.fixed_temperature(350.0)
        ),
    )

    with pytest.raises(ModelValidationError, match="require study energy=True"):
        model.validate()


def test_animation_output_separates_frames_checkpoints_and_storage_budget():
    request = outputs.animation(
        every=0.05,
        maximum_frames=240,
        restart=outputs.checkpoints(every=1.0, keep=2),
        storage_budget="512 MiB",
    )

    assert request.frames.mode == "interval"
    assert request.frames.coordinate == "physical-time"
    assert request.frames.every == 0.05
    assert request.checkpoints.enabled is True
    assert request.checkpoints.keep == 2
    assert request.storage.maximum_bytes == 512 * 1024**2
    assert request.storage.compression == "gzip"
    assert request.to_dict()["checkpoints"]["enabled"] is True


@pytest.mark.parametrize("invalid", ["lots", "0 MiB", 0, True])
def test_storage_budget_rejects_ambiguous_or_nonpositive_values(invalid):
    with pytest.raises(ValueError, match="Storage budget"):
        outputs.storage(invalid)


def test_transient_study_requires_transient_procedure():
    model = Model(
        study=studies.internal_flow(steady=False),
        domain=geometry.circular_pipe(length=1.0, diameter=0.1),
        fluid=fluids.newtonian("water", density=1000.0, dynamic_viscosity=0.001),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.01),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    with pytest.raises(ValueError, match="Study and procedure disagree"):
        model.step(procedure=procedures.steady())
    step = model.step(
        procedure=procedures.transient(
            end_time=2.0,
            initial_time_step=0.001,
            maximum_time_step=0.005,
        ),
        output=outputs.animation(every=0.05),
    )
    assert step.procedure.to_dict()["type"] == "transient"


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
