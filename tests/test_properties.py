import json
from pathlib import Path

import jsonschema
import pytest

from agentcfd import contracts, properties


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
    assert state.schema == "agentcfd.thermophysical-state"
    jsonschema.Draft202012Validator(
        contracts.load("thermophysical-state.schema.json")
    ).validate(state.to_dict())


def test_coolprop_property_provider_rejects_invalid_or_failed_states(monkeypatch):
    provider = properties.CoolPropPropertyProvider()
    with pytest.raises(ValueError, match="fluid must be a non-empty string"):
        provider.at_pressure_temperature(None, pressure=101325.0, temperature=300.0)
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


def test_thermophysical_state_rejects_ambiguous_manual_records():
    values = dict(
        fluid="Water",
        backend="default",
        pressure=101325.0,
        temperature=300.0,
        phase="liquid",
        density=998.0,
        dynamic_viscosity=1.0e-3,
        specific_heat=4180.0,
        thermal_conductivity=0.6,
        speed_of_sound=1480.0,
        prandtl_number=7.0,
        provider="test",
        provider_version="1",
    )
    with pytest.raises(ValueError, match="density"):
        properties.ThermophysicalState(**{**values, "density": True})
    with pytest.raises(ValueError, match="phase must be a non-empty string"):
        properties.ThermophysicalState(**{**values, "phase": ""})


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
