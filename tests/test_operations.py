import shlex

import jsonschema
import pytest

from agentcfd import contracts, operations
from agentcfd._api_contract import CLI_COMMANDS


def test_operation_catalog_is_versioned_and_matches_public_commands():
    report = operations.as_dict()

    jsonschema.Draft202012Validator(
        contracts.load("operation-catalog.schema.json")
    ).validate(report)
    assert len(report["operations"]) == len(operations.all())
    for operation in operations.all():
        top_level_command = shlex.split(operation.command)[1]
        assert top_level_command in CLI_COMMANDS
        if operation.output_contract is not None:
            assert operation.output_contract in contracts.available()


def test_operation_risk_annotations_are_conservative():
    run = operations.get("run_project")
    cleanup = operations.get("apply_cleanup")
    status = operations.get("inspect_status")

    assert run.effect == "execute"
    assert run.retry_policy == "do-not-retry-automatically"
    assert run.approval_policy == "user-execution"
    assert run.expected_artifacts == ("run.json", "result.json", "summary.json")
    assert run.mcp_annotations == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert cleanup.destructive is True
    assert cleanup.approval_policy == "user-destructive"
    assert cleanup.retry_policy == "state-dependent"
    assert cleanup.supports_preview is True
    assert cleanup.mcp_annotations["destructiveHint"] is True
    assert status.mcp_annotations["readOnlyHint"] is True
    assert status.approval_policy == "none"
    assert status.retry_policy == "safe"
    assert status.starts_solver is False


def test_unknown_operation_fails_closed():
    with pytest.raises(ValueError, match="Unknown AgentCFD operation"):
        operations.get("arbitrary_shell")
