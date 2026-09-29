import jsonschema
import pytest

from agentcfd import (
    Model,
    boundaries,
    contracts,
    fluids,
    geometry,
    outputs,
    procedures,
    providers,
    studies,
)


def _pipe_step():
    model = Model(
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=2.0, diameter=0.1),
        fluid=fluids.newtonian(
            "water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.01),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    return model.step(output=outputs.standard())


def _channel_step():
    domain = geometry.rectangular_channel(
        length=1.2,
        height=0.2,
        width=0.1,
    ).with_baffle(x=0.35, height=0.12, thickness=0.01)
    model = Model(
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
    return model.step(
        procedure=procedures.transient(end_time=1.0, initial_time_step=0.01),
        output=outputs.animation(every=0.1, maximum_frames=11),
    )


def test_provider_catalog_is_versioned_and_schema_valid():
    report = providers.as_dict()

    jsonschema.Draft202012Validator(
        contracts.load("provider-catalog.schema.json")
    ).validate(report)
    assert providers.ids() == ("reference", "openfoam")
    assert [item["name"] for item in report["providers"]] == list(providers.ids())
    openfoam = providers.get("openfoam")
    assert openfoam.license == "GPL-3.0-or-later (external program)"
    assert "container_image" in openfoam.option_contract.accepted
    assert "export_fields" in openfoam.option_contract.accepted


def test_provider_option_contract_rejects_unknown_and_wrong_typed_values():
    contract = providers.get("openfoam").option_contract

    issues = contract.issues(
        {
            "cross_section_cell": 8,
            "export_fields": "yes",
            "timeout_seconds": float("inf"),
        }
    )
    assert [issue["code"] for issue in issues] == [
        "ACFD-PROVIDER-OPTION-001",
        "ACFD-PROVIDER-OPTION-002",
        "ACFD-PROVIDER-OPTION-002",
    ]
    assert issues[0]["suggestions"] == ["cross_section_cells"]
    with pytest.raises(ValueError, match="ACFD-PROVIDER-OPTION-002"):
        providers.validate_options("openfoam", {"export_fields": "yes"})


def test_one_resolver_selects_reference_pipe_and_channel_variants(tmp_path):
    reference = providers.resolve("reference", step=_pipe_step())
    pipe = providers.resolve(
        "openfoam",
        step=_pipe_step(),
        case_directory=tmp_path / "pipe",
        options={"cross_section_cells": 12, "timeout_seconds": 30.0},
    )
    channel = providers.resolve(
        "openfoam",
        step=_channel_step(),
        case_directory=tmp_path / "channel",
        options={"timeout_seconds": 30.0},
    )

    assert type(reference).__name__ == "ReferencePipeProvider"
    assert type(pipe).__name__ == "OpenFOAMProvider"
    assert pipe.mesh.cross_section_cells == 12
    assert type(channel).__name__ == "OpenFOAMChannelProvider"
    assert providers.variant("reference", step=_pipe_step()) == "hagen-poiseuille"
    assert providers.variant("openfoam", step=_pipe_step()) == "circular-pipe"
    assert providers.variant("openfoam", step=_channel_step()) == "baffled-channel"


def test_unknown_provider_fails_before_runtime_resolution():
    with pytest.raises(ValueError, match="Unknown provider"):
        providers.resolve("mystery", step=_pipe_step())
