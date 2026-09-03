import json
from pathlib import Path

import pytest

from agentcfd import properties


def test_coolprop_property_provider_returns_auditable_si_state(monkeypatch):
    values = {
        "D": 0.4409206436,
        "V": 1.73e-5,
        "C": 1981.5422966,
        "L": 0.0360,
        "A": 548.0,
        "Prandtl": 0.952,
    }

    def props_si(output, pressure_name, pressure, temperature_name, temperature, fluid):
        assert (pressure_name, pressure) == ("P", 101325.0)
        assert (temperature_name, temperature) == ("T", 500.0)
        assert fluid == "IF97::Water"
        return values[output]

    def phase_si(*args):
        return "gas"

    monkeypatch.setattr(properties, "_coolprop_api", lambda: (props_si, phase_si))
    monkeypatch.setattr(
        properties.CoolPropPropertyProvider,
        "descriptor",
        lambda self: properties.ProviderDescriptor(
            name="coolprop-properties",
            version="test",
            license="MIT",
            available=True,
            execution_boundary="optional-in-process-library",
            capabilities=("properties.pressure-temperature-state",),
        ),
    )

    state = properties.CoolPropPropertyProvider().at_pressure_temperature(
        "IF97::Water",
        pressure=101325,
        temperature=500,
    )

    assert state.backend == "IF97"
    assert state.phase == "gas"
    assert state.density == pytest.approx(values["D"])
    assert state.provider_version == "test"
    assert state.to_dict()["pressure"] == 101325.0


def test_coolprop_property_provider_rejects_invalid_or_failed_states(monkeypatch):
    provider = properties.CoolPropPropertyProvider()
    with pytest.raises(ValueError, match="Absolute pressure"):
        provider.at_pressure_temperature("Water", pressure=0.0, temperature=300.0)

    monkeypatch.setattr(
        properties,
        "_coolprop_api",
        lambda: (
            lambda *args: (_ for _ in ()).throw(RuntimeError("out of range")),
            lambda *args: "liquid",
        ),
    )
    monkeypatch.setattr(
        properties.CoolPropPropertyProvider,
        "descriptor",
        lambda self: properties.ProviderDescriptor(
            name="coolprop-properties",
            version="test",
            license="MIT",
            available=True,
            execution_boundary="optional-in-process-library",
            capabilities=(),
        ),
    )
    with pytest.raises(ValueError, match="could not evaluate density"):
        provider.at_pressure_temperature("Water", pressure=101325.0, temperature=300.0)


def test_frozen_if97_runtime_evidence_matches_upstream_reference_values():
    path = Path(__file__).parents[1] / "docs" / "coolprop-if97-validation.json"
    evidence = json.loads(path.read_text())

    assert evidence["provider"] == {
        "name": "coolprop-properties",
        "version": "8.0.0",
        "license": "MIT",
        "backend": "IF97",
    }
    assert evidence["outputs"]["density_kg_per_m3"] == pytest.approx(0.4409206435977277)
    assert evidence["outputs"]["specific_heat_J_per_kg_K"] == pytest.approx(
        1981.5422965970472
    )
    assert all(check["status"] == "passed" for check in evidence["checks"])
