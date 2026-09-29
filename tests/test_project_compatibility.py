import json

import jsonschema

from agentcfd import compatibility, contracts, projects
from agentcfd.cli import main


def test_current_project_compatibility_is_side_effect_free(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    original_entrypoint = project.entrypoint.read_bytes()

    report = compatibility.inspect_project(project.root)

    jsonschema.Draft202012Validator(
        contracts.load("project-compatibility.schema.json")
    ).validate(report)
    assert report["status"] == "compatible"
    assert report["compatible"] is True
    assert report["can_open"] is True
    assert report["mutated"] is False
    assert project.entrypoint.read_bytes() == original_entrypoint
    assert main(["compatibility", str(project.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "compatible"


def test_legacy_newer_invalid_and_missing_projects_are_classified(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "agentcfd.toml").write_text(
        'schema = "agentcfd.project/0.0"\n', encoding="utf-8"
    )
    legacy_report = compatibility.inspect_project(legacy)
    assert legacy_report["status"] == "upgrade_required"
    assert legacy_report["upgrade_available"] is False
    assert legacy_report["issues"][0]["code"] == "PROJECT_SCHEMA_LEGACY"

    newer = tmp_path / "newer"
    newer.mkdir()
    (newer / "agentcfd.toml").write_text(
        'schema = "agentcfd.project/0.2"\n', encoding="utf-8"
    )
    assert compatibility.inspect_project(newer)["status"] == "unsupported_newer"

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "agentcfd.toml").write_text(
        'schema = "agentcfd.project/0.1"\n', encoding="utf-8"
    )
    invalid_report = compatibility.inspect_project(invalid)
    assert invalid_report["status"] == "invalid"
    assert invalid_report["can_open"] is False

    assert compatibility.inspect_project(tmp_path / "absent")["status"] == "not_found"
