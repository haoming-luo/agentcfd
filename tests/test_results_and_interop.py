import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from agentcfd import Artifact, Check, Model, Quantity, SimulationResult, benchmarks, boundaries, contracts, fluids, geometry, interoperability, licensing, read_result_record, studies, verification
from agentcfd.provenance import content_fingerprint


def test_all_published_json_schemas_are_valid():
    schema_root = Path(__file__).parents[1] / "schemas"
    for path in schema_root.glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_machine_catalogs_validate_against_installed_contracts():
    for schema_name, payload in (
        ("benchmark-catalog.schema.json", benchmarks.as_dict()),
        ("license-catalog.schema.json", licensing.as_dict()),
        (
            "validation-point.schema.json",
            {
                "schema": "agentcfd.validation-point/0.1",
                **verification.assess_validation_point(
                    1.0,
                    1.0,
                    numerical_standard_uncertainty=0.0,
                    input_standard_uncertainty=0.0,
                    experimental_standard_uncertainty=0.0,
                ).to_dict(),
            },
        ),
    ):
        jsonschema.Draft202012Validator(contracts.load(schema_name)).validate(payload)


def test_scientific_fingerprint_rejects_non_string_mapping_keys():
    with pytest.raises(ValueError, match="non-string mapping key"):
        content_fingerprint({1: "ambiguous"})


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


def test_result_claims_reject_boolean_numeric_and_truthy_state_inputs():
    with pytest.raises(ValueError, match="finite number"):
        Quantity(True, "Pa")
    with pytest.raises(ValueError, match="passed must be a boolean"):
        Check("process", 1)
    with pytest.raises(ValueError, match="must not be a boolean"):
        Check("process", True, value=True)
    with pytest.raises(ValueError, match="converged must be a boolean"):
        SimulationResult(
            status="completed",
            converged=1,
            provider="test",
            quantities={"value": Quantity(1.0, None)},
            checks=(Check("process", True),),
        )
    with pytest.raises(ValueError, match="must map non-empty names to Quantity"):
        SimulationResult(
            status="completed",
            converged=True,
            provider="test",
            quantities={"value": 1.0},
            checks=(Check("process", True),),
        )
    with pytest.raises(ValueError, match="must contain Check records"):
        SimulationResult(
            status="completed",
            converged=True,
            provider="test",
            quantities={"value": Quantity(1.0, None)},
            checks=(True,),
        )
    with pytest.raises(ValueError, match="size_bytes must be an integer"):
        Artifact("evidence.txt", size_bytes=1.5)
    with pytest.raises(ValueError, match="arrays must use non-empty string names"):
        SimulationResult(
            status="completed",
            converged=True,
            provider="test",
            quantities={"value": Quantity(1.0, None)},
            checks=(Check("process", True),),
            arrays={1: [1.0]},
        )


def test_learning_sample_rejects_ambiguous_names():
    result = pipe_model().step().run()
    with pytest.raises(ValueError, match="inputs must use non-empty string names"):
        result.to_sample(inputs={1: 0.05}, outputs=("flow.pressure_drop",))
    with pytest.raises(ValueError, match="outputs must not contain duplicates"):
        result.to_sample(
            inputs={"diameter": 0.05},
            outputs=("flow.pressure_drop", "flow.pressure_drop"),
        )


def test_result_reader_verifies_derived_state_and_artifact_identity(tmp_path):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("immutable evidence")
    result = SimulationResult(
        status="completed",
        converged=True,
        provider="test",
        quantities={"flow.pressure_drop": Quantity(1.0, "Pa")},
        checks=(Check("verification", True, kind="verification"),),
        artifacts={"evidence": Artifact.from_path(evidence)},
    )
    result_path = result.write(tmp_path / "result.json")

    record = read_result_record(result_path)

    assert record["accepted"] is True
    assert record["artifact_records"]["evidence"]["path"] == "evidence.txt"
    evidence.write_text("changed evidence")
    with pytest.raises(ValueError, match="no longer matches"):
        read_result_record(result_path)


def test_result_reader_rejects_nonfinite_duplicate_and_inconsistent_claims(tmp_path):
    result_path = pipe_model().step().run().write(tmp_path / "result.json")
    original = result_path.read_text()

    result_path.write_text(original.replace('"accepted": true', '"accepted": false', 1))
    with pytest.raises(ValueError, match="accepted flag is inconsistent"):
        read_result_record(result_path)

    result_path.write_text(original.replace("{", '{"nonfinite": NaN,', 1))
    with pytest.raises(ValueError, match="non-finite JSON number NaN"):
        read_result_record(result_path)

    result_path.write_text(original.replace("{", '{"schema": "duplicate",', 1))
    with pytest.raises(ValueError, match="duplicate key 'schema'"):
        read_result_record(result_path)

    result_path.write_text(original.replace('"accepted": true', '"accepted": 1', 1))
    with pytest.raises(ValueError, match="derived state is malformed"):
        read_result_record(result_path)

    payload = json.loads(original)
    payload["quantities"]["flow.pressure_drop"]["value"] = True
    result_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="quantities are malformed"):
        read_result_record(result_path)

    payload = json.loads(original)
    payload["checks"][0]["kind"] = "certified"
    result_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="checks are malformed"):
        read_result_record(result_path)


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
    jsonschema.Draft202012Validator(
        contracts.load("coupling-manifest.schema.json")
    ).validate(payload)


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
    with pytest.raises(ValueError, match="must be a boolean"):
        interoperability.ExchangeField(
            "fluid.pressure", "cfd-to-fem", "facet", "Pa", conservative=1
        )
    field = interoperability.ExchangeField(
        "fluid.pressure", "cfd-to-fem", "facet", "Pa", conservative=True
    )
    with pytest.raises(ValueError, match="duplicate canonical names"):
        interoperability.CouplingManifest(
            interface="wall",
            source_model_sha256="1" * 64,
            target="agentfem:model",
            coordinate_frame="global",
            mesh_sha256="2" * 64,
            fields=(field, field),
        )
