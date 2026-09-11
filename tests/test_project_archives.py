import json
import zipfile

import agentcfd
import jsonschema
import pytest

from agentcfd import Artifact, FieldRecord, archives, contracts, projects
from agentcfd.cli import entrypoint
from agentcfd.provenance import file_sha256


def test_project_archive_is_previewed_atomic_compact_and_independently_verified(
    tmp_path, capsys, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    (project.root / "input").mkdir()
    (project.root / "input" / "operating-note.txt").write_text(
        "Reviewed inlet condition.\n", encoding="utf-8"
    )
    (project.root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    native = project.root / ".agentcfd" / "work" / "native" / "100"
    native.mkdir(parents=True)
    (native / "U").write_bytes(b"native-field" * 100)
    old_campaign = project.root / "campaigns" / "unselected"
    old_campaign.mkdir(parents=True)
    (old_campaign / "result.json").write_text("{}", encoding="utf-8")
    project.run()
    fields = project.root / "output" / "fields"
    fields.mkdir()
    (fields / "fields.xdmf").write_text("not opened", encoding="utf-8")
    (fields / "fields.h5").write_bytes(b"not opened")
    (fields / "manifest.json").write_text("{}", encoding="utf-8")

    def reject_field_open(_directory):
        raise ValueError("field payload opened")

    monkeypatch.setattr(
        projects.data_exchange, "verify_field_bundle", reject_field_open
    )

    plan = project.archive_plan(profile="decision")
    jsonschema.Draft202012Validator(
        contracts.load("project-archive-plan.schema.json")
    ).validate(plan)
    paths = {record["path"] for record in plan["files"]}
    assert "case.py" in paths
    assert "helper.py" in paths
    assert "input/operating-note.txt" in paths
    assert "output/result.json" in paths
    assert not any(path.startswith(".agentcfd/") for path in paths)
    assert not any(path.startswith("campaigns/") for path in paths)
    assert not any("/fields/" in f"/{path}" for path in paths)
    assert plan["field_payloads_included"] is False
    assert plan["verification"]["field_payloads_opened"] is False
    with pytest.raises(ValueError, match="requires verified output"):
        project.archive_plan(profile="portable")

    output = tmp_path / "handoff" / "pipe-decision.zip"
    archive_path, manifest = project.export_archive(output, profile="decision")
    assert archive_path == output.resolve()
    jsonschema.Draft202012Validator(
        contracts.load("project-archive.schema.json")
    ).validate(manifest)
    with zipfile.ZipFile(output) as bundle:
        names = set(bundle.namelist())
        stored_manifest = json.loads(bundle.read("agentcfd-archive.json"))
    assert stored_manifest == manifest
    assert names == {"agentcfd-archive.json", *paths}

    verification = archives.verify_project_archive(output)
    jsonschema.Draft202012Validator(
        contracts.load("project-archive-verification.schema.json")
    ).validate(verification)
    assert verification["verified"] is True
    assert verification["file_count"] == plan["file_count"]
    assert verification["accepted"] is True
    assert verification["uncompressed_bytes_verified"] == plan["source_bytes"]

    restored, restoration = archives.restore_project_archive(
        output, tmp_path / "restored-api"
    )
    jsonschema.Draft202012Validator(
        contracts.load("project-archive-restoration.schema.json")
    ).validate(restoration)
    assert restoration["archive_verified"] is True
    assert restoration["file_count"] == plan["file_count"]
    assert restoration["restored_bytes"] == plan["source_bytes"]
    assert (restored / "archive-source.json").is_file()
    assert not (restored / ".agentcfd").exists()
    restored_project = projects.Project(restored)
    assert restored_project.verify()["verified"] is True
    assert restored_project.status()["state"] == "complete"
    materialized_summary = json.loads(
        (restored / "output" / "summary.json").read_text(encoding="utf-8")
    )
    assert materialized_summary["root"] == "."
    assert materialized_summary["result"] == "output/result.json"
    assert materialized_summary["source_result"]["path"] == "output/result.json"
    moved_restoration = tmp_path / "moved-restoration"
    restored.rename(moved_restoration)
    moved_project = projects.Project(moved_restoration)
    assert moved_project.verify()["verified"] is True
    moved_summary = moved_project.result_summary()
    assert moved_summary["root"] == str(moved_restoration.resolve())
    assert moved_summary["result"] == str(
        moved_restoration.resolve() / "output" / "result.json"
    )

    assert (
        entrypoint(
            [
                "archive",
                str(project.root),
                "--profile",
                "decision",
                "--plan-only",
                "--json",
            ]
        )
        == 0
    )
    cli_plan = json.loads(capsys.readouterr().out)
    assert cli_plan["files"] == plan["files"]
    assert entrypoint(["verify", "archive", str(output), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    cli_restored = tmp_path / "restored-cli"
    assert entrypoint(["restore", str(output), str(cli_restored), "--json"]) == 0
    cli_restoration = json.loads(capsys.readouterr().out)
    assert cli_restoration["archive_verified"] is True
    assert projects.Project(cli_restored).status()["state"] == "complete"
    assert "project-archive.schema.json" in contracts.available()
    assert "project-archive-restoration.schema.json" in contracts.available()

    with pytest.raises(FileExistsError, match="already exists"):
        project.export_archive(output)

    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(output.read_bytes())
    with zipfile.ZipFile(tampered, "a") as bundle:
        bundle.writestr("unexpected.txt", "not indexed")
    report = archives.verify_project_archive(tampered)
    assert report["verified"] is False
    assert report["checks"][0]["code"] == "ARCHIVE_MANIFEST"
    assert "disagree" in report["checks"][0]["message"]
    assert entrypoint(["verify", "archive", str(tampered), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["verified"] is False
    with pytest.raises(ValueError, match="not verified"):
        archives.restore_project_archive(tampered, tmp_path / "unsafe-restore")
    assert not (tmp_path / "unsafe-restore").exists()

    with pytest.raises(FileExistsError, match="already exists"):
        archives.restore_project_archive(output, moved_restoration)


def test_project_archive_rejects_invalid_profile_and_missing_output(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    project.run()

    with pytest.raises(ValueError, match="decision.*portable"):
        project.archive_plan(profile="native")
    assert entrypoint(["archive", str(project.root), "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert "requires OUTPUT" in error["error"]["message"]
    assert callable(agentcfd.restore_project_archive)
    assert callable(agentcfd.verify_project_archive)


def test_decision_restore_explicitly_compacts_omitted_spatial_artifacts(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "source")
    completed = project.run()
    fields = completed.directory / "fields"
    fields.mkdir()
    xdmf = fields / "fields.xdmf"
    h5 = fields / "fields.h5"
    manifest = fields / "manifest.json"
    xdmf.write_text("portable index", encoding="utf-8")
    h5.write_bytes(b"portable field payload")
    manifest.write_text("{}\n", encoding="utf-8")

    result_path = completed.result_path
    result = json.loads(result_path.read_text(encoding="utf-8"))
    artifact_paths = {
        "fields.xdmf": xdmf,
        "fields.hdf5": h5,
        "fields.manifest": manifest,
    }
    result["artifacts"] = {
        name: path.relative_to(completed.directory).as_posix()
        for name, path in artifact_paths.items()
    }
    result["artifact_records"] = {
        name: Artifact.from_path(
            path,
            role="portable-field-bundle",
        ).as_dict(base=completed.directory)
        for name, path in artifact_paths.items()
    }
    field = FieldRecord(
        unit="m/s",
        location="point",
        artifact=str(xdmf),
        components=("x", "y", "z"),
        representation="xdmf-hdf5",
    ).as_record("fluid.velocity")
    result["fields"] = {"fluid.velocity": field}
    result["field_records"] = [field]
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary_path = completed.directory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["fields"] = result["fields"]
    summary["available"]["fields"] = ["fluid.velocity"]
    summary["source_result"]["bytes"] = result_path.stat().st_size
    summary["source_result"]["sha256"] = file_sha256(result_path)
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    run_path = completed.directory / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["result_sha256"] = file_sha256(result_path)
    run["result_profile"] = "full-fields"
    run_path.write_text(
        json.dumps(run, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        projects.data_exchange,
        "verify_field_bundle",
        lambda _path: {
            "schema": "agentcfd.field-bundle-verification/0.1",
            "verified": True,
            "frame_count": 1,
            "point_count": 1,
            "cell_block_count": 1,
            "formats": ["hdf5", "xdmf"],
        },
    )

    assert project.verify(verify_fields=False)["verified"] is True
    archive_path, _ = project.export_archive(
        tmp_path / "decision.zip",
        profile="decision",
    )
    restored, report = archives.restore_project_archive(
        archive_path,
        tmp_path / "restored",
    )

    assert report["result_profile"] == "summary-only"
    assert report["omitted_artifacts"] == sorted(artifact_paths)
    restored_result = projects.read_result_record(
        restored / "output" / "result.json",
        verify_artifacts=True,
    )
    assert restored_result["fields"] == {}
    assert restored_result["artifacts"] == {}
    assert restored_result["accepted"] is True
    assert restored_result["provenance"]["archive_restoration"][
        "source_result_sha256"
    ] == file_sha256(completed.result_path)
    assert projects.Project(restored).verify()["verified"] is True

    portable_archive, _ = project.export_archive(
        tmp_path / "portable.zip",
        profile="portable",
    )
    portable, portable_report = archives.restore_project_archive(
        portable_archive,
        tmp_path / "portable",
    )
    assert portable_report["result_profile"] == "source"
    assert portable_report["omitted_artifacts"] == []
    portable_result = projects.read_result_record(
        portable / "output" / "result.json",
        verify_artifacts=True,
    )
    assert portable_result["fields"]["fluid.velocity"]["artifact"] == (
        "fields/fields.xdmf"
    )
    moved_portable = tmp_path / "moved-portable"
    portable.rename(moved_portable)
    assert projects.Project(moved_portable).verify()["verified"] is True
