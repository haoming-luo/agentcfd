"""Side-effect-free project compatibility and upgrade inspection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shlex
import tomllib

from ._version import __version__
from .errors import ProjectError


SUPPORTED_PROJECT_SCHEMA = "agentcfd.project/0.1"


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    """One stable, machine-actionable compatibility finding."""

    code: str
    severity: str
    message: str
    repair: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _manifest_path(location: str | Path) -> Path | None:
    selected = Path(location).expanduser()
    if selected.name == "agentcfd.toml":
        return selected.resolve() if selected.is_file() else None
    current = selected.resolve()
    if current.is_file():
        current = current.parent
    while True:
        candidate = current / "agentcfd.toml"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def inspect_project(location: str | Path) -> dict[str, object]:
    """Inspect schema compatibility without importing ``case.py`` or changing files."""

    manifest = _manifest_path(location)
    issues: list[CompatibilityIssue] = []
    detected: str | None = None
    status = "not_found"
    compatible = False
    can_open = False
    upgrade_available = False

    if manifest is None:
        issues.append(
            CompatibilityIssue(
                "PROJECT_MANIFEST_NOT_FOUND",
                "error",
                "No agentcfd.toml was found at or above the selected location.",
                "Select an AgentCFD project or create one with `agentcfd init`.",
            )
        )
    else:
        try:
            payload = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            status = "invalid"
            issues.append(
                CompatibilityIssue(
                    "PROJECT_MANIFEST_INVALID",
                    "error",
                    f"The project manifest cannot be parsed: {error}",
                    "Repair agentcfd.toml before opening or upgrading the project.",
                )
            )
        else:
            raw_schema = payload.get("schema")
            detected = raw_schema if isinstance(raw_schema, str) else None
            if detected == SUPPORTED_PROJECT_SCHEMA:
                # Reuse the complete current validator, which remains side-effect-free
                # and does not import the user-owned project entrypoint.
                try:
                    from .projects import ProjectManifest

                    ProjectManifest.read(manifest)
                except (ProjectError, ValueError, OSError) as error:
                    status = "invalid"
                    issues.append(
                        CompatibilityIssue(
                            "PROJECT_MANIFEST_INVALID",
                            "error",
                            str(error),
                            "Repair the reported manifest field; no project file was changed.",
                        )
                    )
                else:
                    status = "compatible"
                    compatible = True
                    can_open = True
            elif detected is None:
                status = "invalid"
                issues.append(
                    CompatibilityIssue(
                        "PROJECT_SCHEMA_MISSING",
                        "error",
                        "The project manifest has no string schema identifier.",
                        (
                            f"Declare schema = \"{SUPPORTED_PROJECT_SCHEMA}\" only "
                            "after reviewing the manifest."
                        ),
                    )
                )
            elif detected.startswith("agentcfd.project/0.0"):
                status = "upgrade_required"
                issues.append(
                    CompatibilityIssue(
                        "PROJECT_SCHEMA_LEGACY",
                        "error",
                        f"Project schema {detected!r} predates the supported schema.",
                        (
                            "Use a reviewed migration when one becomes available; "
                            "automatic rewriting is intentionally disabled."
                        ),
                    )
                )
            elif detected.startswith("agentcfd.project/"):
                status = "unsupported_newer"
                issues.append(
                    CompatibilityIssue(
                        "PROJECT_SCHEMA_UNSUPPORTED",
                        "error",
                        f"Project schema {detected!r} is not supported by this AgentCFD release.",
                        (
                            "Open the project with a compatible AgentCFD version; "
                            "do not downgrade the manifest by hand."
                        ),
                    )
                )
            else:
                status = "invalid"
                issues.append(
                    CompatibilityIssue(
                        "PROJECT_SCHEMA_FOREIGN",
                        "error",
                        f"Manifest schema {detected!r} is not an AgentCFD project schema.",
                        "Select the correct project directory or restore its original manifest.",
                    )
                )

    next_action = None
    if compatible and manifest is not None:
        next_action = {
            "operation": "inspect_project",
            "command": f"agentcfd project {shlex.quote(str(manifest.parent))} --json",
            "reason": "The project can be opened by this release.",
        }

    return {
        "schema": "agentcfd.project-compatibility/0.1",
        "agentcfd_version": __version__,
        "location": str(Path(location).expanduser().resolve()),
        "manifest": None if manifest is None else str(manifest),
        "detected_project_schema": detected,
        "supported_project_schema": SUPPORTED_PROJECT_SCHEMA,
        "status": status,
        "compatible": compatible,
        "can_open": can_open,
        "upgrade_available": upgrade_available,
        "mutated": False,
        "issues": [issue.to_dict() for issue in issues],
        "next_action": next_action,
    }


__all__ = ["SUPPORTED_PROJECT_SCHEMA", "CompatibilityIssue", "inspect_project"]
