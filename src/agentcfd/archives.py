"""Compact, verified project handoff archives without provider workspaces."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import TYPE_CHECKING, Mapping
import zipfile

from .jsonio import strict_json_object
from .provenance import file_sha256

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


def _selected_files(project: "Project", run_directory: Path, profile: str) -> tuple[Path, ...]:
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
    return tuple(sorted(selected, key=lambda path: _safe_relative(root, path).as_posix()))


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
        raise ValueError("Project archive requires verified output: " + "; ".join(failed))
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
                with source.open("rb") as input_stream, archive.open(
                    _zip_info(str(record["path"]), compress_type=compression), "w"
                ) as output_stream:
                    while chunk := input_stream.read(1024 * 1024):
                        output_stream.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
                if byte_count != record["bytes"] or digest.hexdigest() != record["sha256"]:
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
                raise ValueError("Archive file index is missing or exceeds its safety limit.")
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
                raise ValueError("Archive members disagree with the manifest file index.")
            add("ARCHIVE_MANIFEST", True, "Manifest and member index are coherent.")
            for record in records:
                digest = hashlib.sha256()
                byte_count = 0
                with archive.open(record["path"], "r") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        byte_count += len(chunk)
                if byte_count != record["bytes"] or digest.hexdigest() != record["sha256"]:
                    raise ValueError(f"Archive payload changed: {record['path']}")
                verified_files += 1
                verified_bytes += byte_count
            add(
                "ARCHIVE_PAYLOADS",
                True,
                f"Verified {verified_files} content-addressed project files.",
            )
    except (OSError, KeyError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
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


__all__ = [
    "export_project_archive",
    "plan_project_archive",
    "verify_project_archive",
]
