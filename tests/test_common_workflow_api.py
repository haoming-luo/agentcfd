from pathlib import Path

import jsonschema
import pytest

from agentcfd import (
    Model,
    boundaries,
    contracts,
    fluids,
    geometry,
    initialization,
    meshing,
    outputs,
    procedures,
    studies,
)
from agentcfd.errors import ModelValidationError, UnsupportedCaseError
from agentcfd.providers import OpenFOAMProvider
from agentcfd.projects import Project
from agentcfd.results import Check, FieldRecord, History, Quantity, SimulationResult


def baffle_model() -> Model:
    domain = geometry.rectangular_channel(
        length=1.2,
        height=0.20,
        width=0.10,
    ).with_baffle(
        name="baffle",
        x=0.35,
        height=0.12,
        thickness=0.01,
        attached_to="bottom",
    )
    return Model(
        name="baffle-wake",
        study=studies.internal_flow(steady=False),
        domain=domain,
        fluid=fluids.newtonian(
            "water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.5),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
        baffle=boundaries.no_slip_wall(),
    )


def test_channel_baffle_has_stable_named_regions_and_hydraulic_diameter():
    domain = baffle_model().domain
    assert domain.surface_names == ("inlet", "outlet", "walls", "baffle")
    assert domain.hydraulic_diameter == pytest.approx(2 * 0.2 * 0.1 / 0.3)
    assert domain.to_dict()["baffles"][0]["attached_to"] == "bottom"


def test_model_requires_complete_role_compatible_boundary_coverage():
    model = baffle_model()
    model.validate()

    model._boundaries["inlet"] = boundaries.no_slip_wall()
    with pytest.raises(ModelValidationError, match="role 'inlet'"):
        model.validate()

    model = baffle_model()
    del model._boundaries["baffle"]
    with pytest.raises(ModelValidationError, match="missing: baffle"):
        model.validate()


def test_transient_step_serializes_mesh_initialization_and_reports():
    request = outputs.OutputRequest(
        fields=("fluid.velocity", "fluid.pressure", "fluid.vorticity"),
        histories=("flow.mass_balance",),
        frames=outputs.FieldFrames(
            mode="interval",
            every=0.01,
            coordinate="physical-time",
            maximum=101,
        ),
        reports=(
            outputs.probe(
                "wake-probe",
                at=(0.5, 0.05, 0.05),
                fields=("fluid.velocity", "fluid.pressure"),
                every=2,
            ),
            outputs.surface_report(
                "outlet-pressure",
                region="outlet",
                field="fluid.pressure",
                operation="area-average",
            ),
            outputs.flow_uniformity("outlet-quality", region="outlet", every=2),
            outputs.force_report(
                "baffle-drag",
                regions=("baffle",),
                direction=(1.0, 0.0, 0.0),
            ),
        ),
    )
    mesh = meshing.automatic(
        base_size=0.02,
        maximum_cells=750_000,
        local_sizing=(meshing.refine("baffle", size=0.005),),
        boundary_layers=(
            meshing.layers(
                "walls",
                "baffle",
                count=4,
                first_height=0.001,
                growth_rate=1.2,
            ),
        ),
    )
    step = baffle_model().step(
        procedure=procedures.transient(end_time=1.0, initial_time_step=0.001),
        initialization=initialization.potential_flow(),
        mesh=mesh,
        output=request,
    )

    record = step.to_dict()
    assert record["mesh"]["local_sizing"][0]["regions"] == ["baffle"]
    assert record["mesh"]["maximum_cells"] == 750_000
    assert record["initialization"]["type"] == "potential-flow"
    assert record["output"]["reports"][0]["name"] == "wake-probe"
    assert mesh.boundary_layers[0].total_thickness == pytest.approx(0.005368)
    assert len(step.fingerprint()) == 64
    assert step.fingerprint() == step.fingerprint()
    jsonschema.Draft202012Validator(
        contracts.load("analysis-request.schema.json")
    ).validate(record)

    with pytest.raises(UnsupportedCaseError, match="circular-pipe geometry"):
        OpenFOAMProvider().validate(step)


def test_mesh_and_report_region_references_fail_early():
    with pytest.raises(ValueError, match="unknown regions: missing"):
        baffle_model().step(
            procedure=procedures.transient(end_time=1.0, initial_time_step=0.01),
            mesh=meshing.automatic(
                base_size=0.02,
                local_sizing=(meshing.refine("missing", size=0.005),),
            ),
        )

    with pytest.raises(ValueError, match="cannot exceed the base size"):
        meshing.automatic(
            base_size=0.01,
            local_sizing=(meshing.refine("baffle", size=0.02),),
        )

    with pytest.raises(ValueError, match="at least 1000"):
        meshing.automatic(base_size=0.01, maximum_cells=999)

    normalized = outputs.force_report(
        "drag", regions=("baffle",), direction=(2.0, 0.0, 0.0)
    )
    assert normalized.direction == (1.0, 0.0, 0.0)

    with pytest.raises(ValueError, match="unknown regions: missing"):
        baffle_model().step(
            procedure=procedures.transient(end_time=1.0, initial_time_step=0.01),
            output=outputs.OutputRequest(
                fields=("fluid.velocity",),
                histories=(),
                reports=(
                    outputs.surface_report(
                        "bad-report",
                        region="missing",
                        field="fluid.pressure",
                    ),
                ),
            ),
        )


def test_result_queries_are_discoverable_and_check_field_association():
    result = SimulationResult(
        status="completed",
        converged=True,
        provider="test",
        quantities={"flow.pressure_drop": Quantity(12.0, "Pa")},
        histories={
            "wake.velocity": History(
                abscissa=(0.0, 1.0),
                values=(0.1, 0.2),
                unit="m/s",
            )
        },
        fields={
            "fluid.velocity.point": FieldRecord(
                unit="m/s",
                location="point",
                artifact="fields.xdmf",
            ),
            "fluid.velocity.cell": FieldRecord(
                unit="m/s",
                location="cell",
                artifact="native-fields.h5",
            ),
        },
        checks=(Check("complete", True, kind="runtime"),),
    )
    assert result.quantity("flow.pressure_drop").value == 12.0
    assert result.history("wake.velocity").values[-1] == 0.2
    assert result.field("fluid.velocity", location="point").artifact == "fields.xdmf"
    assert result.available_data()["quantities"] == ("flow.pressure_drop",)
    assert (
        result.field("fluid.velocity", location="cell").artifact == "native-fields.h5"
    )
    with pytest.raises(ValueError, match="multiple associations"):
        result.field("fluid.velocity")


def test_baffle_example_produces_executable_inspectable_plan():
    root = Path(__file__).parents[1] / "examples" / "channel_baffle_project"
    plan = Project(root).plan()
    assert plan["readiness"]["model_valid"] is True
    assert plan["readiness"]["provider_compatible"] is True
    assert plan["decisions"]["mesh_strategy"] == "intent:structured"
    assert plan["decisions"]["solver"] == "pimpleFoam"
    assert plan["decisions"]["required_capability"] == (
        "openfoam.transient-laminar-baffled-channel"
    )
    assert plan["decisions"]["output_plan"]["estimated_mesh_cells"] == 23880
    assert (
        plan["decisions"]["output_plan"]["channels"]["reports"]["retention"]
        == "all compact samples"
    )
