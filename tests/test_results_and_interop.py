import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from agentcfd import Check, Model, Quantity, SimulationResult, boundaries, fluids, geometry, interoperability, studies


def test_all_published_json_schemas_are_valid():
    schema_root = Path(__file__).parents[1] / "schemas"
    for path in schema_root.glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))


def pipe_model() -> Model:
    return Model(
        name="test-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=10.0, diameter=0.05),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.02),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def test_result_round_trip_and_learning_sample(tmp_path):
    result = pipe_model().step().run()
    path = result.write(tmp_path / "result.json")
    payload = json.loads(path.read_text())
    assert payload["schema"] == "agentcfd.simulation-result"
    assert payload["schema_version"] == "0.1.0"
    assert payload["accepted"] is True
    assert payload["trust_level"] == "verified"
    assert payload["scientific_inputs"]["complete"] is True
    assert len(payload["scientific_inputs"]["fingerprint"]) == 71
    assert payload["history_records"][0]["abscissa_name"] == "radius"

    sample = result.to_sample(
        parameters={"diameter": 0.05},
        responses=("flow.pressure_drop",),
    )
    assert sample["schema"] == "agentcae.scientific-sample"
    assert sample["schema_version"] == "0.1.0"
    assert sample["source"]["product"] == "agentcfd"
    assert isinstance(sample["outputs"]["flow.pressure_drop"], float)
    assert sample["quantity_schema"][0]["unit"] == "Pa"
    assert sample["trust_level"] == "verified"

    exchange = result.to_exchange(include_histories=True)
    assert exchange["schema"] == "agentcae.simulation-result"
    assert exchange["source"]["product"] == "agentcfd"

    schema_root = Path(__file__).parents[1] / "schemas"
    jsonschema.Draft202012Validator(
        json.loads((schema_root / "simulation-result.schema.json").read_text())
    ).validate(payload)
    jsonschema.Draft202012Validator(
        json.loads((schema_root / "scientific-sample.schema.json").read_text())
    ).validate(sample)
    jsonschema.Draft202012Validator(
        json.loads((schema_root / "result-exchange.schema.json").read_text())
    ).validate(exchange)


def test_execution_acceptance_and_trust_are_separate():
    result = SimulationResult(
        status="completed",
        converged=True,
        provider="test",
        quantities={"flow.pressure_drop": Quantity(1.0, "Pa")},
        checks=(Check("process", True, kind="runtime"),),
    )
    assert result.accepted is True
    assert result.trust_level == "converged"
    with pytest.raises(RuntimeError, match="verified"):
        result.require_trust("verified")


def test_cfd_to_fem_manifest_is_explicit_and_versioned():
    model_digest = hashlib.sha256(b"model").hexdigest()
    mesh_digest = hashlib.sha256(b"mesh").hexdigest()
    manifest = interoperability.fluid_loads_to_solid(
        interface="pipe-wall",
        source_model_sha256=model_digest,
        target="agentfem:steam-pipe",
        coordinate_frame="plant-global",
        mesh_sha256=mesh_digest,
    )
    payload = manifest.to_dict()
    assert payload["schema"] == "agentcae.coupling-manifest/0.1"
    assert {item["name"] for item in payload["fields"]} == {
        "fluid.pressure",
        "solid.traction",
        "thermal.temperature",
    }
    assert all(item["direction"] == "cfd-to-fem" for item in payload["fields"])


def test_manifest_rejects_unstable_identity():
    with pytest.raises(ValueError, match="SHA-256"):
        interoperability.CouplingManifest(
            interface="wall",
            source_model_sha256="not-a-digest",
            target="agentfem:model",
            coordinate_frame="global",
            mesh_sha256="0" * 64,
            fields=(
                interoperability.ExchangeField(
                    "fluid.pressure", "cfd-to-fem", "facet", "Pa", conservative=True
                ),
            ),
        )
