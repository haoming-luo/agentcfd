import json

from agentcfd import operations
from agentcfd.cli import main


def _invoke(capsys, operation, **arguments):
    command = operations.argv(operation, **arguments)
    assert main(list(command)) == 0
    return json.loads(capsys.readouterr().out)


def test_agent_can_complete_reference_project_from_operation_contracts(
    tmp_path,
    capsys,
):
    project = tmp_path / "contract journey"
    common = {"project": str(project)}

    created = _invoke(
        capsys,
        "create_project",
        project=str(project),
        template="industrial-pipe",
    )
    assert created["provider"] == "reference"

    checked = _invoke(capsys, "check_project", **common)
    assert checked["valid"] is True
    planned = _invoke(capsys, "plan_project", **common)
    assert planned["readiness"]["ready_to_run"] is True
    assert planned["decisions"]["provider_resolution"] == {
        "family": "reference",
        "variant": "hagen-poiseuille",
        "effective_options": {},
    }

    result = _invoke(capsys, "run_project", **common)
    assert result["accepted"] is True
    snapshot = _invoke(capsys, "inspect_project", **common)
    assert snapshot["state"] == "complete"
    compact = _invoke(capsys, "inspect_result", **common)
    assert compact["accepted"] is True
    assert compact["observation_cost"]["field_payloads_opened"] == 0
    verified = _invoke(capsys, "verify_project", **common)
    assert verified["verified"] is True

    cleanup = _invoke(capsys, "preview_cleanup", **common)
    assert cleanup["applied"] is False
    assert str(project / "output") in cleanup["preserved"]
