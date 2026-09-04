import json
from pathlib import Path

import jsonschema
import pytest

from agentcfd import outputs, procedures
from agentcfd.errors import CaseIntegrityError, UnsupportedCaseError
from agentcfd.providers import (
    OpenFOAMTurbulentPrecursorProvider,
    prepare_turbulent_wall_function_study,
    prepare_turbulent_wall_study,
)
from agentcfd.providers.openfoam_precursor import (
    _precursor_residual_check,
    _precursor_series,
    _tail_relative_range,
)
from agentcfd.results import Quantity

from test_openfoam_provider import pipe_model, turbulent_pipe_model


def turbulent_step():
    return turbulent_pipe_model().step(
        procedure=procedures.steady(
            relative_tolerance=1.0e-4,
            maximum_iterations=300,
        ),
        output=outputs.turbulent_internal_flow(),
    )


def test_precursor_case_is_periodic_content_addressed_and_schema_valid(tmp_path):
    provider = OpenFOAMTurbulentPrecursorProvider(
        cross_section_cells=8,
        maximum_iterations=200,
    )
    first = provider.prepare(turbulent_step(), tmp_path / "first")
    second = provider.prepare(turbulent_step(), tmp_path / "second")

    assert first.case_sha256 == second.case_sha256


    assert first.capability == (
        "openfoam.periodic-k-omega-sst-circular-pipe-precursor"
    )
    assert set(first.files) == {
        "0/U",
        "0/p",
        "0/k",
        "0/nut",
        "0/omega",
        "constant/fvOptions",
        "constant/transportProperties",
        "constant/turbulenceProperties",
        "system/blockMeshDict",
        "system/controlDict",
        "system/fvSchemes",
        "system/fvSolution",
    }
    mesh = (first.directory / "system/blockMeshDict").read_text()
    velocity = (first.directory / "0/U").read_text()
    transport = (first.directory / "constant/transportProperties").read_text()
    control = (first.directory / "system/controlDict").read_text()
    assert mesh.count("type cyclic;") == 2
    assert "neighbourPatch periodic_out;" in mesh
    assert "neighbourPatch periodic_in;" in mesh
    assert "(8 8 1)" in mesh
    assert velocity.count("type cyclic;") == 2
    assert "transportModel  Newtonian;" in transport
    assert "application     simpleFoam;" in control
    assert "type meanVelocityForce;" in (
        first.directory / "constant/fvOptions"
    ).read_text()
    manifest = json.loads((first.directory / "agentcfd-case.json").read_text())
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/openfoam-case.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)


@pytest.mark.parametrize(
    "wall_function",
    (
        "nutUBlendedWallFunction",
        "nutUSpaldingWallFunction",
        "nutkWallFunction",
    ),
)
def test_precursor_momentum_wall_function_is_explicit_and_hashed(
    tmp_path, wall_function
):
    provider = OpenFOAMTurbulentPrecursorProvider(
        cross_section_cells=8,
        maximum_iterations=20,
        nut_wall_function=wall_function,
    )
    prepared = provider.prepare(turbulent_step(), tmp_path / wall_function)

    nut = (prepared.directory / "0/nut").read_text()
    assert f"type {wall_function};" in nut


def test_precursor_rejects_unknown_momentum_wall_function():
    with pytest.raises(ValueError, match="nut_wall_function must be one of"):
        OpenFOAMTurbulentPrecursorProvider(nut_wall_function="inventedWallFunction")


def test_precursor_declares_near_wall_grading(tmp_path):
    prepared = OpenFOAMTurbulentPrecursorProvider(
        cross_section_cells=16,
        nominal_wall_cell_fraction=0.125,
    ).prepare(turbulent_step(), tmp_path / "graded")
    mesh = (prepared.directory / "system/blockMeshDict").read_text()
    assert "// agentcfdNominalWallCellFraction 0.125" in mesh
    assert "// agentcfdWallToCoreExpansionRatio" in mesh


def test_fixed_wall_cell_study_prepares_three_content_addressed_cases(tmp_path):
    study = prepare_turbulent_wall_study(turbulent_step(), tmp_path / "study")
    payload = study.to_dict()

    assert payload["schema"] == "agentcfd.openfoam-turbulent-wall-study/0.1"
    assert payload["nominal_wall_cell_fraction"] == 0.0625
    assert [case["cross_section_cells"] for case in payload["cases"]] == [8, 16, 32]
    assert [case["maximum_iterations"] for case in payload["cases"]] == [
        1000,
        4000,
        6000,
    ]
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "schemas/openfoam-turbulent-wall-study.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(payload)
    for case in payload["cases"]:
        mesh = (
            study.directory / case["directory"] / "system/blockMeshDict"
        ).read_text()
        assert "// agentcfdNominalWallCellFraction 0.0625" in mesh

    with pytest.raises(FileExistsError, match="not empty"):
        prepare_turbulent_wall_study(turbulent_step(), study.directory)


def test_wall_function_study_prepares_identical_mesh_cases(tmp_path):
    study = prepare_turbulent_wall_function_study(
        turbulent_step(),
        tmp_path / "wall-functions",
    )
    payload = study.to_dict()

    assert payload["schema"] == (
        "agentcfd.openfoam-turbulent-wall-function-study/0.1"
    )
    assert payload["cross_section_cells"] == 16
    assert [case["nut_wall_function"] for case in payload["cases"]] == [
        "nutUBlendedWallFunction",
        "nutUSpaldingWallFunction",
        "nutkWallFunction",
    ]
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "schemas/openfoam-turbulent-wall-function-study.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(payload)
    mesh_hashes = set()
    for case in payload["cases"]:
        case_directory = study.directory / case["directory"]
        mesh_hashes.add((case_directory / "system/blockMeshDict").read_text())
        assert f"type {case['nut_wall_function']};" in (
            case_directory / "0/nut"
        ).read_text()
    assert len(mesh_hashes) == 1


def test_prepared_precursor_binds_mesh_and_iteration_runtime_controls(tmp_path):
    source = tmp_path / "source"
    OpenFOAMTurbulentPrecursorProvider(
        cross_section_cells=8,
        nominal_wall_cell_fraction=0.0625,
        maximum_iterations=1000,
    ).prepare(turbulent_step(), source)

    changed = OpenFOAMTurbulentPrecursorProvider(
        case_directory=source,
        cross_section_cells=16,
        nominal_wall_cell_fraction=0.0625,
        maximum_iterations=1000,
    )
    with pytest.raises(CaseIntegrityError, match="mesh resolution differs"):
        changed.run_prepared(turbulent_step())

    changed = OpenFOAMTurbulentPrecursorProvider(
        case_directory=source,
        cross_section_cells=8,
        nominal_wall_cell_fraction=0.0625,
        maximum_iterations=2000,
    )
    with pytest.raises(CaseIntegrityError, match="iteration limit differs"):
        changed.run_prepared(turbulent_step())


def test_precursor_rejects_extreme_cumulative_wall_grading_before_meshing(tmp_path):
    provider = OpenFOAMTurbulentPrecursorProvider(
        cross_section_cells=32,
        nominal_wall_cell_fraction=0.125,
    )
    with pytest.raises(UnsupportedCaseError, match="estimated axial-to-smallest-radial"):
        provider.prepare(turbulent_step(), tmp_path / "extreme")


def test_precursor_rejects_laminar_or_changed_prepared_case(tmp_path):
    provider = OpenFOAMTurbulentPrecursorProvider(case_directory=tmp_path / "case")
    with pytest.raises(Exception, match="k-omega SST"):
        provider.prepare(pipe_model().step())

    provider.prepare(turbulent_step())
    velocity = tmp_path / "case/0/U"
    velocity.write_text(velocity.read_text() + "\n// changed\n")
    with pytest.raises(Exception, match="missing or changed"):
        provider.run_prepared(turbulent_step())


def test_precursor_log_and_residual_evidence_is_fail_closed():
    log = """
Time = 9
smoothSolver:  Solving for Uz, Initial residual = 0.002, Final residual = 1e-08, No Iterations 3
smoothSolver:  Solving for k, Initial residual = 0.0002, Final residual = 1e-08, No Iterations 2
smoothSolver:  Solving for omega, Initial residual = 0.0003, Final residual = 1e-08, No Iterations 2
Pressure gradient source: uncorrected Ubar = 0.9999, pressure gradient = 0.091
Time = 10
smoothSolver:  Solving for Uz, Initial residual = 8e-05, Final residual = 1e-09, No Iterations 2
smoothSolver:  Solving for k, Initial residual = 7e-05, Final residual = 1e-09, No Iterations 2
smoothSolver:  Solving for omega, Initial residual = 6e-05, Final residual = 1e-09, No Iterations 2
Pressure gradient source: uncorrected Ubar = 1.0, pressure gradient = 0.09
"""
    velocities, gradients = _precursor_series(log)
    assert velocities == {9.0: 0.9999, 10.0: 1.0}
    assert gradients == {9.0: 0.091, 10.0: 0.09}
    assert _tail_relative_range(tuple(gradients.values()), 2) == pytest.approx(1 / 90)

    quantities = {
        "solver.initial_residual.p": Quantity(5e-5, "1"),
        "solver.final_residual.p": Quantity(5e-5, "1"),
        "solver.initial_residual.Uz": Quantity(8e-5, "1"),
        "solver.initial_residual.k": Quantity(7e-5, "1"),
        "solver.initial_residual.omega": Quantity(6e-5, "1"),
    }
    assert _precursor_residual_check(quantities, tolerance=1e-4).passed
    del quantities["solver.initial_residual.omega"]
    assert not _precursor_residual_check(quantities, tolerance=1e-4).passed
