from __future__ import annotations

import re
import tomllib
from pathlib import Path

import agentcfd


ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent_across_public_metadata() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project_version = tomllib.load(stream)["project"]["version"]

    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    citation_match = re.search(r"^version: (\S+)$", citation, flags=re.MULTILINE)
    assert citation_match is not None

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert agentcfd.__version__ == project_version
    assert citation_match.group(1) == project_version
    assert f"agentcfd=={project_version}" in readme
    assert f"## {project_version} —" in changelog
    assert changelog.index("## Unreleased") < changelog.index(
        f"## {project_version} —"
    )
