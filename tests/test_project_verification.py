from __future__ import annotations

import json

import jsonschema

from agentcfd import contracts, projects
from agentcfd.cli import entrypoint
from agentcfd.provenance import content_fingerprint


def _validate(report: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(
        contracts.load("project-verification.schema.json")
    ).validate(report)


def test_project_verification_checks_run_result_and_registered_artifacts(
    tmp_path, capsys
) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()

    report = project.verify()

    _validate(report)
    assert report["verified"] is True
    assert report["accepted"] is True
    assert report["trust_level"] == "verified"
    assert report["result"]["path"] == str(completed.result_path)
    assert {check["code"] for check in report["checks"]} == {
        "RUN_RECORD_INTEGRITY",
        "PLAN_INTEGRITY",
        "RESULT_INTEGRITY",
        "SUMMARY_INTEGRITY",
        "RUN_RESULT_CONSISTENCY",
    }
    assert report["field_bundle"] is None
    assert report["next_action"]["operation"] == "view"
    assert report["next_action"]["starts_solver"] is False
    assert report["observation_cost"]["field_payloads_opened"] is False
    assert report["observation_cost"]["plan_json_bytes_read"] > 0
    assert report["observation_cost"]["summary_json_bytes_read"] > 0
    assert report["observation_cost"]["control_files_hashed"] == 1

    assert entrypoint(["verify", "project", str(project.root), "--json"]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["verified"] is True


def test_project_verification_fails_closed_on_run_result_drift(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()
    run_path = completed.directory / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["accepted"] = False
    run_path.write_text(json.dumps(run), encoding="utf-8")

    report = project.verify()

    _validate(report)
    assert report["verified"] is False
    consistency = next(
        check
        for check in report["checks"]
        if check["code"] == "RUN_RESULT_CONSISTENCY"
    )
    assert consistency["passed"] is False
    assert "accepted" in consistency["message"]
    assert report["next_action"]["operation"] == "run"
    assert report["next_action"]["starts_solver"] is True


def test_project_verification_fails_closed_on_result_claim_tampering(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()
    result_path = completed.result_path
    text = result_path.read_text(encoding="utf-8")
    result_path.write_text(
        text.replace('"accepted": true', '"accepted": false', 1),
        encoding="utf-8",
    )

    report = project.verify()

    _validate(report)
    assert report["verified"] is False
    integrity = next(
        check for check in report["checks"] if check["code"] == "RESULT_INTEGRITY"
    )
    assert integrity["passed"] is False
    assert "accepted flag is inconsistent" in integrity["message"]
    assert report["accepted"] is None


def test_project_verification_accepts_reproducible_a3_analysis_identity(
    tmp_path,
) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()
    run_path = completed.directory / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run.pop("analysis_sha256")
    run.pop("summary")
    run.pop("result_sha256")
    run_path.write_text(json.dumps(run), encoding="utf-8")
    (completed.directory / "summary.json").unlink()
    plan = json.loads(completed.plan_path.read_text(encoding="utf-8"))
    decisions = plan["decisions"]
    legacy_payload = {
        "model": plan["model"]["summary"],
        "procedure": decisions["procedure"],
        "output_request": decisions["outputs"],
    }
    if decisions["initialization"] is not None:
        legacy_payload["initialization"] = decisions["initialization"]
    if decisions["mesh_intent"] is not None:
        legacy_payload["mesh"] = decisions["mesh_intent"]
    result_path = completed.result_path
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["provenance"]["analysis_sha256"] = content_fingerprint(
        legacy_payload
    ).removeprefix("sha256:")
    result_path.write_text(json.dumps(result), encoding="utf-8")

    report = project.verify()

    _validate(report)
    assert report["verified"] is True
    consistency = next(
        check
        for check in report["checks"]
        if check["code"] == "RUN_RESULT_CONSISTENCY"
    )
    assert "0.1.0a3 compatibility path" in consistency["message"]


def test_project_verification_fails_closed_on_plan_tampering(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()
    plan_path = completed.plan_path
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["model"]["name"] = "tampered"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    report = project.verify()

    _validate(report)
    assert report["verified"] is False
    integrity = next(
        check for check in report["checks"] if check["code"] == "PLAN_INTEGRITY"
    )
    assert integrity["passed"] is False


def test_project_verification_fails_closed_on_summary_drift(tmp_path) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()
    summary_path = completed.directory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["quantities"]["flow.pressure_drop"]["value"] = 0.0
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    report = project.verify()

    _validate(report)
    assert report["verified"] is False
    integrity = next(
        check for check in report["checks"] if check["code"] == "SUMMARY_INTEGRITY"
    )
    assert integrity["passed"] is False
    assert "quantities" in integrity["message"]


def test_project_verification_delegates_complete_portable_bundle(
    tmp_path, monkeypatch
) -> None:
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()
    fields = completed.directory / "fields"
    fields.mkdir()
    for name in ("fields.xdmf", "fields.h5", "manifest.json"):
        (fields / name).write_text("opaque", encoding="utf-8")

    observed = []

    def verify_bundle(path):
        observed.append(path)
        return {
            "schema": "agentcfd.field-bundle-verification/0.1",
            "verified": True,
            "frame_count": 2,
            "point_count": 8,
            "cell_block_count": 1,
            "formats": ["hdf5", "xdmf"],
        }

    monkeypatch.setattr(projects.data_exchange, "verify_field_bundle", verify_bundle)

    report = project.verify()

    _validate(report)
    assert report["verified"] is True
    assert observed == [fields]
    assert report["field_bundle"]["frame_count"] == 2
    assert report["observation_cost"]["field_payloads_opened"] is True
    assert report["checks"][-1]["code"] == "FIELD_BUNDLE_INTEGRITY"


def test_project_verification_contract_is_shipped() -> None:
    assert "project-verification.schema.json" in contracts.available()
