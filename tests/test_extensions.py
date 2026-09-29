import json

import jsonschema
import pytest

from agentcfd import contracts, extensions
from agentcfd.cli import main


class _Distribution:
    def __init__(self, name, version):
        self.name = name
        self.version = version
        self.metadata = {"Name": name}


class _EntryPoint:
    def __init__(self, *, name, value, group, distribution, implementation=None):
        self.name = name
        self.value = value
        self.group = group
        self.dist = distribution
        self.implementation = implementation
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        return self.implementation


class _Implementation:
    def __init__(self, *, kind, name, capabilities=("example.capability",)):
        self._record = {
            "schema": "agentcfd.extension/1",
            "kind": kind,
            "name": name,
            "capabilities": list(capabilities),
        }

    def descriptor(self):
        return dict(self._record)


def _entry_points(monkeypatch, *records):
    def selected(*, group):
        return tuple(record for record in records if record.group == group)

    monkeypatch.setattr(extensions.metadata, "entry_points", selected)


def test_extension_discovery_is_descriptor_only_and_schema_valid(monkeypatch):
    implementation = _Implementation(kind="learning", name="physicsnemo")
    entry_point = _EntryPoint(
        name="physicsnemo",
        value="agentcfd_learning_physicsnemo:extension",
        group="agentcfd.learning.v1",
        distribution=_Distribution("agentcfd-learning-physicsnemo", "0.1.0"),
        implementation=implementation,
    )
    _entry_points(monkeypatch, entry_point)

    report = extensions.as_dict()

    jsonschema.Draft202012Validator(
        contracts.load("extension-catalog.schema.json")
    ).validate(report)
    assert report["summary"] == {
        "discovered": 1,
        "compatible": 1,
        "incompatible": 0,
    }
    assert report["extensions"][0]["object_ref"] == entry_point.value
    assert entry_point.load_calls == 0
    assert extensions.load("learning", "physicsnemo") is implementation
    assert entry_point.load_calls == 1


def test_incompatible_and_ambiguous_extensions_fail_closed(monkeypatch):
    old = _EntryPoint(
        name="legacy",
        value="legacy:extension",
        group="agentcfd.providers",
        distribution=_Distribution("legacy-provider", "1.0"),
    )
    duplicate_a = _EntryPoint(
        name="shared",
        value="first:extension",
        group="agentcfd.exporters.v1",
        distribution=_Distribution("first-exporter", "1.0"),
    )
    duplicate_b = _EntryPoint(
        name="shared",
        value="second:extension",
        group="agentcfd.exporters.v1",
        distribution=_Distribution("second-exporter", "1.0"),
    )
    _entry_points(monkeypatch, old, duplicate_a, duplicate_b)

    records = extensions.discover()
    assert {record.compatibility_code for record in records} == {
        "unsupported-api-group",
        "ambiguous-name",
    }
    with pytest.raises(RuntimeError, match="unsupported-api-group"):
        extensions.load("provider", "legacy")
    with pytest.raises(RuntimeError, match="ambiguous-name"):
        extensions.load("exporter", "shared")
    assert old.load_calls == duplicate_a.load_calls == duplicate_b.load_calls == 0


def test_loaded_extension_must_match_the_declared_interface(monkeypatch):
    wrong = _EntryPoint(
        name="physicsnemo",
        value="wrong:extension",
        group="agentcfd.learning.v1",
        distribution=_Distribution("wrong-learning", "0.1.0"),
        implementation=_Implementation(kind="provider", name="physicsnemo"),
    )
    _entry_points(monkeypatch, wrong)

    with pytest.raises(RuntimeError, match="descriptor is incompatible"):
        extensions.load("learning", "physicsnemo")
    assert wrong.load_calls == 1


def test_versioned_extension_wins_over_same_named_legacy_record(monkeypatch):
    implementation = _Implementation(kind="provider", name="external")
    current = _EntryPoint(
        name="external",
        value="current:extension",
        group="agentcfd.providers.v1",
        distribution=_Distribution("current-provider", "1.0"),
        implementation=implementation,
    )
    legacy = _EntryPoint(
        name="external",
        value="legacy:extension",
        group="agentcfd.providers",
        distribution=_Distribution("legacy-provider", "1.0"),
    )
    _entry_points(monkeypatch, current, legacy)

    assert extensions.load("provider", "external") is implementation
    assert current.load_calls == 1
    assert legacy.load_calls == 0


def test_extensions_cli_uses_same_catalog(monkeypatch, capsys):
    _entry_points(monkeypatch)

    assert main(["extensions", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "agentcfd.extension-catalog/0.1"
    assert report["summary"]["discovered"] == 0

    assert main(["extensions"]) == 0
    output = capsys.readouterr().out
    assert "0 compatible / 0 discovered" in output
    assert "No optional extensions discovered." in output
