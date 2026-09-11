from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

from agentcfd import (
    Artifact,
    Check,
    SimulationResult,
    fluids,
    initialization,
    outputs,
    procedures,
    regions,
)
from agentcfd.errors import CaseIntegrityError, UnsupportedCaseError
from agentcfd.projects import Project
from agentcfd.providers.openfoam_channel import (
    OpenFOAMChannelProvider,
    _RollingRestartPublisher,
    _write_restart_bundle,
    materialize_interrupted_restart,
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
    assert "writeFormat binary;" in control
    assert "writeCompression off;" in control
    assert "agentcfd_inlet_flow" in control
    assert "near_wake" in control


def test_channel_provider_streams_command_output_to_live_log(tmp_path, monkeypatch):
    step = Project(EXAMPLE).load_step()
    provider = OpenFOAMChannelProvider(case_directory=tmp_path / "case")
    monkeypatch.setattr(
        provider,
        "_commands",
        lambda _step: {"blockMesh": "/runtime/blockMesh"},
    )

    def fake_run(argv, **kwargs):
        assert "capture_output" not in kwargs
        stream = kwargs["stdout"]
        stream.write("streamed while command is active\n")
        stream.flush()
        assert (provider.case_directory / "log.blockMesh").read_text() == (
            "streamed while command is active\n"
        )
        return subprocess.CompletedProcess(argv, 1, stdout=None, stderr=None)

    monkeypatch.setattr("agentcfd.providers.openfoam_channel.subprocess.run", fake_run)

    result = provider.run(step)

    assert result.status == "failed"
    assert (provider.case_directory / "log.blockMesh").read_text() == (
        "streamed while command is active\n"
    )


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


def test_channel_accepts_shared_pressure_loss_report(tmp_path):
    step = Project(EXAMPLE).load_step()
    step = replace(
        step,
        output=replace(
            step.output,
            reports=(
                *step.output.reports,
                outputs.pressure_loss(
                    "channel-loss", inlet="inlet", outlet="outlet"
                ),
            ),
        ),
    )

    OpenFOAMChannelProvider(case_directory=tmp_path).prepare(step)
    control = (tmp_path / "system" / "controlDict").read_text()

    assert "agentcfd_total_pressure" in control
    assert "agentcfd_loss_channel_loss_inlet" in control
    assert "agentcfd_loss_channel_loss_outlet" in control


def test_channel_accepts_shared_flow_uniformity_report(tmp_path):
    step = Project(EXAMPLE).load_step()

    OpenFOAMChannelProvider(case_directory=tmp_path).prepare(step)
    control = (tmp_path / "system/controlDict").read_text()

    assert "agentcfd_uniformity_outlet_quality" in control
    assert "operation uniformity;" in control
    assert "operation areaNormalAverage;" in control


def test_channel_lowers_reusable_internal_sections_to_sampled_planes(tmp_path):
    step = Project(EXAMPLE).load_step()
    step.model.sections(
        regions.plane(
            "upstream-section",
            origin=(0.20, 0.10, 0.05),
            normal=(2.0, 0.0, 0.0),
        ),
        regions.plane(
            "downstream-section",
            origin=(0.80, 0.10, 0.05),
            normal=(1.0, 0.0, 0.0),
        ),
    )
    step = replace(
        step,
        output=replace(
            step.output,
            reports=(
                outputs.pressure_loss(
                    "internal-loss",
                    inlet="upstream-section",
                    outlet="downstream-section",
                ),
                outputs.flow_uniformity(
                    "internal-quality", region="downstream-section"
                ),
            ),
        ),
    )

    OpenFOAMChannelProvider(case_directory=tmp_path).prepare(step)
    control = (tmp_path / "system/controlDict").read_text()

    assert control.count("regionType sampledSurface;") == 5
    assert "name upstream_section;" in control
    assert "name downstream_section;" in control
    assert "type cuttingPlane;" in control
    assert "point (0.20000000000000001 0.10000000000000001 0.050000000000000003);" in control
    assert "normal (1 0 0);" in control
    assert control.count("weightField U;") == 2
    assert "weightField phi;" not in control
    assert "operation areaNormalIntegrate;" in control
    assert "fields (U);" in control


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
            "times/1.5/U",
            "times/1.5/p",
            "times/2/U",
            "times/2/p",
            "restart.json",
        ]
        metadata = json.loads(archive.read("restart.json"))
        assert metadata["atomic_publication"] is True
        assert metadata["bounded_memory_streaming"] is True
        assert metadata["in_run_publication_count"] == 0
        assert metadata["first_in_run_publication_time"] is None
        assert metadata["latest_in_run_publication_time"] is None

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


def test_rolling_restart_publishes_stable_checkpoint_before_solver_exit(tmp_path):
    step = Project(EXAMPLE).load_step()
    case = tmp_path / "case"
    prepared = OpenFOAMChannelProvider(case_directory=case).prepare(step)
    target = tmp_path / "published" / "restart.zip"
    publisher = _RollingRestartPublisher(
        step,
        prepared,
        target,
        poll_seconds=0.005,
    )

    publisher.start()
    try:
        checkpoint = case / "0.5"
        checkpoint.mkdir()
        (checkpoint / "U").write_text("velocity")
        (checkpoint / "p").write_text("pressure")
        for _ in range(100):
            if target.is_file():
                break
            time.sleep(0.005)
        assert target.is_file()
        with zipfile.ZipFile(target) as archive:
            metadata = json.loads(archive.read("restart.json"))
            assert metadata["retained_times"] == [0.5]
            assert metadata["in_run_publication_count"] == 1
            assert metadata["first_in_run_publication_time"] == 0.5
            assert metadata["latest_in_run_publication_time"] == 0.5
            assert publisher.publication_summary == (1, 0.5, 0.5)
    finally:
        publisher.stop()


def test_rolling_restart_publication_metadata_stays_constant_size(tmp_path):
    step = Project(EXAMPLE).load_step()
    case = tmp_path / "case"
    prepared = OpenFOAMChannelProvider(case_directory=case).prepare(step)
    target = tmp_path / "published" / "restart.zip"
    publisher = _RollingRestartPublisher(step, prepared, target)

    for time_name in ("0.5", "1", "1.5", "2"):
        checkpoint = case / time_name
        checkpoint.mkdir()
        (checkpoint / "U").write_text(f"velocity-{time_name}")
        (checkpoint / "p").write_text(f"pressure-{time_name}")
        publisher._sample()
        publisher._sample()

    with zipfile.ZipFile(target) as archive:
        metadata = json.loads(archive.read("restart.json"))
    assert "in_run_publication_times" not in metadata
    assert metadata["in_run_publication_count"] == 4
    assert metadata["first_in_run_publication_time"] == 0.5
    assert metadata["latest_in_run_publication_time"] == 2.0
    assert publisher.publication_summary == (4, 0.5, 2.0)


def test_restart_atomic_replace_preserves_previous_archive_on_failure(
    tmp_path, monkeypatch
):
    step = Project(EXAMPLE).load_step()
    case = tmp_path / "case"
    prepared = OpenFOAMChannelProvider(case_directory=case).prepare(step)
    checkpoint = case / "0.5"
    checkpoint.mkdir()
    (checkpoint / "U").write_text("velocity")
    (checkpoint / "p").write_text("pressure")
    target = tmp_path / "published" / "restart.zip"
    target.parent.mkdir()
    target.write_bytes(b"previous-good-archive")

    def fail_write(*_args, **_kwargs):
        raise OSError("synthetic storage interruption")

    monkeypatch.setattr(
        "agentcfd.providers.openfoam_channel._zip_write_path",
        fail_write,
    )
    with pytest.raises(OSError, match="synthetic storage interruption"):
        _write_restart_bundle(step, prepared, target=target)

    assert target.read_bytes() == b"previous-good-archive"
    assert not list(target.parent.glob(".restart.zip.*.tmp"))


def test_interrupted_checkpoint_requires_identity_and_complete_native_fields(tmp_path):
    step = Project(EXAMPLE).load_step()
    case = tmp_path / "interrupted-case"
    prepared = OpenFOAMChannelProvider(case_directory=case).prepare(step)
    incomplete = case / "0.5"
    incomplete.mkdir()
    (incomplete / "U").write_text("incomplete")
    complete = case / "1"
    complete.mkdir()
    (complete / "U").write_text("velocity")
    (complete / "p").write_text("pressure")

    archive, latest = materialize_interrupted_restart(
        step,
        case,
        expected_analysis_sha256=prepared.analysis_sha256,
    )

    assert archive.is_file()
    assert latest == 1.0
    with zipfile.ZipFile(archive) as bundle:
        assert "times/0.5/U" not in bundle.namelist()
        assert "times/1/U" in bundle.namelist()

    (case / "system" / "fvSchemes").write_text("tampered")
    with pytest.raises(CaseIntegrityError, match="changed after preparation"):
        materialize_interrupted_restart(
            step,
            case,
            expected_analysis_sha256=prepared.analysis_sha256,
        )


def test_channel_provider_resume_restores_checkpoint_and_skips_initialization(
    tmp_path, monkeypatch
):
    step = Project(EXAMPLE).load_step()
    source = tmp_path / "source"
    prepared = OpenFOAMChannelProvider(case_directory=source).prepare(step)
    checkpoint = source / "0.5"
    checkpoint.mkdir()
    (checkpoint / "U").write_text("velocity")
    (checkpoint / "p").write_text("pressure")
    archive, _times = _write_restart_bundle(step, prepared)
    assert archive is not None

    target = tmp_path / "target"
    provider = OpenFOAMChannelProvider(case_directory=target)
    monkeypatch.setattr(
        provider,
        "_commands",
        lambda _step: {
            "potentialFoam": "/runtime/potentialFoam",
            "pimpleFoam": "/runtime/pimpleFoam",
        },
    )
    executed = []

    def fake_run(argv, **_kwargs):
        executed.append(Path(argv[0]).name)
        return subprocess.CompletedProcess(argv, 1, stdout=None, stderr=None)

    monkeypatch.setattr("agentcfd.providers.openfoam_channel.subprocess.run", fake_run)

    result = provider.run(
        step,
        restart_archive=archive,
        source_run_id="source-run",
        expected_analysis_sha256=step.fingerprint(),
    )

    assert executed == ["pimpleFoam"]
    assert (target / "0.5" / "U").read_text() == "velocity"
    assert "startFrom latestTime;" in (target / "system" / "controlDict").read_text()
    assert result.provenance["resumed_from_run_id"] == "source-run"
    assert result.quantities["restart.resumed_from_time"].value == 0.5
    assert "resume_manifest" in result.artifacts
