"""Compact, verified project handoff archives without provider workspaces."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import tomllib
from typing import TYPE_CHECKING, Mapping
import zipfile

from .jsonio import strict_json_object
from .provenance import file_sha256
from .results import read_result_record

if TYPE_CHECKING:  # pragma: no cover
    from .projects import Project


_PROFILES = {"decision", "portable"}
_MANIFEST_NAME = "agentcfd-archive.json"
_STORED_SUFFIXES = {".h5", ".npz", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".mp4"}


def _profile(profile: str) -> str:
    selected = str(profile).strip().lower()
    if selected not in _PROFILES:
        raise ValueError("Archive profile must be 'decision' or 'portable'.")
    return selected


def _safe_relative(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"Archive source escapes the project root: {path}") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"Archive source path is invalid: {path}")
    return relative


def _walk_regular(directory: Path):
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file() and path.name != ".DS_Store":
            yield path


def _selected_files(
    project: "Project", run_directory: Path, profile: str
) -> tuple[Path, ...]:
    root = project.root.resolve()
    selected: set[Path] = set()
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file() or path.name == ".DS_Store":
            continue
        selected.add(path.resolve())
    for directory_name in ("input", "geometry"):
        for path in _walk_regular(root / directory_name) or ():
            selected.add(path.resolve())

    for path in _walk_regular(run_directory) or ():
        relative_to_run = path.relative_to(run_directory)
        if relative_to_run == Path("evidence/restart.zip"):
            continue
        if profile == "decision" and relative_to_run.parts[0] in {
            "fields",
            "postprocess",
        }:
            continue
        selected.add(path.resolve())
    return tuple(
        sorted(selected, key=lambda path: _safe_relative(root, path).as_posix())
    )


def plan_project_archive(
    project: "Project",
    *,
    profile: str = "decision",
    run_id: str | None = None,
) -> dict[str, object]:
    """Verify and inventory one compact handoff without writing an archive."""

    selected_profile = _profile(profile)
    verification = project.verify(
        run_id=run_id,
        verify_fields=selected_profile == "portable",
    )
    if verification.get("verified") is not True:
        failed = [
            str(check.get("message"))
            for check in verification.get("checks", [])
            if isinstance(check, Mapping) and check.get("passed") is not True
        ]
        raise ValueError(
            "Project archive requires verified output: " + "; ".join(failed)
        )
    run_directory = Path(str(verification["run_directory"])).resolve()
    files = _selected_files(project, run_directory, selected_profile)
    records = []
    total_bytes = 0
    fields_bytes = 0
    for path in files:
        relative = _safe_relative(project.root, path).as_posix()
        size = path.stat().st_size
        total_bytes += size
        if "/fields/" in f"/{relative}":
            fields_bytes += size
        records.append(
            {
                "path": relative,
                "bytes": size,
                "sha256": file_sha256(path),
                "role": "published-result"
                if path.is_relative_to(run_directory)
                else "project-input",
            }
        )
    return {
        "schema": "agentcfd.project-archive-plan/0.1",
        "project": project.root.name,
        "profile": selected_profile,
        "run_id": verification["run_id"],
        "accepted": verification["accepted"],
        "trust_level": verification["trust_level"],
        "file_count": len(records),
        "source_bytes": total_bytes,
        "field_payload_bytes": fields_bytes,
        "field_payloads_included": fields_bytes > 0,
        "files": records,
        "excluded": [
            ".agentcfd/ disposable provider workspaces and native time directories",
            ".git/ repository metadata",
            "unselected campaigns/ runs",
            "evidence/restart.zip solver recovery checkpoints",
            "fields/ and postprocess/ for the decision profile",
            "symbolic links",
        ],
        "verification": {
            "schema": verification["schema"],
            "verified": True,
            "checks": len(verification["checks"]),
            "field_payloads_opened": verification["observation_cost"][
                "field_payloads_opened"
            ],
        },
    }


def _zip_info(path: str, *, compress_type: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compress_type
    info.external_attr = 0o100644 << 16
    info.create_system = 3
    return info


def export_project_archive(
    project: "Project",
    destination: str | Path,
    *,
    profile: str = "decision",
    run_id: str | None = None,
) -> tuple[Path, dict[str, object]]:
    """Write a verified project archive atomically and without loading HDF5 in memory."""

    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"Project archive already exists: {target}")
    plan = plan_project_archive(project, profile=profile, run_id=run_id)
    manifest = {
        "schema": "agentcfd.project-archive/0.1",
        "created_at": datetime.now(UTC).isoformat(),
        "project": plan["project"],
        "profile": plan["profile"],
        "run_id": plan["run_id"],
        "accepted": plan["accepted"],
        "trust_level": plan["trust_level"],
        "field_payloads_included": plan["field_payloads_included"],
        "files": plan["files"],
        "excluded": plan["excluded"],
        "source_verification": plan["verification"],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    Path(temporary_name).unlink()
    try:
        with zipfile.ZipFile(
            temporary_name,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for record in manifest["files"]:
                source = project.root / str(record["path"])
                compression = (
                    zipfile.ZIP_STORED
                    if source.suffix.lower() in _STORED_SUFFIXES
                    else zipfile.ZIP_DEFLATED
                )
                digest = hashlib.sha256()
                byte_count = 0
                with (
                    source.open("rb") as input_stream,
                    archive.open(
                        _zip_info(str(record["path"]), compress_type=compression), "w"
                    ) as output_stream,
                ):
                    while chunk := input_stream.read(1024 * 1024):
                        output_stream.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
                if (
                    byte_count != record["bytes"]
                    or digest.hexdigest() != record["sha256"]
                ):
                    raise ValueError(
                        f"Project file changed while archiving: {record['path']}"
                    )
            manifest_bytes = (
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            archive.writestr(
                _zip_info(_MANIFEST_NAME, compress_type=zipfile.ZIP_DEFLATED),
                manifest_bytes,
            )
        verification = verify_project_archive(temporary_name)
        if verification["verified"] is not True:
            raise ValueError("New project archive failed its own integrity check.")
        Path(temporary_name).replace(target)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return target, manifest


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _restored_artifact_path(
    value: str,
    *,
    result_directory: Path,
    restored_root: Path,
    source_root: Path | None,
) -> Path | None:
    selected = Path(value)
    if not selected.is_absolute():
        candidate = (result_directory / selected).resolve()
    else:
        if source_root is None:
            return None
        try:
            relative = selected.resolve().relative_to(source_root.resolve())
        except ValueError:
            return None
        candidate = (restored_root / relative).resolve()
    try:
        candidate.relative_to(restored_root.resolve())
    except ValueError:
        return None
    return candidate


def _materialize_restored_result(
    result_path: Path,
    *,
    restored_root: Path,
    source_root: Path | None,
    profile: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Rebase retained artifacts and explicitly compact omitted decision fields."""

    result = strict_json_object(
        result_path.read_text(encoding="utf-8"),
        label=f"restored AgentCFD result {result_path}",
    )
    artifacts = result.get("artifacts")
    artifact_records = result.get("artifact_records")
    if not isinstance(artifacts, dict) or not isinstance(artifact_records, dict):
        raise ValueError("Restored result has malformed artifact records.")
    if set(artifacts) != set(artifact_records):
        raise ValueError("Restored result artifact indexes disagree.")

    omitted: list[str] = []
    changed = False
    for name in sorted(artifacts):
        record = artifact_records[name]
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError(f"Restored artifact {name!r} has malformed metadata.")
        candidate = _restored_artifact_path(
            str(record["path"]),
            result_directory=result_path.parent,
            restored_root=restored_root,
            source_root=source_root,
        )
        if candidate is None or not candidate.is_file():
            if profile == "portable":
                raise ValueError(
                    f"Portable archive is missing registered artifact {name!r}."
                )
            omitted.append(name)
            continue
        relative = Path(os.path.relpath(candidate, result_path.parent)).as_posix()
        if artifacts[name] != relative or record["path"] != relative:
            artifacts[name] = relative
            record["path"] = relative
            changed = True

    if not omitted:
        fields = result.get("fields", {})
        field_records = result.get("field_records", [])
        if not isinstance(fields, dict) or not isinstance(field_records, list):
            raise ValueError("Restored result field records are malformed.")

        def rebase_field(field: object) -> None:
            nonlocal changed
            if not isinstance(field, dict) or not isinstance(
                field.get("artifact"), str
            ):
                raise ValueError("Restored result field record is malformed.")
            original = str(field["artifact"])
            indexed_value = artifacts.get(original, original)
            if not isinstance(indexed_value, str):
                raise ValueError("Restored result field artifact is malformed.")
            candidate = _restored_artifact_path(
                indexed_value,
                result_directory=result_path.parent,
                restored_root=restored_root,
                source_root=source_root,
            )
            if candidate is None or not candidate.is_file():
                raise ValueError("Portable archive has an unresolved field artifact.")
            relative = Path(os.path.relpath(candidate, result_path.parent)).as_posix()
            if field["artifact"] != relative:
                field["artifact"] = relative
                changed = True

        for field in fields.values():
            rebase_field(field)
        for field in field_records:
            rebase_field(field)

    if omitted:
        omitted_set = set(omitted)
        result["artifacts"] = {
            name: value for name, value in artifacts.items() if name not in omitted_set
        }
        result["artifact_records"] = {
            name: value
            for name, value in artifact_records.items()
            if name not in omitted_set
        }
        # The decision profile intentionally has no spatial field surface.
        # Field records can hold provider paths rather than artifact-index keys,
        # so clearing the complete field index is the only unambiguous contract.
        result["fields"] = {}
        result["field_records"] = []
        provenance = result.get("provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("Restored result provenance is malformed.")
        provenance["result_profile"] = "summary-only"
        provenance["archive_restoration"] = {
            "profile": "decision",
            "source_result_sha256": file_sha256(result_path),
            "omitted_artifacts": omitted,
        }
        result["provenance"] = provenance
        messages = result.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("Restored result messages are malformed.")
        messages.append(
            "Decision archive restoration retained verified quantities and evidence; "
            "omitted spatial artifacts can be regenerated from case.py."
        )
        result["messages"] = messages
        changed = True

    if changed:
        _write_json(result_path, result)
    verified_result = read_result_record(result_path, verify_artifacts=True)
    return verified_result, tuple(omitted)


def verify_project_archive(path: str | Path) -> dict[str, object]:
    """Verify archive structure and every payload without extracting it."""

    source = Path(path).expanduser().resolve()
    checks: list[dict[str, object]] = []

    def add(code: str, passed: bool, message: str) -> None:
        checks.append({"code": code, "passed": passed, "message": message})

    manifest: dict[str, object] | None = None
    verified_files = 0
    verified_bytes = 0
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("Archive contains duplicate member names.")
            if any(not _safe_archive_name(name) for name in names):
                raise ValueError("Archive contains an unsafe member path.")
            if _MANIFEST_NAME not in names:
                raise ValueError("Archive manifest is missing.")
            manifest_info = archive.getinfo(_MANIFEST_NAME)
            if manifest_info.file_size > 8 * 1024 * 1024:
                raise ValueError("Archive manifest exceeds the 8 MiB safety limit.")
            manifest = strict_json_object(
                archive.read(_MANIFEST_NAME).decode("utf-8"),
                label=f"AgentCFD project archive {source}",
            )
            if manifest.get("schema") != "agentcfd.project-archive/0.1":
                raise ValueError("Unsupported AgentCFD project archive schema.")
            if manifest.get("profile") not in _PROFILES:
                raise ValueError("Archive profile is invalid.")
            records = manifest.get("files")
            if not isinstance(records, list) or len(records) > 100_000:
                raise ValueError(
                    "Archive file index is missing or exceeds its safety limit."
                )
            expected_names = {_MANIFEST_NAME}
            for record in records:
                if (
                    not isinstance(record, Mapping)
                    or set(record) != {"path", "bytes", "sha256", "role"}
                    or not isinstance(record.get("path"), str)
                    or not _safe_archive_name(record["path"])
                    or record.get("role") not in {"project-input", "published-result"}
                    or not isinstance(record.get("bytes"), int)
                    or record["bytes"] < 0
                    or not isinstance(record.get("sha256"), str)
                    or len(record["sha256"]) != 64
                ):
                    raise ValueError("Archive file index contains a malformed record.")
                expected_names.add(record["path"])
            if expected_names != set(names):
                raise ValueError(
                    "Archive members disagree with the manifest file index."
                )
            add("ARCHIVE_MANIFEST", True, "Manifest and member index are coherent.")
            for record in records:
                digest = hashlib.sha256()
                byte_count = 0
                with archive.open(record["path"], "r") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        byte_count += len(chunk)
                if (
                    byte_count != record["bytes"]
                    or digest.hexdigest() != record["sha256"]
                ):
                    raise ValueError(f"Archive payload changed: {record['path']}")
                verified_files += 1
                verified_bytes += byte_count
            add(
                "ARCHIVE_PAYLOADS",
                True,
                f"Verified {verified_files} content-addressed project files.",
            )
    except (
        OSError,
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        if not checks:
            add("ARCHIVE_MANIFEST", False, str(error))
        else:
            add("ARCHIVE_PAYLOADS", False, str(error))
    verified = all(check["passed"] is True for check in checks) and len(checks) == 2
    return {
        "schema": "agentcfd.project-archive-verification/0.1",
        "archive": str(source),
        "verified": verified,
        "profile": manifest.get("profile") if manifest is not None else None,
        "run_id": manifest.get("run_id") if manifest is not None else None,
        "accepted": manifest.get("accepted") if manifest is not None else None,
        "file_count": verified_files,
        "uncompressed_bytes_verified": verified_bytes,
        "archive_bytes": source.stat().st_size if source.is_file() else 0,
        "checks": checks,
    }


def restore_project_archive(
    path: str | Path,
    destination: str | Path,
) -> tuple[Path, dict[str, object]]:
    """Restore a verified archive atomically as an immediately openable project."""

    source = Path(path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"Restore destination already exists: {target}")
    verification = verify_project_archive(source)
    if verification["verified"] is not True:
        failures = "; ".join(
            str(check["message"])
            for check in verification["checks"]
            if check["passed"] is not True
        )
        raise ValueError(f"Project archive is not verified: {failures}")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
    )
    restored_files = 0
    restored_bytes = 0
    omitted_artifacts: list[str] = []
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = strict_json_object(
                archive.read(_MANIFEST_NAME).decode("utf-8"),
                label=f"AgentCFD project archive {source}",
            )
            for record in manifest["files"]:
                relative = PurePosixPath(str(record["path"]))
                output = staging.joinpath(*relative.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                byte_count = 0
                with (
                    archive.open(str(record["path"]), "r") as input_stream,
                    output.open("wb") as output_stream,
                ):
                    while chunk := input_stream.read(1024 * 1024):
                        output_stream.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
                if (
                    byte_count != record["bytes"]
                    or digest.hexdigest() != record["sha256"]
                ):
                    raise ValueError(
                        f"Archive payload changed during restore: {record['path']}"
                    )
                restored_files += 1
                restored_bytes += byte_count

        # Extraction is byte-verified against the immutable source manifest.
        # Local navigation records then become project-relative; a decision
        # archive is explicitly materialized as summary-only when source field
        # payloads were intentionally excluded.  archive-source.json retains
        # the exact pre-materialization identities.
        summary_paths = [
            staging.joinpath(*PurePosixPath(str(record["path"])).parts)
            for record in manifest["files"]
            if PurePosixPath(str(record["path"])).name == "summary.json"
        ]
        for summary_path in summary_paths:
            summary = strict_json_object(
                summary_path.read_text(encoding="utf-8"),
                label=f"restored AgentCFD result summary {summary_path}",
            )
            if summary.get("schema") not in {
                "agentcfd.result-summary/0.2",
                "agentcfd.result-summary/0.3",
            }:
                raise ValueError("Restored archive has an unsupported result summary.")
            source_root_value = summary.get("root")
            source_root = (
                Path(source_root_value)
                if isinstance(source_root_value, str)
                and Path(source_root_value).is_absolute()
                else None
            )
            relative_summary = summary_path.relative_to(staging).as_posix()
            result_path = summary_path.with_name("result.json")
            relative_result = result_path.relative_to(staging).as_posix()
            source_result = summary.get("source_result")
            if not isinstance(source_result, dict):
                raise ValueError("Restored result summary has no source identity.")
            restored_result, omitted = _materialize_restored_result(
                result_path,
                restored_root=staging,
                source_root=source_root,
                profile=str(manifest["profile"]),
            )
            omitted_artifacts.extend(omitted)
            summary["root"] = "."
            summary["summary"] = relative_summary
            summary["result"] = relative_result
            source_result["path"] = relative_result
            source_result["bytes"] = result_path.stat().st_size
            source_result["sha256"] = file_sha256(result_path)
            summary["source_result"] = source_result
            summary["fields"] = restored_result["fields"]
            summary["provenance"] = restored_result["provenance"]
            available = summary.get("available")
            if not isinstance(available, dict):
                raise ValueError("Restored result summary availability is malformed.")
            available["fields"] = sorted(restored_result["fields"])
            summary["available"] = available
            artifact_integrity = summary.get("artifact_integrity")
            if isinstance(artifact_integrity, dict):
                artifact_integrity["command"] = (
                    f"agentcfd verify result {relative_result}"
                )
            next_action = summary.get("next_action")
            if isinstance(next_action, dict):
                next_action["command"] = "agentcfd view ."
            _write_json(summary_path, summary)

            run_path = summary_path.with_name("run.json")
            run_record = strict_json_object(
                run_path.read_text(encoding="utf-8"),
                label=f"restored AgentCFD run {run_path}",
            )
            run_directory = summary_path.parent.relative_to(staging).as_posix()
            run_record["directory"] = run_directory
            run_record["result"] = relative_result
            run_record["summary"] = relative_summary
            run_record["plan"] = (
                summary_path.with_name("plan.json").relative_to(staging).as_posix()
            )
            run_record["solver_workspace"] = None
            run_record["result_sha256"] = source_result["sha256"]
            if omitted:
                run_record["source_result_execution_sha256"] = run_record.get(
                    "result_execution_sha256"
                )
                run_record["result_execution_sha256"] = None
                run_record["result_profile"] = "summary-only"
                run_record["field_bundle"] = None
                run_record["archive_restoration"] = {
                    "profile": "decision",
                    "omitted_artifacts": list(omitted),
                }
            _write_json(run_path, run_record)

        project_manifest_path = staging / "agentcfd.toml"
        try:
            project_manifest = tomllib.loads(
                project_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValueError("Restored archive has no valid agentcfd.toml.") from error
        if project_manifest.get("schema") != "agentcfd.project/0.1":
            raise ValueError("Restored archive has an unsupported project schema.")
        entrypoint = project_manifest.get("entrypoint")
        if not isinstance(entrypoint, str) or not _safe_archive_name(entrypoint):
            raise ValueError("Restored project entrypoint is unsafe or missing.")
        if not staging.joinpath(*PurePosixPath(entrypoint).parts).is_file():
            raise ValueError("Restored project entrypoint is missing.")

        provenance = {
            "schema": "agentcfd.archive-source/0.1",
            "restored_at": datetime.now(UTC).isoformat(),
            "source_archive": str(source),
            "source_archive_sha256": file_sha256(source),
            "source_manifest": manifest,
        }
        (staging / "archive-source.json").write_text(
            json.dumps(
                provenance,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    report = {
        "schema": "agentcfd.project-archive-restoration/0.1",
        "source_archive": str(source),
        "source_archive_sha256": provenance["source_archive_sha256"],
        "destination": str(target),
        "profile": verification["profile"],
        "run_id": verification["run_id"],
        "accepted": verification["accepted"],
        "archive_verified": True,
        "file_count": restored_files,
        "restored_bytes": restored_bytes,
        "result_profile": "summary-only" if omitted_artifacts else "source",
        "omitted_artifacts": sorted(set(omitted_artifacts)),
        "provenance": "archive-source.json",
    }
    return target, report


__all__ = [
    "export_project_archive",
    "plan_project_archive",
    "restore_project_archive",
    "verify_project_archive",
]
