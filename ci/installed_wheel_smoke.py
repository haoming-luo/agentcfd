"""Dependency-free smoke test for an installed AgentCFD wheel."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import agentcfd
from agentcfd import contracts


def run(*arguments: str) -> dict[str, object]:
    executable = Path(sys.executable).parent / "agentcfd"
    completed = subprocess.run(
        [str(executable), *arguments, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected JSON object from: {' '.join(arguments)}")
    return payload


def main() -> None:
    assert importlib.util.find_spec("numpy") is None
    assert callable(agentcfd.write_circular_elbow_stl)
    assert callable(agentcfd.open_project)
    required_contracts = {
        "generated-geometry.schema.json",
        "geometry-inspection.schema.json",
        "project-verification.schema.json",
        "simulation-result.schema.json",
        "field-bundle.schema.json",
    }
    assert required_contracts <= set(contracts.available())
    assert all(contracts.path(name).is_file() for name in required_contracts)
    assert run("doctor")["healthy"] is True
    assert run("capabilities")["capabilities"]
    assert run("templates")["templates"]

    with tempfile.TemporaryDirectory(prefix="agentcfd-wheel-") as raw_root:
        root = Path(raw_root)
        elbow = root / "elbow.stl"
        geometry = run(
            "geometry-create",
            "elbow",
            str(elbow),
            "--diameter-m",
            "0.1",
            "--bend-radius-m",
            "0.15",
            "--inlet-length-m",
            "0.3",
            "--outlet-length-m",
            "0.4",
        )
        point = geometry["recommendations"]["interior_point_m"]
        inspection = run(
            "geometry-check",
            str(elbow),
            "--unit",
            "m",
            "--internal-flow",
            "--role",
            "inlet=inlet",
            "--role",
            "outlet=outlet",
            "--role",
            "walls=wall",
        )
        assert inspection["readiness"]["ready_for_import_setup"] is True

        imported = root / "imported"
        run(
            "init",
            str(imported),
            "--template",
            "imported-internal-flow",
            "--provider",
            "openfoam",
            "--geometry",
            str(elbow),
            "--unit",
            "m",
            "--accept-name-roles",
            "--interior-point-m",
            *(str(value) for value in point),
            "--inlet-velocity-m-s",
            "1",
            "0",
            "0",
            "--base-size-m",
            "0.0125",
            "--maximum-cells",
            "500000",
        )
        assert run("check", str(imported))["valid"] is True

        reference = root / "reference"
        run("init", str(reference), "--template", "industrial-pipe")
        result = run("run", str(reference))
        assert result["accepted"] is True
        assert run("verify", "project", str(reference))["verified"] is True


if __name__ == "__main__":
    main()
