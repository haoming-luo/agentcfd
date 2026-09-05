from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agentcfd import fluids
from agentcfd.errors import UnsupportedCaseError
from agentcfd.projects import Project
from agentcfd.providers.openfoam_channel import OpenFOAMChannelProvider


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
