"""Discovery and loading for installed versioned JSON contracts."""

from __future__ import annotations

import json
import sysconfig
from pathlib import Path
from typing import Any


_SCHEMAS = (
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
    "postprocess-recipes.schema.json",
    "project-clean.schema.json",
    "project-diagnosis.schema.json",
    "project-doctor.schema.json",
    "project-logs.schema.json",
    "project-recovery.schema.json",
    "project-status.schema.json",
    "project-storage.schema.json",
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


__all__ = ["available", "catalog", "load", "path"]
