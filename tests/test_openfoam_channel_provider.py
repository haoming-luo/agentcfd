from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import zipfile

import pytest

from agentcfd import Artifact, Check, SimulationResult, fluids, initialization, procedures
from agentcfd.errors import UnsupportedCaseError
from agentcfd.projects import Project
from agentcfd.providers.openfoam_channel import (
    OpenFOAMChannelProvider,
    _write_restart_bundle,
)


EXAMPLE = Path(__file__).parents[1] / "examples" / "channel_baffle_project"


def test_channel_provider_prepares_deterministic_five_block_case(tmp_path):
    step = Project(EXAMPLE).load_step()
    first = OpenFOAMChannelProvider(case_directory=tmp_path / "first").prepare(step)
    second = OpenFOAMChannelProvider(case_directory=tmp_path / "second").prepare(step)

    assert first.case_sha256 == second.case_sha256
    assert first.capability == "openfoam.transient-laminar-baffled-channel"
    block_mesh = (first.directory / "system" / "blockMeshDict").read_text()
    assert block_mesh.count(" name ") == 5
    assert "name overBaffle" in block_mesh
    assert "(upstreamLower 1) (overBaffle 2) (downstreamLower 0)" in block_mesh
    control = (first.directory / "system" / "controlDict").read_text()
    assert "application pimpleFoam;" in control
    assert "agentcfd_inlet_flow" in control
    assert "near_wake" in control


def test_channel_provider_rejects_high_re_laminar_misuse(tmp_path):
    step = Project(EXAMPLE).load_step()
    high_re = deepcopy(step)
    high_re.model.fluid = fluids.newtonian(
        "water", density=998.2, dynamic_viscosity=1.002e-3
    )
    with pytest.raises(UnsupportedCaseError, match="Re < 2300"):
        OpenFOAMChannelProvider(case_directory=tmp_path).prepare(high_re)


def test_channel_report_names_are_lowered_without_leaking_backend_constraints(tmp_path):
    step = Project(EXAMPLE).load_step()
    OpenFOAMChannelProvider(case_directory=tmp_path).prepare(step)
    control = (tmp_path / "system" / "controlDict").read_text()
    assert "near_wake" in control
    assert "outlet_pressure" in control
    assert "baffle_drag" in control


def test_channel_provider_recovers_compact_reports_with_si_units(tmp_path):
    step = Project(EXAMPLE).load_step()
    files = {
        "near_wake/0/U": "# Time probe\n0.1 (1 -2 3)\n0.2 (4 -5 6)\n",
        "near_wake/0/p": "# Time probe\n0.1 0.1\n0.2 0.2\n",
        "outlet_pressure/0/surfaceFieldValue.dat": "# Time areaAverage(p)\n0.1 0.3\n0.2 0.4\n",
        "baffle_drag/0/force.dat": "# Time total xyz rest\n0.1 7 8 9 0 0 0\n0.2 10 11 12 0 0 0\n",
    }
    for relative, content in files.items():
        path = tmp_path / "postProcessing" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    quantities, histories, artifacts = {}, {}, {}

    OpenFOAMChannelProvider()._recover_reports(
        step, tmp_path, quantities, histories, artifacts
    )

    assert histories["probe.near-wake.fluid.velocity.y"].values == (-2.0, -5.0)
    assert histories["probe.near-wake.fluid.pressure.value"].values[-1] == 200.0
    assert histories["report.outlet-pressure"].values[-1] == 400.0
    assert histories["report.baffle-drag"].values[-1] == 10.0
    assert quantities["report.baffle-drag"].unit == "N"
    assert artifacts


def test_channel_provider_merges_report_segments_after_restart(tmp_path):
    step = Project(EXAMPLE).load_step()
    files = {
        "near_wake/0/U": "0.1 (1 2 3)\n",
        "near_wake/2/U": "2.1 (4 5 6)\n",
        "near_wake/0/p": "0.1 0.1\n",
        "near_wake/2/p": "2.1 0.2\n",
        "outlet_pressure/2/surfaceFieldValue.dat": "2.1 0.0\n",
        "baffle_drag/0/force.dat": "0.1 7 8 9\n",
        "baffle_drag/2/force.dat": "2.1 10 11 12\n",
    }
    for relative, content in files.items():
        path = tmp_path / "postProcessing" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    quantities, histories, artifacts = {}, {}, {}

    OpenFOAMChannelProvider()._recover_reports(
        step, tmp_path, quantities, histories, artifacts
    )

    assert histories["probe.near-wake.fluid.velocity.x"].abscissa == (0.1, 2.1)
    assert histories["report.baffle-drag"].values == (7.0, 10.0)
    assert len(artifacts) == len(files)


def test_channel_restart_bundle_is_rolling_deterministic_and_restorable(tmp_path):
    step = Project(EXAMPLE).load_step()
    source_case = tmp_path / "source-case"
    prepared = OpenFOAMChannelProvider(case_directory=source_case).prepare(step)
    for time_name in ("0.5", "1", "1.5", "2"):
        time_directory = source_case / time_name
        time_directory.mkdir()
        (time_directory / "U").write_bytes(f"velocity-{time_name}".encode())
        (time_directory / "p").write_bytes(f"pressure-{time_name}".encode())

    bundle, times = _write_restart_bundle(step, prepared)
    assert bundle is not None
    first_bytes = bundle.read_bytes()
    _write_restart_bundle(step, prepared)
    assert bundle.read_bytes() == first_bytes
    assert times == (1.5, 2.0)
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == [
            "restart.json",
            "times/1.5/U",
            "times/1.5/p",
            "times/2/U",
            "times/2/p",
        ]

    result = SimulationResult(
        status="completed",
        converged=True,
        provider="openfoam",
        quantities={},
        checks=(Check("execution", True, kind="runtime"),),
        artifacts={
            "restart_bundle": Artifact.from_path(
                bundle, role="restart-checkpoints", media_type="application/zip"
            )
        },
        provenance={"model_sha256": step.model.fingerprint()},
    )
    result_path = result.write(tmp_path / "source-result" / "result.json")
    resumed = replace(
        step,
        procedure=procedures.transient(
            end_time=2.5,
            initial_time_step=0.001,
            maximum_time_step=0.005,
            maximum_courant_number=0.5,
        ),
        initialization=initialization.previous_result(str(result_path)),
    )
    target = tmp_path / "resumed-case"
    OpenFOAMChannelProvider(case_directory=target).prepare(resumed)

    assert (target / "2" / "U").read_bytes() == b"velocity-2"
    assert (target / "2" / "p").read_bytes() == b"pressure-2"
    assert "startFrom latestTime;" in (target / "system" / "controlDict").read_text()
    assert "potentialFoam" not in OpenFOAMChannelProvider()._commands(resumed)
