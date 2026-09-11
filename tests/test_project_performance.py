from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema

from agentcfd import contracts, projects
from agentcfd.cli import entrypoint


def _validate(report: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(
        contracts.load("project-performance.schema.json")
    ).validate(report)


def test_project_performance_starts_empty_and_records_completed_runs(
    tmp_path, capsys
) -> None:
    project = projects.init_project(tmp_path / "pipe")

    empty = project.performance()
    _validate(empty)
    assert empty["history"]["status"] == "missing"
    assert empty["history"]["sample_count"] == 0
    assert empty["current"]["calibration"] is None

    completed = project.run()
    report = project.performance()
    _validate(report)
    assert report["history"]["status"] == "valid"
    assert report["history"]["sample_count"] == 1
    assert report["current"]["calibration"]["sample_count"] == 1
    assert report["recent_samples"][0]["run_id"] == completed.run_id
    assert report["recent_samples"][0]["total_seconds"] >= 0.0
    assert Path(report["history"]["path"]).is_file()
    storage = project.storage()
    assert storage["categories"]["runtime_history"]["file_count"] == 1
    assert storage["categories"]["runtime_history"]["bytes"] > 0

    assert entrypoint(["performance", str(project.root), "--json"]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    _validate(cli_report)
    assert cli_report["current"]["calibration"]["sample_count"] == 1


def test_active_progress_prefers_comparable_runtime_history(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    project.run()
    performance = project.performance()
    performance_key = performance["current"]["performance_key"]
    now = datetime.now(UTC)
    run_id = "calibrated-progress"
    project.run_root.mkdir(exist_ok=True)
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "running",
                "phase": "solver",
                "pid": 12345,
                "performance_key": performance_key,
                "started_at": (now - timedelta(milliseconds=1)).isoformat(),
                "completed_at": None,
            }
        ),
        encoding="utf-8",
    )

    progress = projects._run_progress_snapshot(
        project.root,
        json.loads((project.run_root / "run.json").read_text(encoding="utf-8")),
        project.plan(),
        include_storage=False,
    )

    assert progress is not None
    assert progress["estimated_remaining"] is not None
    assert progress["estimated_remaining"]["basis"].startswith(
        "bounded project history; 1 comparable run"
    )
    assert progress["estimated_remaining"]["maximum_seconds"] >= 1.0


def test_invalid_runtime_history_never_breaks_a_simulation(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    history = project.root / ".agentcfd" / "performance.json"
    history.parent.mkdir()
    history.write_text("not-json", encoding="utf-8")

    completed = project.run()
    report = project.performance()

    assert completed.result.status == "completed"
    _validate(report)
    assert report["history"]["status"] == "invalid"
    assert report["current"]["calibration"] is None


def test_oversized_runtime_history_is_not_read_as_calibration(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    history = project.root / ".agentcfd" / "performance.json"
    history.parent.mkdir()
    history.write_text(" " * (512 * 1024 + 1), encoding="utf-8")

    report = project.performance()

    _validate(report)
    assert report["history"]["status"] == "invalid"
    assert report["history"]["sample_count"] == 0


def test_runtime_history_is_bounded_to_fifty_samples(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    key = project.performance()["current"]["performance_key"]
    base = datetime.now(UTC)
    for index in range(55):
        started = base + timedelta(seconds=index * 2)
        project._record_performance(
            run_record={
                "run_id": f"run-{index:02d}",
                "status": "completed",
                "accepted": True,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "result_profile": "full-fields",
                "model_name": plan["model"]["name"],
                "started_at": started.isoformat(),
                "completed_at": (started + timedelta(seconds=index + 1)).isoformat(),
            },
            plan=plan,
            execution_provider="reference",
            performance_key=key,
        )

    report = project.performance()

    _validate(report)
    assert report["history"]["sample_count"] == 50
    assert report["recent_samples"][-1]["run_id"] == "run-54"
    raw = json.loads(Path(report["history"]["path"]).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        contracts.load("performance-history.schema.json")
    ).validate(raw)
    assert len(raw["samples"]) == 50
    assert raw["samples"][0]["run_id"] == "run-05"


def test_runtime_history_write_failure_is_advisory(tmp_path, monkeypatch) -> None:
    project = projects.init_project(tmp_path / "pipe")
    original_write = projects._write_json_atomic

    def fail_performance_write(path, payload):
        if path.name == "performance.json":
            raise OSError("read-only calibration store")
        return original_write(path, payload)

    monkeypatch.setattr(projects, "_write_json_atomic", fail_performance_write)

    completed = project.run()

    assert completed.result.status == "completed"
    assert not (project.root / ".agentcfd" / "performance.json").exists()


def test_runtime_history_contract_is_shipped() -> None:
    assert "project-performance.schema.json" in contracts.available()
    assert "performance-history.schema.json" in contracts.available()
