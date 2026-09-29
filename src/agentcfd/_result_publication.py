"""Pure construction of compact result artifacts for project publication."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import shlex

from .errors import ProjectError
from .provenance import file_sha256


def build_result_summary(
    selected_run: Mapping[str, object],
    *,
    root: Path,
    project_argument: str,
    result_path: Path,
    record: Mapping[str, object],
    result_json_bytes_read: int,
) -> dict[str, object]:
    """Build the bounded, field-free result decision artifact."""

    available_quantities = record.get("quantities", {})
    fields = record.get("fields", {})
    histories = record.get("histories", {})
    checks = record.get("checks", [])
    if not isinstance(available_quantities, Mapping):
        raise ProjectError("Result quantities are malformed.")
    if not isinstance(fields, Mapping) or not isinstance(histories, Mapping):
        raise ProjectError("Result field or history metadata is malformed.")
    if not isinstance(checks, Sequence):
        raise ProjectError("Result checks are malformed.")
    requirements = [
        check
        for check in checks
        if isinstance(check, Mapping) and check.get("kind") == "requirement"
    ]
    failed_checks = [
        check
        for check in checks
        if isinstance(check, Mapping) and check.get("passed") is False
    ]
    failed_scientific_checks = [
        check for check in failed_checks if check.get("kind") != "requirement"
    ]
    verification_command = f"agentcfd verify result {shlex.quote(str(result_path))}"
    accepted = record.get("accepted") is True
    return {
        "schema": "agentcfd.result-summary/0.3",
        "root": str(root),
        "run_id": selected_run.get("run_id"),
        "summary": str(result_path.with_name("summary.json")),
        "result": str(result_path),
        "source_result": {
            "path": str(result_path),
            "bytes": result_path.stat().st_size,
            "sha256": file_sha256(result_path),
        },
        "status": record.get("status"),
        "converged": record.get("converged"),
        "accepted": accepted,
        "trust_level": record.get("trust_level"),
        "provider": record.get("provider"),
        "parameters": selected_run.get("parameters", {}),
        "quantities": dict(available_quantities),
        "histories": dict(histories),
        "fields": dict(fields),
        "available": {
            "quantities": sorted(available_quantities),
            "histories": sorted(histories),
            "fields": sorted(fields),
        },
        "check_count": len(checks),
        "requirements": requirements,
        "failed_checks": failed_checks,
        "scientific_inputs": record.get("scientific_inputs", {}),
        "provenance": record.get("provenance", {}),
        "artifact_integrity": {
            "verified": False,
            "reason": "External artifacts were not opened by this lightweight view.",
            "command": verification_command,
        },
        "observation_cost": {
            "summary_json_bytes_read": 0,
            "result_json_bytes_read": result_json_bytes_read,
            "field_payloads_opened": 0,
            "artifacts_hashed": 0,
        },
        "next_action": {
            "command": f"agentcfd view {project_argument}",
            "reason": (
                "Open the accepted result for spatial review."
                if accepted
                else "Review the unmet design requirements before selecting or changing the design."
                if requirements and not failed_scientific_checks
                else "Review the failed checks before using this result."
            ),
        },
    }


__all__ = ["build_result_summary"]
