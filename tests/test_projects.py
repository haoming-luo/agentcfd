import json
import os
import shlex
import shutil
from pathlib import Path

import jsonschema
import pytest

from agentcfd import Check, contracts, projects
from agentcfd.cli import entrypoint
from agentcfd.errors import ProjectError


def test_process_liveness_treats_permission_denied_as_existing(monkeypatch):
    def denied(_pid, _signal):
        raise PermissionError("managed process boundary")

    monkeypatch.setattr(projects.os, "kill", denied)

    assert projects._process_is_alive(12345) is True


def test_project_lifecycle_is_one_readable_agent_and_human_workflow(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)

    assert project.manifest.default_provider == "reference"
    assert project.manifest.run_mode == "replace"
    assert project.run_root == root / "output"
    assert "def build" in (root / "case.py").read_text()
    assert (root / "AGENTS.md").is_file()
    assert "output/" in (root / ".gitignore").read_text()

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
    assert (
        plan["decisions"]["output_plan"]["channels"]["field_frames"]["resolved_count"]
        == 1
    )
    assert plan["plan_sha256"].startswith("sha256:")
    jsonschema.Draft202012Validator(
        contracts.load("solution-plan.schema.json")
    ).validate(plan)

    completed = project.run()
    assert completed.result.accepted is True
    assert completed.field_bundle is None
    assert completed.plan_path.is_file()
    assert completed.result_path.is_file()
    assert (completed.directory / "run.json").is_file()
    assert "Start here" in (completed.directory / "README.md").read_text()
    assert completed.directory == root / "output"
    assert completed.mode == "replace"
    assert not (root / "__pycache__").exists()

    inspection = project.inspect()
    assert inspection["run_count"] == 1
    assert inspection["latest_run"]["run_id"] == completed.run_id
    assert inspection["latest_run"]["trust_level"] == "verified"


def test_project_replace_mode_overwrites_only_managed_output(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)
    first = project.run()
    (first.directory / "obsolete.txt").write_text("old")

    second = project.run()

    assert second.directory == first.directory == root / "output"
    assert second.run_id != first.run_id
    assert not (second.directory / "obsolete.txt").exists()
    inspection = project.inspect()
    assert inspection["run_count"] == 1
    assert inspection["latest_run"]["run_id"] == second.run_id


def test_project_replace_mode_recovers_interrupted_owned_output(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps({"schema": "agentcfd.project-run/0.1", "status": "preparing"})
    )
    (project.run_root / "partial.dat").write_text("interrupted")

    completed = project.run()

    assert completed.result.accepted is True
    assert not (project.run_root / "partial.dat").exists()


def test_project_refuses_to_replace_a_live_owned_run(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "status": "running",
                "pid": os.getpid(),
            }
        )
    )

    with pytest.raises(ProjectError, match="active AgentCFD run"):
        project.run()


def test_project_records_repairable_solver_failure(tmp_path, monkeypatch):
    project = projects.init_project(tmp_path / "pipe")

    def fail(_provider, _step):
        raise RuntimeError("synthetic solver failure")

    monkeypatch.setattr("agentcfd.projects.ReferencePipeProvider.run", fail)

    with pytest.raises(RuntimeError, match="synthetic"):
        project.run()

    record = json.loads((project.run_root / "run.json").read_text())
    assert record["status"] == "failed"
    assert record["phase"] == "solver"
    assert record["failure"]["safe_to_retry"] is True
    assert "agentcfd run" in record["failure"]["repair"]


def test_failed_openfoam_result_retains_workspace_and_guides_to_logs(
    tmp_path, monkeypatch
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    def fail(provider, _step):
        provider.case_directory.mkdir(parents=True)
        (provider.case_directory / "log.pimpleFoam").write_text(
            "Time = 0.1\nFOAM FATAL ERROR: synthetic failure\n"
        )
        return projects.SimulationResult(
            status="failed",
            converged=False,
            provider="openfoam",
            quantities={},
            checks=(),
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", fail)

    completed = project.run()
    status = project.status()

    assert completed.solver_workspace is not None
    assert completed.solver_workspace.is_dir()
    assert status["state"] == "failed"
    assert status["next_action"]["command"].startswith("agentcfd diagnose ")

    diagnosis = project.diagnose()
    jsonschema.Draft202012Validator(
        contracts.load("project-diagnosis.schema.json")
    ).validate(diagnosis)
    assert diagnosis["primary_finding"]["code"] == "OPENFOAM_FATAL_ERROR"
    assert diagnosis["observation_cost"]["field_payloads_opened"] == 0
    assert diagnosis["next_action"]["command"].startswith("agentcfd logs ")


def test_keep_workspace_persists_cleanup_protection_from_real_run_path(
    tmp_path, monkeypatch
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    project.manifest_path.write_text(
        project.manifest_path.read_text().replace(
            "export_fields = true", "export_fields = false"
        )
    )
    project = projects.Project(project.root)

    def complete(provider, _step):
        provider.case_directory.mkdir(parents=True)
        (provider.case_directory / "native-field").write_bytes(b"retained")
        return projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", complete)

    completed = project.run(keep_workspace=True)
    marker = json.loads(
        (completed.solver_workspace / ".agentcfd-workspace.json").read_text()
    )
    record = json.loads((completed.directory / "run.json").read_text())

    assert marker["retention_reason"] == "explicit-cli"
    assert marker["protected"] is True
    assert record["workspace_retention"] == {
        "retained": True,
        "reason": "explicit-cli",
        "protected_from_default_cleanup": True,
    }
    project.clean(apply=True)
    assert completed.solver_workspace.is_dir()


def test_project_logs_are_bounded_and_prefer_retained_workspace(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    run_id = "failed-run"
    project.run_root.mkdir()
    evidence = project.run_root / "evidence"
    evidence.mkdir()
    (evidence / "pimpleFoam.log").write_text("older published log\n")
    workspace = project.root / ".agentcfd" / "work" / run_id / "openfoam"
    workspace.mkdir(parents=True)
    live_log = workspace / "log.pimpleFoam"
    live_log.write_text("".join(f"line {index}\n" for index in range(200)))
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "accepted": False,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    report = project.logs(lines=3)

    jsonschema.Draft202012Validator(contracts.load("project-logs.schema.json")).validate(
        report
    )
    assert report["source"] == "workspace"
    assert report["returned_lines"] == 3
    assert report["truncated"] is True
    assert report["tail"] == "line 197\nline 198\nline 199\n"
    assert report["available_commands"] == ["pimpleFoam"]


def test_logs_cli_can_select_published_provider_evidence(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    evidence = project.run_root / "evidence"
    evidence.mkdir()
    (evidence / "checkMesh.log").write_text("Mesh OK.\nEnd\n")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "published-log",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "accepted": False,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    assert (
        entrypoint(
            [
                "logs",
                str(project.root),
                "--command",
                "checkMesh",
                "--lines",
                "1",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "published-evidence"
    assert report["tail"] == "End\n"


def test_diagnose_cli_reports_storage_failure_and_safe_preview(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    evidence = project.run_root / "evidence"
    evidence.mkdir()
    (evidence / "simpleFoam.log").write_text(
        "FOAM FATAL ERROR\nNo space left on device\n"
    )
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "failed-storage",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "accepted": False,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    assert entrypoint(["diagnose", str(project.root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["primary_finding"]["code"] == "DISK_SPACE_EXHAUSTED"
    assert report["next_action"]["command"].startswith("agentcfd clean ")


def test_project_resume_stages_identical_interrupted_checkpoint(tmp_path, monkeypatch):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    manifest = project.manifest_path.read_text()
    project.manifest_path.write_text(manifest.replace("export_fields = true", "export_fields = false"))
    project = projects.Project(project.root)
    step = project.load_step()
    plan = project.plan()
    run_id = "interrupted-run"
    source_case = project.root / ".agentcfd" / "work" / run_id / "openfoam"
    provider = projects.OpenFOAMChannelProvider(case_directory=source_case)
    provider.prepare(step)
    checkpoint = source_case / "0.5"
    checkpoint.mkdir()
    (checkpoint / "U").write_text("velocity")
    (checkpoint / "p").write_text("pressure")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "running",
                "phase": "solver",
                "pid": 99999999,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "execution_sha256": project._execution_fingerprint(
                    plan["model"]["analysis_sha256"], provider="openfoam"
                ),
                "started_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    observed = {}

    def complete(_provider, _step, **kwargs):
        archive = Path(kwargs["restart_archive"])
        observed["archive_exists"] = archive.is_file()
        observed["source_run_id"] = kwargs["source_run_id"]
        observed["analysis"] = kwargs["expected_analysis_sha256"]
        return projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", complete)

    recovery = project.recovery()
    jsonschema.Draft202012Validator(
        contracts.load("project-recovery.schema.json")
    ).validate(recovery)
    assert recovery["available"] is True
    assert recovery["source"] == "retained-workspace"
    assert recovery["coordinate"] == {"name": "time", "value": 0.5, "unit": "s"}
    preview = project.clean()
    assert preview["protected_recovery_run_ids"] == [run_id]
    assert preview["candidate_bytes"] == 0

    completed = project.resume()
    record = json.loads((project.run_root / "run.json").read_text())

    assert completed.result.accepted is True
    assert observed == {
        "archive_exists": True,
        "source_run_id": run_id,
        "analysis": plan["model"]["analysis_sha256"],
    }
    assert record["resume"]["source_run_id"] == run_id
    assert not (project.root / ".agentcfd" / "work" / run_id).exists()


def test_project_resume_refuses_changed_analysis(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "old-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "analysis_sha256": "0" * 64,
                "execution_sha256": "1" * 64,
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    with pytest.raises(projects.ProjectError, match="inputs changed"):
        project.resume()


def test_resume_identity_excludes_timeout_and_publication_only_settings(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    step = project.load_step()
    analysis = step.fingerprint()
    first = project._resume_execution_fingerprint(analysis, provider="openfoam")
    full_first = project._execution_fingerprint(analysis, provider="openfoam")
    manifest = project.manifest_path.read_text()
    project.manifest_path.write_text(
        manifest.replace("timeout_seconds = 3600", "timeout_seconds = 7200")
        .replace("export_fields = true", "export_fields = false")
        .replace("keep_workspace = false", "keep_workspace = true")
    )
    changed = projects.Project(project.root)

    assert changed._resume_execution_fingerprint(analysis, provider="openfoam") == first
    assert changed._execution_fingerprint(analysis, provider="openfoam") != full_first


def test_project_campaign_mode_preserves_current_output_and_history(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)
    current = project.run()
    archived = project.run(campaign=True)

    assert current.directory == root / "output"
    assert archived.mode == "campaign"
    assert archived.directory.parent == root / "campaigns"
    assert (current.directory / "run.json").is_file()
    assert (archived.directory / "run.json").is_file()
    assert project.inspect()["run_count"] == 2


def test_project_replace_mode_refuses_unmanaged_output(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)
    project.run_root.mkdir()
    (project.run_root / "notes.txt").write_text("user-owned")

    with pytest.raises(ProjectError, match="unmanaged output"):
        project.run()

    assert (project.run_root / "notes.txt").read_text() == "user-owned"


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
    path.write_text(
        path.read_text().replace(
            "mean_velocity_inlet(0.02)", "mean_velocity_inlet(1.0)"
        )
    )

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


def test_project_plan_rejects_accidental_full_field_frame_explosion(tmp_path):
    project = projects.init_project(tmp_path / "pipe", provider="openfoam")
    case = project.root / "case.py"
    case.write_text(
        case.read_text()
        .replace("studies.internal_flow()", "studies.internal_flow(steady=False)")
        .replace(
            "procedure=procedures.steady(),",
            "procedure=procedures.transient(end_time=10.0, initial_time_step=0.001),",
        )
        .replace(
            "output=outputs.standard(),",
            "output=outputs.animation(every=0.001, maximum_frames=100),",
        )
    )

    plan = project.plan()

    assert plan["readiness"]["ready_to_run"] is False
    issue = next(
        item for item in plan["issues"] if item["code"] == "OUTPUT_POLICY_INVALID"
    )
    assert "10000 full-field frames" in issue["message"]


def test_project_paths_cannot_escape_root(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    manifest = project.manifest_path.read_text().replace(
        'entrypoint = "case.py"',
        'entrypoint = "../case.py"',
    )
    project.manifest_path.write_text(manifest)

    with pytest.raises(ProjectError, match="escapes"):
        projects.Project(project.root)


def test_project_discovers_nearest_manifest_from_nested_directories_and_files(tmp_path):
    outer = projects.init_project(tmp_path / "outer")
    inner = projects.init_project(outer.root / "input" / "nested-project")
    deep = inner.root / "output" / "fields"
    deep.mkdir(parents=True)

    assert projects.Project(deep).root == inner.root
    assert projects.Project.discover(deep).root == inner.root
    assert projects.Project(inner.entrypoint).root == inner.root


def test_project_next_action_uses_dot_from_any_nested_working_directory(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe with spaces")
    nested = project.root / "input" / "cad"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    report = projects.Project.discover().status()

    assert report["root"] == str(project.root)
    assert shlex.split(report["next_action"]["command"]) == ["agentcfd", "run", "."]


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


def test_baffle_channel_template_selects_openfoam_and_plans_cleanly(tmp_path, capsys):
    root = tmp_path / "wake"

    assert (
        entrypoint(["init", str(root), "--template", "baffle-channel", "--json"]) == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["provider"] == "openfoam"
    plan = projects.Project(root).plan()
    assert plan["readiness"]["provider_compatible"] is True
    assert plan["decisions"]["solver"] == "pimpleFoam"
    output_plan = plan["decisions"]["output_plan"]
    assert output_plan["estimated_mesh_cells"] == 23880
    assert output_plan["estimate_calibration"]["safety_factor"] == 1.25
    assert (
        output_plan["estimated_temporary_peak_bytes"]
        >= 1.25 * output_plan["estimate_calibration"]["raw_staging_bytes"]
    )
    assert [
        item["name"]
        for item in plan["decisions"]["output_plan"]["channels"]["views"][
            "definitions"
        ]
    ] == ["midplane-vorticity", "wake-streamlines", "centerline-pressure"]


def test_baffle_channel_template_rejects_reference_provider(tmp_path):
    with pytest.raises(ValueError, match="requires provider='openfoam'"):
        projects.init_project(
            tmp_path / "wake", template="baffle-channel", provider="reference"
        )


def test_agentfem_style_run_alias_targets_current_project(tmp_path, capsys):
    root = tmp_path / "short-run"
    projects.init_project(root)

    assert entrypoint(["run", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "reference-pipe"


def test_project_status_guides_ready_complete_and_modified_workflows(tmp_path):
    project = projects.init_project(tmp_path / "pipe")

    ready = project.status()
    jsonschema.Draft202012Validator(
        contracts.load("project-status.schema.json")
    ).validate(ready)
    assert ready["state"] == "ready"
    assert ready["next_action"]["command"].startswith("agentcfd run ")
    assert ready["postprocess"]["primary"] is None

    project.run()
    complete = project.status()
    assert complete["state"] == "complete"
    assert complete["next_action"]["command"].startswith("agentcfd view ")
    assert complete["postprocess"]["result"].endswith("output/result.json")

    case = project.entrypoint
    case.write_text(
        case.read_text().replace('name="water-pipe"', 'name="water-pipe-v2"')
    )
    modified = project.status()
    assert modified["state"] == "modified"
    assert modified["inputs_changed"] is True
    assert "Inputs changed" in modified["next_action"]["reason"]


def test_project_doctor_combines_health_resource_and_energy_truthfulness(
    tmp_path, capsys
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    report = project.doctor()

    jsonschema.Draft202012Validator(
        contracts.load("project-doctor.schema.json")
    ).validate(report)
    assert report["resource_estimate"]["estimated_mesh_cells"] == 23880
    assert report["resource_estimate"]["nominal_solver_steps"] == 400
    assert report["resource_estimate"]["cell_updates_proxy"] == 19_104_000
    assert report["resource_estimate"]["energy"]["status"] == "not-measured"
    assert report["observation_cost"]["field_payloads_opened"] == 0

    expected_exit = 0 if report["healthy"] else 3
    assert entrypoint(["doctor", str(project.root), "--json"]) == expected_exit
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["resource_estimate"] == report["resource_estimate"]


def test_project_status_next_command_preserves_paths_with_spaces(tmp_path):
    project = projects.init_project(tmp_path / "pipe with spaces")

    command = project.status()["next_action"]["command"]

    assert shlex.split(command) == ["agentcfd", "run", str(project.root)]


def test_project_status_is_portable_when_a_legacy_run_directory_moves(tmp_path):
    original = projects.init_project(tmp_path / "original")
    original.run()
    record_path = original.run_root / "run.json"
    record = json.loads(record_path.read_text())
    record.pop("analysis_sha256", None)
    record.pop("execution_sha256", None)
    record_path.write_text(json.dumps(record))
    moved_root = tmp_path / "moved project"
    shutil.copytree(original.root, moved_root)

    report = projects.Project(moved_root).status()

    assert report["state"] == "complete"
    assert report["inputs_changed"] is False
    assert report["postprocess"]["result"] == str(moved_root / "output" / "result.json")


def test_project_status_detects_operational_provider_setting_changes(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    analysis_sha = plan["model"]["analysis_sha256"]
    execution_sha = project._execution_fingerprint(analysis_sha, provider="openfoam")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "prior-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": analysis_sha,
                "execution_sha256": execution_sha,
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text().replace("keep_workspace = false", "keep_workspace = true")
    )

    report = projects.Project(project.root).status()

    assert report["inputs_changed"] is True
    assert report["state"] in {"modified", "blocked"}


def test_project_status_does_not_invalidate_result_for_timeout_or_workspace_policy(
    tmp_path,
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    analysis_sha = plan["model"]["analysis_sha256"]
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "modern-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": analysis_sha,
                "execution_sha256": project._execution_fingerprint(
                    analysis_sha, provider="openfoam"
                ),
                "result_execution_sha256": project._result_execution_fingerprint(
                    analysis_sha, provider="openfoam"
                ),
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        .replace("keep_workspace = false", "keep_workspace = true")
        .replace("timeout_seconds = 3600", "timeout_seconds = 7200")
    )

    report = projects.Project(project.root).status()

    assert report["inputs_changed"] is False
    assert report["state"] == "complete"


def test_completed_result_remains_viewable_when_optional_runtime_is_absent(
    tmp_path, monkeypatch
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    analysis_sha = plan["model"]["analysis_sha256"]
    project.run_root.mkdir()
    (project.run_root / "result.json").write_text("{}")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "portable-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": analysis_sha,
                "execution_sha256": project._execution_fingerprint(
                    analysis_sha, provider="openfoam"
                ),
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr("agentcfd.projects.data_exchange.io_available", lambda: False)

    report = project.status()

    assert report["readiness"]["portable_io_available"] is False
    assert report["state"] == "complete"
    assert report["next_action"]["command"].startswith("agentcfd view ")


def test_project_status_marks_dead_in_progress_record_as_interrupted(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "interrupted",
                "directory": str(project.run_root),
                "status": "running",
                "pid": 999_999_999,
                "plan_sha256": project.plan()["plan_sha256"],
                "completed_at": None,
            }
        )
    )

    report = project.status()

    assert report["state"] == "interrupted"
    assert report["active"] is False
    assert report["next_action"]["command"].startswith("agentcfd run ")


def test_project_status_observes_live_openfoam_progress_without_field_reads(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    run_id = "live-progress"
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "running",
                "phase": "solver",
                "pid": os.getpid(),
                "started_at": "2026-09-06T00:00:00+00:00",
                "completed_at": None,
            }
        )
    )
    case = project.root / ".agentcfd" / "work" / run_id / "openfoam"
    case.mkdir(parents=True)
    (case / "0.5").mkdir()
    (case / "log.pimpleFoam").write_text(
        """Time = 0.75
Courant Number mean: 0.12 max: 0.48
smoothSolver: Solving for Ux, Initial residual = 2e-4, Final residual = 3e-7, No Iterations 2
GAMG: Solving for p, Initial residual = 8e-4, Final residual = 9e-7, No Iterations 3
"""
    )
    for name, value in {
        "agentcfd_inlet_flow": -0.01,
        "agentcfd_outlet_flow": 0.00999,
        "agentcfd_inlet_pressure": 0.25,
        "agentcfd_outlet_pressure": 0.05,
    }.items():
        monitor = case / "postProcessing" / name / "0"
        monitor.mkdir(parents=True)
        (monitor / "surfaceFieldValue.dat").write_text(
            f"# Time value\n0.5 {value}\n0.75 {value}\n"
        )

    report = project.status(include_storage=True)

    jsonschema.Draft202012Validator(
        contracts.load("project-status.schema.json")
    ).validate(report)
    progress = report["progress"]
    assert report["state"] == "running"
    assert report["next_action"]["command"].startswith("agentcfd watch ")
    assert progress["current_command"] == "pimpleFoam"
    assert progress["coordinate"] == {
        "name": "physical_time",
        "unit": "s",
        "current": 0.75,
        "target": 2.0,
        "fraction": 0.375,
    }
    assert progress["latest_residuals"]["p"]["initial"] == pytest.approx(8e-4)
    assert progress["courant_number"]["maximum"] == pytest.approx(0.48)
    assert progress["monitors"]["relative_mass_imbalance"] == pytest.approx(0.001)
    assert progress["monitors"]["pressure_drop"] == pytest.approx(200.0)
    assert progress["workspace"]["native_time_directory_count"] == 1
    assert progress["workspace"]["bytes"] > 0
    assert progress["observation_cost"]["field_payloads_opened"] == 0
    assert progress["observation_cost"]["monitor_bytes_read"] > 0
    assert progress["estimated_remaining"]["minimum_seconds"] >= 0


def test_project_status_rereads_atomic_completion_before_reporting_interrupted(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    marker = project.run_root / "run.json"
    initial = {
        "schema": "agentcfd.project-run/0.1",
        "run_id": "finishing",
        "mode": "replace",
        "directory": str(project.run_root),
        "status": "exporting",
        "phase": "portable-fields",
        "pid": 12345,
        "analysis_sha256": plan["model"]["analysis_sha256"],
        "started_at": "2026-09-06T00:00:00+00:00",
        "completed_at": None,
    }
    marker.write_text(json.dumps(initial))
    (project.run_root / "result.json").write_text("{}")

    def finish_during_probe(_pid):
        marker.write_text(
            json.dumps(
                {
                    **initial,
                    "status": "completed",
                    "phase": "complete",
                    "pid": None,
                    "accepted": True,
                    "completed_at": "2026-09-06T00:00:01+00:00",
                }
            )
        )
        return False

    monkeypatch.setattr(projects, "_process_is_alive", finish_during_probe)

    report = project.status()

    assert report["state"] == "complete"
    assert report["latest_run"]["phase"] == "complete"
    assert report["progress"] is None


def test_watch_follows_running_project_until_atomic_completion(
    tmp_path, monkeypatch, capsys
):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    marker = project.run_root / "run.json"
    initial = {
        "schema": "agentcfd.project-run/0.1",
        "run_id": "watched",
        "mode": "replace",
        "directory": str(project.run_root),
        "status": "running",
        "phase": "solver",
        "pid": os.getpid(),
        "analysis_sha256": plan["model"]["analysis_sha256"],
        "started_at": "2026-09-06T00:00:00+00:00",
        "completed_at": None,
    }
    marker.write_text(json.dumps(initial))

    def complete(_interval):
        (project.run_root / "result.json").write_text("{}")
        marker.write_text(
            json.dumps(
                {
                    **initial,
                    "status": "completed",
                    "phase": "complete",
                    "pid": None,
                    "accepted": True,
                    "completed_at": "2026-09-06T00:00:01+00:00",
                }
            )
        )

    monkeypatch.setattr("agentcfd.cli.time.sleep", complete)

    assert entrypoint(["watch", str(project.root), "--interval", "0.2", "--json"]) == 0
    snapshots = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [snapshot["state"] for snapshot in snapshots] == ["running", "complete"]
    assert snapshots[0]["next_action"]["command"].startswith("agentcfd watch ")
    assert snapshots[1]["postprocess"]["result"].endswith("output/result.json")


def test_project_status_surfaces_failed_acceptance_checks(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "result.json").write_text("{}")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "review-me",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": False,
                "failed_checks": ["mass-balance"],
                "plan_sha256": project.plan()["plan_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    report = project.status()

    assert report["state"] == "review"
    assert "mass-balance" in report["next_action"]["reason"]
    assert report["postprocess"]["primary"].endswith("result.json")


def test_storage_inventory_and_clean_preserve_results(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run()
    workspace = project.root / ".agentcfd" / "work" / "debug-run"
    workspace.mkdir(parents=True)
    (workspace / "native-field").write_bytes(b"temporary")

    inventory = project.storage()
    jsonschema.Draft202012Validator(
        contracts.load("project-storage.schema.json")
    ).validate(inventory)
    assert inventory["reclaimable_bytes"] == len(b"temporary")
    assert inventory["next_action"]["command"].endswith(" --apply")

    preview = project.clean()
    jsonschema.Draft202012Validator(
        contracts.load("project-clean.schema.json")
    ).validate(preview)
    assert preview["applied"] is False
    assert (workspace / "native-field").is_file()
    applied = project.clean(apply=True)
    assert applied["reclaimed_bytes"] == len(b"temporary")
    assert not workspace.exists()
    assert (project.run_root / "result.json").is_file()


def test_clean_protects_live_run_workspace(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "live-run",
                "status": "running",
                "pid": os.getpid(),
            }
        )
    )
    live = project.root / ".agentcfd" / "work" / "live-run"
    stale = project.root / ".agentcfd" / "work" / "stale-run"
    live.mkdir(parents=True)
    stale.mkdir()
    (live / "state").write_bytes(b"live")
    (stale / "state").write_bytes(b"stale")

    inventory = project.storage()
    assert inventory["reclaimable_bytes"] == len(b"stale")
    report = project.clean(apply=True)

    assert report["protected_active_run_ids"] == ["live-run"]
    assert live.is_dir()
    assert not stale.exists()
    assert report["reclaimed_bytes"] == len(b"stale")


def test_clean_requires_explicit_scope_to_remove_retained_workspace(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    retained = project.root / ".agentcfd" / "work" / "retained-run"
    retained.mkdir(parents=True)
    (retained / "native-field").write_bytes(b"retained")
    (retained / ".agentcfd-workspace.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.workspace/0.1",
                "run_id": "retained-run",
                "retention_reason": "explicit-cli",
                "protected": True,
            }
        )
    )

    inventory = project.storage()
    assert inventory["reclaimable_bytes"] == 0
    assert inventory["categories"]["temporary_workspaces"][
        "protected_retained_run_ids"
    ] == ["retained-run"]
    protected = project.clean(apply=True)
    assert protected["protected_retained_run_ids"] == ["retained-run"]
    assert retained.is_dir()

    preview = project.clean(include_retained=True)
    assert preview["candidate_bytes"] > 0
    removed = project.clean(apply=True, include_retained=True)
    assert removed["reclaimed_bytes"] > 0
    assert not retained.exists()


def test_status_storage_clean_and_view_cli_are_human_and_agent_friendly(
    tmp_path, capsys
):
    root = tmp_path / "pipe"
    projects.init_project(root).run()

    assert entrypoint(["status", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"
    assert entrypoint(["storage", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["managed_bytes"] > 0
    assert entrypoint(["clean", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert entrypoint(["view", str(root), "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["kind"] == "result-json"
    jsonschema.Draft202012Validator(
        contracts.load("project-view.schema.json")
    ).validate(view)


def test_status_summarizes_xdmf_without_loading_field_payload(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run()
    fields = project.run_root / "fields"
    fields.mkdir()
    (fields / "fields.xdmf").write_text("<Xdmf/>")
    (fields / "manifest.json").write_text(
        json.dumps(
            {
                "axis": {
                    "name": "time",
                    "unit": "s",
                    "physical_time": True,
                    "values": [0.1, 0.2],
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
                "storage": {"actual_portable_bytes": 1024},
            }
        )
    )

    summary = project.status()["postprocess"]["field_summary"]

    assert summary["frame_count"] == 2
    assert summary["axis"]["last"] == 0.2
    assert summary["fields"][0]["association"] == "point"
    assert summary["portable_display"] == "1.00 KiB"


def test_json_mode_returns_structured_repairable_error(tmp_path, capsys):
    missing = tmp_path / "missing"

    assert entrypoint(["status", str(missing), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(contracts.load("error.schema.json")).validate(
        payload
    )
    assert payload["schema"] == "agentcfd.error/0.1"
    assert payload["error"]["code"] == "FILE_NOT_FOUND_ERROR"
    assert payload["error"]["repair"]
