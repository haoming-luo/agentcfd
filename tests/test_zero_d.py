import json
import math

import jsonschema
import pytest

from agentcfd import contracts, engineering, fluids, zero_d


@pytest.fixture
def water():
    return fluids.newtonian(
        "water",
        density=998.2,
        dynamic_viscosity=1.002e-3,
    )


def test_churchill_covers_laminar_and_turbulent_regimes():
    assert zero_d.churchill_friction_factor(1000.0) == pytest.approx(64.0 / 1000.0)
    churchill = zero_d.churchill_friction_factor(
        100_000.0,
        relative_roughness=0.001,
    )
    colebrook = engineering.darcy_friction_factor(
        100_000.0,
        relative_roughness=0.001,
    )
    assert churchill == pytest.approx(colebrook, rel=0.01)


def test_single_laminar_pipe_matches_hagen_poiseuille(water):
    pressure_difference = 0.1
    pipe = zero_d.CircularPipe(
        "capillary",
        "upstream",
        "downstream",
        length=1.0,
        diameter=0.01,
    )
    flow = pipe.flow_rate(pressure_difference, fluid=water)
    expected = (
        math.pi
        * pipe.diameter**4
        * pressure_difference
        / (128.0 * water.dynamic_viscosity * pipe.length)
    )
    assert flow == pytest.approx(expected, rel=2.0e-6)
    assert pipe.pressure_loss(flow, fluid=water) == pytest.approx(
        pressure_difference,
        rel=2.0e-6,
    )


def test_demand_node_solves_pressure_and_exposes_convenience_accessors(water):
    system = zero_d.network("feed-line", fluid=water)
    system.node("supply", pressure=200_000.0)
    system.node("load", volume_flow_source=-0.001)
    system.pipe(
        "feed",
        "supply",
        "load",
        length=10.0,
        diameter=0.05,
        roughness=1.0e-5,
    )

    result = system.solve().require_accepted()

    assert result.converged is True
    assert result.volume_flow_rate("feed") == pytest.approx(0.001, abs=1.0e-9)
    assert result.mass_flow_rate("feed") == pytest.approx(0.9982, abs=1.0e-6)
    assert result.pressure("load") < result.pressure("supply")
    assert result.branch("feed")["flow_regime"] == "turbulent"
    assert result.node("load")["boundary_type"] == "solved-pressure"
    with pytest.raises(KeyError, match="Unknown zero-dimensional branch"):
        result.branch("missing")


def test_equal_parallel_resistances_split_flow_equally(water):
    system = zero_d.network("parallel", fluid=water)
    system.node("supply", pressure=200_000.0)
    system.node("load", volume_flow_source=-0.002)
    system.resistance(
        "branch-a",
        "supply",
        "load",
        linear_resistance=1.0e8,
    )
    system.resistance(
        "branch-b",
        "supply",
        "load",
        linear_resistance=1.0e8,
    )

    result = system.solve().require_accepted()

    assert result.pressure("load") == pytest.approx(100_000.0)
    assert result.volume_flow_rate("branch-a") == pytest.approx(0.001)
    assert result.volume_flow_rate("branch-b") == pytest.approx(0.001)


def test_mass_flow_source_is_converted_with_declared_density(water):
    system = zero_d.network("mass-demand", fluid=water)
    system.node("supply", pressure=10_000.0)
    system.node("load", mass_flow_source=-0.9982)
    system.resistance("line", "supply", "load", linear_resistance=1.0e6)

    result = system.solve().require_accepted()

    assert result.volume_flow_rate("line") == pytest.approx(0.001)
    assert result.mass_flow_rate("line") == pytest.approx(0.9982)

    with pytest.raises(ValueError, match="either volume_flow_source"):
        zero_d.network("ambiguous", fluid=water).node(
            "load",
            volume_flow_source=0.0,
            mass_flow_source=0.0,
        )


@pytest.mark.parametrize(
    ("linear", "quadratic", "flow"),
    [
        (2.0e7, 0.0, 0.001),
        (0.0, 3.0e10, -0.002),
        (2.0e7, 3.0e10, 0.0015),
    ],
)
def test_general_resistance_round_trip(water, linear, quadratic, flow):
    branch = zero_d.FlowResistance(
        "restriction",
        "a",
        "b",
        linear_resistance=linear,
        quadratic_resistance=quadratic,
    )
    pressure = branch.pressure_loss(flow, fluid=water)
    assert branch.flow_rate(pressure, fluid=water) == pytest.approx(flow)


def test_elevation_head_drives_downhill_flow(water):
    system = zero_d.network("gravity-line", fluid=water)
    system.node("high", pressure=0.0, elevation=10.0)
    system.node("low", pressure=0.0, elevation=0.0)
    system.resistance(
        "downhill",
        "high",
        "low",
        linear_resistance=1.0e8,
    )

    result = system.solve().require_accepted()

    expected = water.density * 9.80665 * 10.0 / 1.0e8
    assert result.volume_flow_rate("downhill") == pytest.approx(expected)
    assert result.branch("downhill")["elevation_pressure_difference"][
        "value"
    ] == pytest.approx(water.density * 9.80665 * 10.0)


def test_result_contract_fingerprint_and_json_output_are_stable(tmp_path, water):
    system = zero_d.network("serializable", fluid=water, gravity=0.0)
    system.node("inlet", pressure=1000.0)
    system.node("outlet", pressure=0.0)
    system.resistance("r", "inlet", "outlet", linear_resistance=1.0e6)

    first_fingerprint = system.fingerprint()
    result = system.solve().require_accepted()
    output = result.write_json(tmp_path / "results" / "zero_d_result.json")
    model_output = system.write_json(tmp_path / "model" / "zero_d_network.json")

    payload = json.loads(output.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        contracts.load("zero-d-result.schema.json")
    ).validate(payload)
    assert payload["model"]["sha256"] == first_fingerprint
    assert system.fingerprint() == first_fingerprint
    assert json.loads(model_output.read_text(encoding="utf-8"))["schema"] == (
        "agentcfd.zero-d-network/0.1"
    )


def test_network_rejects_invalid_or_unanchored_topology(water):
    with pytest.raises(TypeError, match="NewtonianFluid"):
        zero_d.network("invalid", fluid=object())

    system = zero_d.network("unanchored", fluid=water)
    system.node("a").node("b")
    system.resistance("r", "a", "b", linear_resistance=1.0)
    with pytest.raises(ValueError, match="prescribed-pressure reference"):
        system.solve()

    missing = zero_d.network("missing", fluid=water)
    missing.node("a", pressure=0.0)
    missing.resistance("r", "a", "b", linear_resistance=1.0)
    with pytest.raises(ValueError, match="unknown nodes"):
        missing.solve()

    with pytest.raises(ValueError, match="cannot also prescribe"):
        zero_d.Node("bad", pressure=0.0, volume_flow_source=1.0)
    with pytest.raises(ValueError, match="already defined"):
        zero_d.network("duplicate", fluid=water).node("a").node("a")
    with pytest.raises(ValueError, match="friction_model"):
        zero_d.CircularPipe(
            "p",
            "a",
            "b",
            length=1.0,
            diameter=0.1,
            friction_model="unknown",
        )
