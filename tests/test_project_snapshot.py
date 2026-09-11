from __future__ import annotations

import json

import jsonschema

import agentcfd
from agentcfd import contracts, projects
from agentcfd.cli import entrypoint


def _validate(report: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(
        contracts.load("project-snapshot.schema.json")
    ).validate(report)


def test_project_snapshot_is_one_safe_surface_before_and_after_run(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")

    ready = project.snapshot()
    _validate(ready)
    assert ready["state"] == "ready"
    assert ready["output"] is None
    assert ready["result"] is None
    assert ready["storage"] is None
    next_action = dict(ready["next_action"])
    command = next_action.pop("command")
    assert next_action == {
        "operation": "run",
        "kind": "execute",
        "arguments": [str(project.root)],
        "mutates_project": True,
        "starts_solver": True,
        "reason": "Create the first result.",
    }
    assert command.startswith("agentcfd run ")
    assert str(project.root) in command
    assert ready["observation_cost"]["field_payloads_opened"] == 0

    completed = project.run()
    published = project.snapshot(include_storage=True)
    _validate(published)
    assert published["state"] == "complete"
    assert published["output"]["run_id"] == completed.run_id
    assert published["output"]["expert_workspace"] == {
        "retained": False,
        "path": None,
        "visibility": "hidden-provider-implementation",
        "reason": "none",
    }
    assert {item["name"] for item in published["output"]["published_files"]} == {
        "guide",
        "plan",
        "run",
        "summary",
        "result",
    }
    assert published["result"]["accepted"] is True
    assert published["storage"]["categories"]["current_output"]["exists"] is True
    assert published["next_action"]["operation"] == "view"
    assert published["next_action"]["starts_solver"] is False
    assert published["observation_cost"]["compact_result_json_bytes_read"] > 0
    assert published["observation_cost"]["recursive_storage_scan"] is True

    fields = completed.directory / "fields"
    fields.mkdir()
    (fields / "fields.xdmf").write_text("<Xdmf/>", encoding="utf-8")
    (fields / "fields.h5").write_bytes(b"opaque-field-payload")
    (fields / "manifest.json").write_text(
        json.dumps(
            {
                "axis": {
                    "name": "time",
                    "unit": "s",
                    "physical_time": True,
                    "values": [0.0, 0.1],
                },
                "fields": [
                    {
                        "name": "fluid.velocity",
                        "export_name": "fluid.velocity.point",
                        "association": "point",
                        "unit": "m/s",
                        "components": ["x", "y", "z"],
                    }
                ],
                "output_selection": {"profile": "visualization"},
                "storage": {"actual_portable_bytes": 2048},
            }
        ),
        encoding="utf-8",
    )
    portable = project.snapshot(include_result=False)
    _validate(portable)
    assert portable["output"]["fields"]["xdmf"] == str(fields / "fields.xdmf")
    assert portable["output"]["fields"]["h5"] == str(fields / "fields.h5")
    assert portable["output"]["fields"]["summary"]["frame_count"] == 2
    assert portable["observation_cost"]["field_payloads_opened"] == 0


def test_public_open_project_and_cli_snapshot_work_from_nested_path(
    tmp_path, capsys
) -> None:
    project = projects.init_project(tmp_path / "pipe")
    nested = project.root / "input" / "geometry"
    nested.mkdir(parents=True)

    assert agentcfd.open_project(nested).root == project.root
    assert entrypoint(["project", str(nested), "--no-result", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    _validate(report)
    assert report["project"]["root"] == str(project.root)
    assert report["result"] is None
    assert report["next_action"]["operation"] == "run"


def test_project_snapshot_schema_is_shipped() -> None:
    assert "project-snapshot.schema.json" in contracts.available()


def test_project_actions_publish_state_cost_and_side_effect_contracts(
    tmp_path, capsys
) -> None:
    project = projects.init_project(tmp_path / "pipe")

    ready = project.actions()
    jsonschema.Draft202012Validator(
        contracts.load("project-actions.schema.json")
    ).validate(ready)
    by_operation = {action["operation"]: action for action in ready["actions"]}
    assert len(by_operation) == len(ready["actions"])
    assert ready["recommended_operation"] == "run"
    assert by_operation["run"]["available"] is True
    assert by_operation["run"]["mutates_project"] is True
    assert by_operation["run"]["starts_solver"] is True
    assert by_operation["run"]["approval"] == "solver-execution"
    assert by_operation["verify"]["available"] is False
    assert by_operation["clean"]["mutates_project"] is False
    assert by_operation["clean"]["command"].endswith(" --json")
    assert ready["observation_cost"] == {
        "field_payloads_opened": 0,
        "recursive_storage_scan": False,
    }

    project.run()
    complete = project.actions()
    complete_actions = {action["operation"]: action for action in complete["actions"]}
    assert complete["recommended_operation"] == "view"
    assert sum(action["recommended"] for action in complete["actions"]) == 1
    assert complete_actions["view"]["available"] is True
    assert complete_actions["result"]["available"] is True
    assert complete_actions["verify"]["available"] is True
    assert complete_actions["verify"]["cost"] == "full-integrity"
    assert complete_actions["archive"]["available"] is True
    assert complete_actions["archive"]["mutates_project"] is False

    assert entrypoint(["actions", str(project.root), "--json"]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report == complete
    assert "project-actions.schema.json" in contracts.available()
