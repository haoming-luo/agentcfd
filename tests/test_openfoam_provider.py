import json

import pytest

from agentcfd import Model, boundaries, fluids, geometry, studies
from agentcfd.errors import ProviderUnavailableError, UnsupportedCaseError
from agentcfd.providers import OpenFOAMMeshControls, OpenFOAMProvider
from agentcfd.providers.openfoam import _solver_converged


def pipe_model(*, energy: bool = False, roughness: float = 0.0, velocity: float = 0.01) -> Model:
    return Model(
        name="openfoam-pipe",
        study=studies.internal_flow(energy=energy),
        domain=geometry.circular_pipe(length=2.0, diameter=0.1, roughness=roughness),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(velocity),
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

    assert "type cylinder;" in mesh
    assert mesh.count("hex (") == 5
    assert mesh.count("    arc ") == 8
    assert "arc 4 5 (0 -0.050000000000000003 0)" in mesh
    assert "inlet" in mesh and "outlet" in mesh and "wall" in mesh
    assert "value uniform (0 0 0.01);" in velocity
    assert "dimensions      [0 2 -2 0 0 0 0];" in pressure
    assert "nu              [0 2 -1 0 0 0 0]" in transport


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
    monkeypatch.setattr(provider, "_commands", lambda: {"blockMesh": None, "simpleFoam": None})
    with pytest.raises(ProviderUnavailableError, match="blockMesh, simpleFoam"):
        provider.run(pipe_model().step())


def test_openfoam_convergence_requires_explicit_solver_marker():
    assert not _solver_converged("Time = 500\nEnd\n")
    assert _solver_converged("SIMPLE solution converged in 42 iterations\nEnd\n")
