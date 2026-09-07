"""Discovery and loading for installed versioned JSON contracts."""

from __future__ import annotations

import json
import sysconfig
from importlib import import_module, metadata
from pathlib import Path
from typing import Any, Iterable, Mapping


_SCHEMAS = (
    "agentcae-compatibility.schema.json",
    "analysis-request.schema.json",
    "benchmark-catalog.schema.json",
    "boundary-role-map.schema.json",
    "campaign-index.schema.json",
    "campaign-compaction.schema.json",
    "campaign-plan.schema.json",
    "campaign-promotion.schema.json",
    "campaign-request.schema.json",
    "campaign-sweep.schema.json",
    "capability-catalog.schema.json",
    "coupling-manifest.schema.json",
    "error.schema.json",
    "field-bundle.schema.json",
    "grid-convergence.schema.json",
    "geometry-inspection.schema.json",
    "inlet-direction-assessment.schema.json",
    "license-catalog.schema.json",
    "openfoam-case.schema.json",
    "openfoam-grid-study.schema.json",
    "openfoam-imported-flow-evidence.schema.json",
    "openfoam-imported-mesh-plan.schema.json",
    "openfoam-imported-mesh-result.schema.json",
    "openfoam-mesh.schema.json",
    "openfoam-precursor-map.schema.json",
    "openfoam-turbulent-wall-study.schema.json",
    "openfoam-turbulent-wall-function-study.schema.json",
    "openfoam-turbulent-model-study.schema.json",
    "openfoam-turbulent-model-sweep.schema.json",
    "parameter-set.schema.json",
    "postprocess-recipes.schema.json",
    "project-clean.schema.json",
    "project-creation-request.schema.json",
    "project-diagnosis.schema.json",
    "project-doctor.schema.json",
    "project-logs.schema.json",
    "project-initialization.schema.json",
    "project-recovery.schema.json",
    "project-snapshot.schema.json",
    "result-summary.schema.json",
    "project-status.schema.json",
    "project-storage.schema.json",
    "project-verification.schema.json",
    "project-view.schema.json",
    "result-exchange.schema.json",
    "scientific-sample.schema.json",
    "simulation-result.schema.json",
    "solution-plan.schema.json",
    "thermophysical-state.schema.json",
    "time-step-sensitivity.schema.json",
    "turbulent-precursor-grid-study.schema.json",
    "turbulent-wall-function-study.schema.json",
    "turbulent-model-study.schema.json",
    "turbulent-model-sweep.schema.json",
    "turbulent-wall-study.schema.json",
    "validation-point.schema.json",
)

_AGENTCAE_BINDINGS = (
    ("simulation-result", "result-exchange.schema.json"),
    ("scientific-sample", "scientific-sample.schema.json"),
    ("field-bundle", "field-bundle.schema.json"),
    ("coupling-manifest", "coupling-manifest.schema.json"),
)


def available() -> tuple[str, ...]:
    """Return the stable names of contracts shipped with AgentCFD."""

    return _SCHEMAS


def path(name: str) -> Path:
    """Locate an installed or source-checkout contract without extra packages."""

    selected = str(name).strip()
    if selected not in _SCHEMAS:
        raise KeyError(f"Unknown AgentCFD contract {name!r}.")
    roots = (
        Path(sysconfig.get_path("data")) / "share" / "agentcfd" / "schemas",
        Path(__file__).resolve().parents[2] / "schemas",
    )
    for root in roots:
        candidate = root / selected
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Installed AgentCFD contract is missing: {selected}")


def load(name: str) -> dict[str, Any]:
    """Load one contract as JSON using only the standard library."""

    payload = json.loads(path(name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"AgentCFD contract must contain a JSON object: {name}")
    return payload


def catalog() -> dict[str, object]:
    """Return a machine-readable catalog with canonical schema identifiers."""

    return {
        "schema": "agentcfd.contract-catalog/0.1",
        "contracts": [
            {"name": name, "id": load(name).get("$id"), "path": str(path(name))}
            for name in available()
        ],
    }


def agentcae_contracts() -> tuple[dict[str, str | None], ...]:
    """Return the AgentCAE identities that AgentCFD currently produces.

    The identities are derived from the shipped JSON schemas, so the audit
    cannot silently diverge from the records validated by AgentCFD itself.
    """

    records = []
    for name, producer_contract in _AGENTCAE_BINDINGS:
        payload = load(producer_contract)
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"Contract properties are missing: {producer_contract}")
        schema_property = properties.get("schema")
        if not isinstance(schema_property, dict) or not isinstance(
            schema_property.get("const"), str
        ):
            raise ValueError(
                f"Contract schema identity is missing: {producer_contract}"
            )
        version_property = properties.get("schema_version")
        schema_version = None
        if version_property is not None:
            if not isinstance(version_property, dict) or not isinstance(
                version_property.get("const"), str
            ):
                raise ValueError(
                    f"Contract schema version is missing: {producer_contract}"
                )
            schema_version = version_property["const"]
        records.append(
            {
                "name": name,
                "schema": schema_property["const"],
                "schema_version": schema_version,
                "producer_contract": producer_contract,
            }
        )
    return tuple(records)


def compare_agentcae_catalog(
    observed: Iterable[Mapping[str, object]],
    *,
    package_version: str | None,
) -> dict[str, object]:
    """Compare an AgentCAE catalog with AgentCFD's emitted identities."""

    expected = agentcae_contracts()
    expected_by_name = {str(item["name"]): item for item in expected}
    observed_records: list[dict[str, str | None]] = []
    issues: list[dict[str, object]] = []
    observed_by_name: dict[str, dict[str, str | None]] = {}
    for item in observed:
        name = item.get("name")
        schema = item.get("schema")
        schema_version = item.get("schema_version")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(schema, str)
            or not schema.strip()
            or (schema_version is not None and not isinstance(schema_version, str))
        ):
            raise ValueError("AgentCAE catalog records must have valid contract headers.")
        normalized = {
            "name": name,
            "schema": schema,
            "schema_version": schema_version,
        }
        observed_records.append(normalized)
        if name in observed_by_name:
            issues.append(
                {
                    "code": "duplicate-contract",
                    "contract": name,
                    "expected": expected_by_name.get(name),
                    "actual": normalized,
                }
            )
        else:
            observed_by_name[name] = normalized

    for name, expected_record in expected_by_name.items():
        actual = observed_by_name.get(name)
        if actual is None:
            issues.append(
                {
                    "code": "missing-contract",
                    "contract": name,
                    "expected": expected_record,
                    "actual": None,
                }
            )
            continue
        expected_header = {
            "name": expected_record["name"],
            "schema": expected_record["schema"],
            "schema_version": expected_record["schema_version"],
        }
        if actual != expected_header:
            issues.append(
                {
                    "code": "contract-header-mismatch",
                    "contract": name,
                    "expected": expected_record,
                    "actual": actual,
                }
            )
    for name, actual in observed_by_name.items():
        if name not in expected_by_name:
            issues.append(
                {
                    "code": "unexpected-contract",
                    "contract": name,
                    "expected": None,
                    "actual": actual,
                }
            )

    compatible = not issues
    return {
        "schema": "agentcfd.agentcae-compatibility/0.1",
        "status": "compatible" if compatible else "incompatible",
        "compatible": compatible,
        "agentcae_version": package_version,
        "expected_contracts": list(expected),
        "observed_contracts": sorted(observed_records, key=lambda item: item["name"]),
        "issues": issues,
        "next_action": None
        if compatible
        else {
            "command": 'python -m pip install --upgrade "agentcfd[interop]"',
            "reason": "Install matching AgentCFD and AgentCAE contract versions.",
        },
    }


def agentcae_compatibility() -> dict[str, object]:
    """Audit the optional installed AgentCAE package without requiring it."""

    try:
        package_version = metadata.version("agentcae")
    except metadata.PackageNotFoundError:
        return {
            "schema": "agentcfd.agentcae-compatibility/0.1",
            "status": "not-installed",
            "compatible": None,
            "agentcae_version": None,
            "expected_contracts": list(agentcae_contracts()),
            "observed_contracts": [],
            "issues": [
                {
                    "code": "agentcae-not-installed",
                    "contract": None,
                    "expected": None,
                    "actual": None,
                }
            ],
            "next_action": {
                "command": 'python -m pip install "agentcfd[interop]"',
                "reason": "Install the optional neutral contract package before auditing.",
            },
        }

    try:
        agentcae_contracts_module = import_module("agentcae.contracts")
        available_contracts = agentcae_contracts_module.available()
        observed = [item.summary() for item in available_contracts]
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        return {
            "schema": "agentcfd.agentcae-compatibility/0.1",
            "status": "unavailable",
            "compatible": False,
            "agentcae_version": package_version,
            "expected_contracts": list(agentcae_contracts()),
            "observed_contracts": [],
            "issues": [
                {
                    "code": "agentcae-catalog-unavailable",
                    "contract": None,
                    "expected": None,
                    "actual": {"error": str(exc)},
                }
            ],
            "next_action": {
                "command": 'python -m pip install --force-reinstall "agentcfd[interop]"',
                "reason": "Restore an importable AgentCAE contract catalog.",
            },
        }
    return compare_agentcae_catalog(observed, package_version=package_version)


__all__ = [
    "agentcae_compatibility",
    "agentcae_contracts",
    "available",
    "catalog",
    "compare_agentcae_catalog",
    "load",
    "path",
]
