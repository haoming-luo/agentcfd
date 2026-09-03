import hashlib
import json

import pytest

from agentcfd import Model, boundaries, fluids, geometry, interoperability, studies


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
    assert payload["schema"] == "agentcfd.simulation-result/0.1"
    assert payload["accepted"] is True

    sample = result.to_sample(
        parameters={"diameter": 0.05},
        responses=("flow.pressure_drop",),
    )
    assert sample["schema"] == "agentcae.scientific-sample/0.1"
    assert sample["source"] == "agentcfd"
    assert sample["responses"]["flow.pressure_drop"]["unit"] == "Pa"


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
