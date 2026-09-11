import json
import zipfile

import jsonschema
import pytest

from agentcfd import archives, contracts, projects
from agentcfd.cli import entrypoint


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

    monkeypatch.setattr(projects.data_exchange, "verify_field_bundle", reject_field_open)

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

    assert (
        entrypoint(
            ["archive", str(project.root), "--profile", "decision", "--plan-only", "--json"]
        )
        == 0
    )
    cli_plan = json.loads(capsys.readouterr().out)
    assert cli_plan["files"] == plan["files"]
    assert entrypoint(["verify", "archive", str(output), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert "project-archive.schema.json" in contracts.available()

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


def test_project_archive_rejects_invalid_profile_and_missing_output(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    project.run()

    with pytest.raises(ValueError, match="decision.*portable"):
        project.archive_plan(profile="native")
    assert entrypoint(["archive", str(project.root), "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert "requires OUTPUT" in error["error"]["message"]
