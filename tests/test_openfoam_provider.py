import json
import math
import subprocess
from pathlib import Path

import pytest

from agentcfd import Model, boundaries, fluids, geometry, studies
from agentcfd.errors import ProviderUnavailableError, UnsupportedCaseError
from agentcfd.providers import OpenFOAMMeshControls, OpenFOAMProvider
from agentcfd.providers.openfoam import (
    _mesh_quality_quantities,
    _read_scalar_series,
    _recover_patch_data,
    _solver_converged,
)


def pipe_model(
    *,
    energy: bool = False,
    roughness: float = 0.0,
    velocity: float = 0.01,
    fully_developed: bool = False,
) -> Model:
    inlet = (
        boundaries.fully_developed_velocity_inlet(velocity)
        if fully_developed
        else boundaries.mean_velocity_inlet(velocity)
    )
    return Model(
        name="openfoam-pipe",
        study=studies.internal_flow(energy=energy),
        domain=geometry.circular_pipe(length=2.0, diameter=0.1, roughness=roughness),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=inlet,
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def test_openfoam_case_lowering_is_deterministic_and_content_addressed(tmp_path):
    provider = OpenFOAMProvider(mesh=OpenFOAMMeshControls(cross_section_cells=4, axial_cells=20))
    first = provider.prepare(pipe_model().step(), tmp_path / "first")
    second = provider.prepare(pipe_model().step(), tmp_path / "second")

    assert first.case_sha256 == second.case_sha256
    assert first.model_sha256 == second.model_sha256
    assert len(first.files) == 8
    assert all(len(digest) == 64 for digest in first.files.values())

    manifest = json.loads((first.directory / "agentcfd-case.json").read_text())
    assert manifest["case_sha256"] == first.case_sha256
    assert manifest["provider"]["execution_boundary"] == "filesystem-and-subprocess"


def test_openfoam_case_has_physical_pipe_and_expected_boundary_semantics(tmp_path):
    case = OpenFOAMProvider().prepare(pipe_model().step(), tmp_path / "case")
    mesh = (case.directory / "system" / "blockMeshDict").read_text()
    velocity = (case.directory / "0" / "U").read_text()
    pressure = (case.directory / "0" / "p").read_text()
    transport = (case.directory / "constant" / "transportProperties").read_text()
    control = (case.directory / "system" / "controlDict").read_text()

    assert "type cylinder;" in mesh
    assert mesh.count("hex (") == 5
    assert mesh.count("    arc ") == 8
    assert "arc 4 5 (0 -0.050000000000000003 0)" in mesh
    assert "inlet" in mesh and "outlet" in mesh and "wall" in mesh
    assert "value uniform (0 0 0.01);" in velocity
    assert "dimensions      [0 2 -2 0 0 0 0];" in pressure
    assert "nu              [0 2 -1 0 0 0 0]" in transport
    assert control.count("type surfaceFieldValue;") == 4
    assert "operation areaAverage;" in control
    assert "operation sum;" in control


def test_openfoam_fully_developed_inlet_uses_non_compiling_radial_expression(tmp_path):
    case = OpenFOAMProvider().prepare(
        pipe_model(fully_developed=True).step(),
        tmp_path / "case",
    )
    velocity = (case.directory / "0" / "U").read_text()

    assert "type uniformFixedValue;" in velocity
    assert "type expression;" in velocity
    assert "sqr(pos().x()) + sqr(pos().y())" in velocity
    assert "codedFixedValue" not in velocity and "exprFixedValue" not in velocity

    reference = pipe_model(fully_developed=True).step().run()
    assert reference.quantities["flow.mean_velocity"].value == pytest.approx(0.01)


@pytest.mark.parametrize(
    "model, message",
    [
        (pipe_model(energy=True), "isothermal"),
        (pipe_model(roughness=1.0e-5), "smooth pipe"),
        (pipe_model(velocity=0.03), "laminar provider range"),
    ],
)
def test_openfoam_provider_fails_closed_on_unsupported_physics(tmp_path, model, message):
    with pytest.raises(UnsupportedCaseError, match=message):
        OpenFOAMProvider().prepare(model.step(), tmp_path / "case")


def test_openfoam_provider_never_overwrites_a_case(tmp_path):
    case_directory = tmp_path / "case"
    case_directory.mkdir()
    (case_directory / "keep.txt").write_text("user data")
    with pytest.raises(FileExistsError, match="not empty"):
        OpenFOAMProvider().prepare(pipe_model().step(), case_directory)
    assert (case_directory / "keep.txt").read_text() == "user data"


def test_openfoam_run_requires_external_runtime_after_preparing(tmp_path, monkeypatch):
    provider = OpenFOAMProvider(case_directory=tmp_path / "case")
    monkeypatch.setattr(
        provider,
        "_commands",
        lambda: {"blockMesh": None, "checkMesh": None, "simpleFoam": None},
    )
    with pytest.raises(ProviderUnavailableError, match="blockMesh, checkMesh, simpleFoam"):
        provider.run(pipe_model().step())


def test_openfoam_container_execution_uses_argument_list_and_isolated_case_mount(
    tmp_path,
    monkeypatch,
):
    provider = OpenFOAMProvider(container_image="opencfd/openfoam-run:2606")
    monkeypatch.setattr("agentcfd.providers.openfoam.shutil.which", lambda name: "/usr/bin/docker")

    commands = provider._commands()
    argv = provider._execution_argv("checkMesh", commands["checkMesh"], tmp_path)

    assert argv == [
        "/usr/bin/docker",
        "run",
        "--rm",
        "-v",
        f"{tmp_path.resolve()}:/case",
        "-w",
        "/case",
        "opencfd/openfoam-run:2606",
        "checkMesh",
        "-case",
        "/case",
    ]
    descriptor = provider.descriptor()
    assert descriptor.available is True
    assert descriptor.execution_boundary == "filesystem-and-container-subprocess"


def test_openfoam_container_reports_missing_docker_clearly(tmp_path, monkeypatch):
    provider = OpenFOAMProvider(
        case_directory=tmp_path / "case",
        container_image="opencfd/openfoam-run:2606",
    )
    monkeypatch.setattr(provider, "_commands", lambda: {"docker": None})

    with pytest.raises(ProviderUnavailableError, match="requires docker on PATH"):
        provider.run(pipe_model().step())


def test_openfoam_run_recovers_an_accepted_result_end_to_end(tmp_path, monkeypatch):
    case_directory = tmp_path / "case"
    provider = OpenFOAMProvider(case_directory=case_directory)
    monkeypatch.setattr(
        provider,
        "_commands",
        lambda: {
            "blockMesh": "/runtime/blockMesh",
            "checkMesh": "/runtime/checkMesh",
            "simpleFoam": "/runtime/simpleFoam",
        },
    )

    volume_flow = math.pi * 0.1**2 / 4.0 * 0.01
    pressure_drop = 32.0 * 1.002e-3 * 2.0 * 0.01 / 0.1**2

    def fake_run(argv, **kwargs):
        name = Path(argv[0]).name
        if name == "simpleFoam":
            samples = {
                "agentcfd_inlet_flow": -volume_flow,
                "agentcfd_outlet_flow": volume_flow,
                "agentcfd_inlet_pressure": pressure_drop / 998.2,
                "agentcfd_outlet_pressure": 0.0,
            }
            for function_name, value in samples.items():
                directory = case_directory / "postProcessing" / function_name / "0"
                directory.mkdir(parents=True)
                (directory / "surfaceFieldValue.dat").write_text(
                    f"# Time value\n10 {value:.17g}\n"
                )
            final = case_directory / "10"
            final.mkdir()
            (final / "U").write_text("synthetic velocity field")
            (final / "p").write_text("synthetic pressure field")
            stdout = "Time = 10\nSIMPLE solution converged in 10 iterations\nEnd\n"
        elif name == "checkMesh":
            stdout = "    cells: 1000\nMax aspect ratio = 2\nMesh non-orthogonality Max: 3 average: 1\nMax skewness = 0.5\nMesh OK.\nEnd\n"
        else:
            stdout = "End\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("agentcfd.providers.openfoam.subprocess.run", fake_run)
    result = provider.run(pipe_model().step())

    assert result.status == "completed"
    assert result.converged is True
    assert result.accepted is True
    assert result.quantities["flow.pressure_drop"].value == pytest.approx(pressure_drop)
    assert result.quantities["flow.pressure_drop_relative_error"].value == pytest.approx(
        0.0,
        abs=1.0e-14,
    )
    assert result.quantities["mesh.cell_count"].value == 1000
    assert result.scientific_inputs["mesh_controls"] == {
        "cross_section_cells": 8,
        "axial_cells": None,
    }
    assert set(result.fields) == {"U", "p"}
    assert len(result.histories["flow.pressure_drop"].values) == 1
    assert all(check.passed for check in result.checks)


def test_openfoam_convergence_requires_explicit_solver_marker():
    assert not _solver_converged("Time = 500\nEnd\n")
    assert _solver_converged("SIMPLE solution converged in 42 iterations\nEnd\n")


def test_openfoam_scalar_history_recovery_merges_restart_directories(tmp_path):
    first = tmp_path / "postProcessing" / "agentcfd_inlet_flow" / "0"
    second = tmp_path / "postProcessing" / "agentcfd_inlet_flow" / "50"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "surfaceFieldValue.dat").write_text("# Time sum(phi)\n1 -0.1\n50 -0.2\n")
    (second / "surfaceFieldValue.dat").write_text("# Time sum(phi)\n50 -0.21\n60 -0.3\n")

    assert _read_scalar_series(tmp_path, "agentcfd_inlet_flow") == {
        1.0: -0.1,
        50.0: -0.21,
        60.0: -0.3,
    }


def test_openfoam_checkmesh_metrics_are_structured():
    quantities = _mesh_quality_quantities(
        """    cells:            128000
    Max aspect ratio = 2.5 OK.
    Mesh non-orthogonality Max: 6.62 average: 2.95
    Max skewness = 1.047 OK.
    Mesh OK.
"""
    )

    assert quantities["mesh.cell_count"].value == 128000
    assert quantities["mesh.maximum_aspect_ratio"].value == pytest.approx(2.5)
    assert quantities["mesh.maximum_non_orthogonality"].value == pytest.approx(6.62)
    assert quantities["mesh.average_non_orthogonality"].value == pytest.approx(2.95)
    assert quantities["mesh.maximum_skewness"].value == pytest.approx(1.047)


def test_openfoam_patch_recovery_computes_physical_quantities_and_checks(tmp_path):
    samples = {
        "agentcfd_inlet_flow": ("# Time sum(phi)\n1 -0.01\n2 -0.01\n"),
        "agentcfd_outlet_flow": ("# Time sum(phi)\n1 0.0099\n2 0.01\n"),
        "agentcfd_inlet_pressure": ("# Time areaAverage(p)\n1 0.30\n2 0.25\n"),
        "agentcfd_outlet_pressure": ("# Time areaAverage(p)\n1 0\n2 0\n"),
    }
    for name, content in samples.items():
        directory = tmp_path / "postProcessing" / name / "0"
        directory.mkdir(parents=True)
        (directory / "surfaceFieldValue.dat").write_text(content)

    quantities, histories, checks, message = _recover_patch_data(
        tmp_path,
        density=1000.0,
        reference_pressure_drop=250.0,
        solver_tolerance=1.0e-6,
        reference_pressure_drop_per_flow=25_000.0,
    )

    assert quantities["flow.inlet_volume_flow_rate"].value == pytest.approx(0.01)
    assert quantities["flow.mass_flow_rate"].value == pytest.approx(10.0)
    assert quantities["flow.pressure_drop"].value == pytest.approx(250.0)
    assert quantities["reference.flow.pressure_drop"].value == pytest.approx(250.0)
    assert quantities["reference.flow.pressure_drop_requested"].value == pytest.approx(250.0)
    assert histories["flow.pressure_drop"].values == pytest.approx((300.0, 250.0))
    assert all(check.passed for check in checks)
    assert "recovered" in message
