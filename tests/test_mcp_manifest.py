import json

import jsonschema

from agentcfd import contracts, mcp, operations
from agentcfd.cli import main


def test_mcp_manifest_is_generated_from_bounded_operations():
    report = mcp.as_dict()

    jsonschema.Draft202012Validator(
        contracts.load("mcp-manifest.schema.json")
    ).validate(report)
    assert {tool["name"] for tool in report["tools"]} == {
        operation.name for operation in operations.all()
    }
    tools = {tool["name"]: tool for tool in report["tools"]}
    assert tools["create_project"]["inputSchema"]["required"] == [
        "project",
        "template",
    ]
    assert tools["inspect_status"]["inputSchema"]["required"] == ["project"]
    assert tools["run_project"]["annotations"]["openWorldHint"] is True
    assert tools["apply_cleanup"]["annotations"]["destructiveHint"] is True
    assert report["safety"] == {
        "arbitrary_shell": False,
        "arbitrary_python": False,
        "extension_code_loaded_during_discovery": False,
        "approval_is_enforced_by": "host-and-operation-adapter",
    }


def test_mcp_project_resources_are_read_only_operation_views():
    report = mcp.as_dict()
    templates = report["resource_templates"]

    assert templates
    read_names = {
        operation.name
        for operation in operations.all()
        if operation.effect == "read" and "PROJECT" in operation.command.split()
    }
    assert {item["name"] for item in templates} == read_names
    assert all("{project}" in item["uriTemplate"] for item in templates)


def test_mcp_manifest_cli_is_machine_and_human_readable(capsys):
    assert main(["mcp-manifest", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == mcp.as_dict()

    assert main(["mcp-manifest"]) == 0
    output = capsys.readouterr().out
    assert "bounded tools" in output
    assert "transport: not bundled" in output
