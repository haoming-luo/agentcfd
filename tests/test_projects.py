import json
from pathlib import Path

import jsonschema
import pytest

from agentcfd import contracts, projects
from agentcfd.cli import entrypoint
from agentcfd.errors import ProjectError


def test_project_lifecycle_is_one_readable_agent_and_human_workflow(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)

    assert project.manifest.default_provider == "reference"
    assert "def build" in (root / "case.py").read_text()
    assert (root / "AGENTS.md").is_file()

    plan = project.plan()
    assert plan["readiness"] == {
        "model_valid": True,
        "provider_compatible": True,
        "runtime_available": True,
        "portable_io_available": True,
        "ready_to_run": True,
    }
    assert plan["decisions"]["solver"] == "Hagen-Poiseuille"
    assert plan["decisions"]["portable_formats"] == []
    assert plan["plan_sha256"].startswith("sha256:")
    jsonschema.Draft202012Validator(contracts.load("solution-plan.schema.json")).validate(
        plan
    )

    completed = project.run()
    assert completed.result.accepted is True
    assert completed.field_bundle is None
    assert completed.plan_path.is_file()
    assert completed.result_path.is_file()
    assert (completed.directory / "run.json").is_file()

    inspection = project.inspect()
    assert inspection["run_count"] == 1
    assert inspection["latest_run"]["run_id"] == completed.run_id
    assert inspection["latest_run"]["trust_level"] == "verified"


def test_project_init_refuses_to_overwrite_user_directory(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    (root / "notes.txt").write_text("preserve")

    with pytest.raises(FileExistsError, match="not empty"):
        projects.init_project(root)

    assert (root / "notes.txt").read_text() == "preserve"


def test_project_plan_returns_addressable_physics_issue(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    path = project.root / "case.py"
    path.write_text(path.read_text().replace("mean_velocity_inlet(0.02)", "mean_velocity_inlet(1.0)"))

    plan = project.plan()

    assert plan["readiness"]["ready_to_run"] is False
    issue = next(
        issue
        for issue in plan["issues"]
        if issue["code"] == "REFERENCE_REYNOLDS_OUT_OF_RANGE"
    )
    assert issue["severity"] == "error"
    assert "OpenFOAM" in issue["repair"]

    with pytest.raises(ProjectError, match="not ready"):
        project.run()


def test_project_paths_cannot_escape_root(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    manifest = project.manifest_path.read_text().replace(
        'entrypoint = "case.py"',
        'entrypoint = "../case.py"',
    )
    project.manifest_path.write_text(manifest)

    with pytest.raises(ProjectError, match="escapes"):
        projects.Project(project.root)


def test_project_cli_init_check_run_and_inspect(tmp_path, capsys):
    root = tmp_path / "cli-pipe"

    assert entrypoint(["init", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "reference"
    assert entrypoint(["check", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert entrypoint(["run", "project", str(root), "--json"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["accepted"] is True
    assert Path(run["result"]).is_file()
    assert entrypoint(["inspect", str(root), "--json"]) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection["run_count"] == 1


def test_agentfem_style_run_alias_targets_current_project(tmp_path, capsys):
    root = tmp_path / "short-run"
    projects.init_project(root)

    assert entrypoint(["run", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "reference-pipe"
