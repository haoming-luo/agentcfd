from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_fast_gate_is_one_bounded_linux_job() -> None:
    workflow = _workflow("test.yml")

    assert workflow.count("    runs-on:") == 1
    assert "runs-on: ubuntu-latest" in workflow
    assert "macos-" not in workflow
    assert "windows-" not in workflow
    assert "matrix:" not in workflow
    assert "timeout-minutes: 15" in workflow
    assert "cancel-in-progress: true" in workflow


def test_fast_gate_skips_docs_and_reuses_one_local_quality_gate() -> None:
    workflow = _workflow("test.yml")

    assert workflow.count('      - "**/*.md"') == 2
    assert workflow.count('      - "docs/**/*.md"') == 2
    assert '      - "docs/**"' not in workflow
    assert workflow.count("python -m build") == 1
    assert workflow.count("ci/installed_wheel_smoke.py") == 1
    assert "python -m ruff check ." in workflow
    assert "python -m pytest -q" in workflow


def test_fast_gate_has_no_scheduled_or_cross_platform_trigger() -> None:
    workflow = _workflow("test.yml")

    assert "\n  schedule:" not in workflow
    assert "\n  release:" not in workflow
    assert "macos-" not in workflow
    assert "windows-" not in workflow


def test_cross_platform_acceptance_is_manual_and_five_combinations() -> None:
    workflow = _workflow("acceptance.yml")

    assert workflow.startswith("name: Cross-platform acceptance\n\non:\n  workflow_dispatch:\n")
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    assert workflow.count("          - os:") == 5
    assert workflow.count("          - os: ubuntu-latest") == 3
    assert workflow.count("          - os: macos-latest") == 1
    assert workflow.count("          - os: windows-latest") == 1
    assert "cancel-in-progress: true" in workflow
    assert "\n  schedule:" not in workflow


def test_release_builds_once_and_tests_the_same_artifact_on_full_matrix() -> None:
    workflow = _workflow("release.yml")

    assert "types: [published]" in workflow
    assert workflow.count("python -m build") == 1
    assert workflow.count("actions/upload-artifact@") == 1
    assert workflow.count("actions/download-artifact@") == 2
    assert "retention-days: 3" in workflow
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in workflow
    assert 'python-version: ["3.11", "3.12", "3.13"]' in workflow
    assert "needs: [build, cross-platform]" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    assert "\n  schedule:" not in workflow
