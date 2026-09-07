from __future__ import annotations

import json

import jsonschema

from agentcfd import contracts
from agentcfd.cli import main


def _observed_catalog() -> list[dict[str, str | None]]:
    return [
        {
            "name": item["name"],
            "schema": item["schema"],
            "schema_version": item["schema_version"],
        }
        for item in contracts.agentcae_contracts()
    ]


def _validate(report: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(
        contracts.load("agentcae-compatibility.schema.json")
    ).validate(report)


def test_agentcae_contracts_are_derived_from_producer_schemas() -> None:
    records = {item["name"]: item for item in contracts.agentcae_contracts()}
    assert records["simulation-result"] == {
        "name": "simulation-result",
        "schema": "agentcae.simulation-result",
        "schema_version": "0.1.0",
        "producer_contract": "result-exchange.schema.json",
    }
    assert records["scientific-sample"]["schema_version"] == "0.1.0"
    assert records["field-bundle"]["schema"] == "agentcae.field-bundle"
    assert records["coupling-manifest"]["schema_version"] is None


def test_matching_agentcae_catalog_is_compatible() -> None:
    report = contracts.compare_agentcae_catalog(
        _observed_catalog(), package_version="0.1.0a1"
    )
    assert report["status"] == "compatible"
    assert report["compatible"] is True
    assert report["issues"] == []
    assert report["next_action"] is None
    _validate(report)


def test_agentcae_catalog_drift_fails_closed() -> None:
    observed = _observed_catalog()
    observed[0]["schema_version"] = "9.0"
    observed.pop(1)
    observed.append(
        {"name": "unknown", "schema": "agentcae.unknown", "schema_version": "0.1"}
    )
    report = contracts.compare_agentcae_catalog(
        observed, package_version="9.0.0"
    )
    assert report["status"] == "incompatible"
    assert report["compatible"] is False
    assert {issue["code"] for issue in report["issues"]} == {
        "contract-header-mismatch",
        "missing-contract",
        "unexpected-contract",
    }
    assert report["next_action"]["command"].startswith("python -m pip install")
    _validate(report)


def test_missing_optional_agentcae_has_explicit_install_action(monkeypatch) -> None:
    def missing_distribution(name: str) -> str:
        assert name == "agentcae"
        raise contracts.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(contracts.metadata, "version", missing_distribution)
    report = contracts.agentcae_compatibility()
    assert report["status"] == "not-installed"
    assert report["compatible"] is None
    assert report["issues"][0]["code"] == "agentcae-not-installed"
    _validate(report)


def test_cli_can_run_explicit_agentcae_audit(monkeypatch, capsys) -> None:
    compatible = contracts.compare_agentcae_catalog(
        _observed_catalog(), package_version="0.1.0a1"
    )
    monkeypatch.setattr(contracts, "agentcae_compatibility", lambda: compatible)
    assert main(["contracts", "--check-agentcae", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agentcae_compatibility"]["status"] == "compatible"
