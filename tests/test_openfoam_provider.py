import hashlib
import json
import math
import subprocess
from pathlib import Path

import jsonschema
import pytest

from agentcfd import Model, boundaries, fluids, geometry, outputs, procedures, studies
from agentcfd.errors import CaseIntegrityError, ProviderUnavailableError, UnsupportedCaseError
from agentcfd.providers import (
    OpenFOAMMeshControls,
    OpenFOAMProvider,
    OpenFOAMValidationPolicy,
    OpenFOAMTurbulentPrecursorProvider,
    prepare_pipe_grid_study,
)
from agentcfd.results import Artifact, Check, FieldRecord, SimulationResult
from agentcfd.providers.openfoam import (
    _bounded_pipe_convergence,
    _container_image_identity,
    _mesh_controls_from_case,
    _mesh_metric_checks,
    _mesh_quality_quantities,
    _nominal_wall_cell_height,
    _outer_residual_check,
    _pressure_drop_stability_check,
    _read_scalar_series,
    _read_y_plus_series,
    _recover_turbulence_data,
    _runtime_version,
    _runtime_version_key,
    _recover_patch_data,
    _solver_converged,
    _solver_residual_evidence,
    _unexpected_case_entries,
    _wall_normal_expansion_ratio,
)
from agentcfd.results import Check, History, Quantity


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


def turbulent_pipe_model(*, velocity: float = 1.0) -> Model:
    return Model(
        name="turbulent-openfoam-pipe",
        study=studies.internal_flow(
            turbulence="k-omega-sst",
            wall_treatment="blended-wall-functions",
        ),
        domain=geometry.circular_pipe(length=3.0, diameter=0.1),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.turbulent_mean_velocity_inlet(
            velocity,
            intensity=0.05,
            length_scale=0.007,
        ),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def accepted_precursor(
    tmp_path: Path,
    step,
    *,
    cross_section_cells: int = 8,
    nominal_wall_cell_fraction: float | None = None,
) -> Path:
    source = tmp_path / "precursor"
    prepared = OpenFOAMTurbulentPrecursorProvider(
        cross_section_cells=cross_section_cells,
        nominal_wall_cell_fraction=nominal_wall_cell_fraction,
    ).prepare(step, source)
    solution = source / "100"
    solution.mkdir()
    field_units = {
        "U": "m/s",
        "p": "m^2/s^2",
        "k": "m^2/s^2",
        "omega": "1/s",
        "nut": "m^2/s",
    }
    fields = {}
    artifacts = {}
    mesh_sha256 = "a" * 64
    mesh_manifest = source / "agentcfd-mesh.json"
    mesh_manifest.write_text(
        json.dumps(
            {
                "schema": "agentcfd.openfoam-mesh/0.1",
                "mesh_sha256": mesh_sha256,
                "root": "constant/polyMesh",
                "files": {},
            }
        )
    )
    artifacts["case_manifest"] = Artifact.from_path(
        source / "agentcfd-case.json", role="precursor-evidence"
    )
    artifacts["mesh_manifest"] = Artifact.from_path(
        mesh_manifest, role="precursor-evidence"
    )
    for name, unit in field_units.items():
        path = solution / name
        path.write_text(f"deterministic {name} field\n")
        fields[name] = FieldRecord(
            unit=unit,
            location="cell",
            artifact=str(path),
            mesh_sha256=mesh_sha256,
        )
        artifacts[f"field_{name}"] = Artifact.from_path(path, role="precursor-evidence")
    result = SimulationResult(
        name="test-precursor",
        status="completed",
        converged=True,
        provider="openfoam-periodic-precursor",
        quantities={"flow.pressure_gradient": Quantity(85.0, "Pa/m")},
        checks=(
            Check(
                name="precursor-verified",
                passed=True,
                kind="verification",
            ),
        ),
        fields=fields,
        artifacts=artifacts,
        scientific_inputs={
            "precursor": {
                "cross_section_cells": cross_section_cells,
                "axial_cells": 1,
                "nominal_wall_cell_fraction": nominal_wall_cell_fraction,
                "periodic_end_planes": True,
            }
        },
        provenance={
            "model_sha256": step.model.fingerprint(),
            "mesh_sha256": mesh_sha256,
            "case_sha256": prepared.case_sha256,
            "provider_version": "2606",
        },
    )
    result.write(source / "agentcfd-result.json")
    return source


def test_turbulent_pipe_maps_only_an_accepted_content_addressed_precursor(tmp_path):
    step = turbulent_pipe_model().step(
        procedure=procedures.steady(relative_tolerance=1e-4, maximum_iterations=300),
        output=outputs.turbulent_internal_flow(),
    )
    source = accepted_precursor(tmp_path, step)
    provider = OpenFOAMProvider(
        precursor_case=source,
        mesh=OpenFOAMMeshControls(cross_section_cells=8, axial_cells=120),
    )
    prepared = provider.prepare(step, tmp_path / "target")

    assert "agentcfd-precursor-map.json" in prepared.files
    assert "system/mapFieldsDict" in prepared.files
    contract = json.loads(
        (prepared.directory / "agentcfd-precursor-map.json").read_text()
    )
    case_schema = json.loads(
        (Path(__file__).parents[1] / "schemas/openfoam-case.schema.json").read_text()
    )
    mapping_schema = json.loads(
        (
            Path(__file__).parents[1]
            / "schemas/openfoam-precursor-map.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(case_schema).validate(
        json.loads((prepared.directory / "agentcfd-case.json").read_text())
    )
    jsonschema.Draft202012Validator(mapping_schema).validate(contract)
    assert contract["method"] == "mapNearest"
    assert contract["source_time"] == "100"
    assert set(contract["mapped_fields"]) == {"U", "p", "k", "omega", "nut"}
    assert contract["source_model_sha256"] == step.model.fingerprint()
    assert len(contract["source_case_manifest_sha256"]) == 64
    assert len(contract["source_mesh_manifest_sha256"]) == 64
    assert contract["source_pressure_gradient_pa_per_m"] == 85.0
    assert "patchMap       ();" in (
        prepared.directory / "system/mapFieldsDict"
    ).read_text()
    assert "type zeroGradient;" in (prepared.directory / "0/k").read_text()
    assert "extrapolateProfile yes;" in (prepared.directory / "0/U").read_text()
    assert "relTol 0;" in (prepared.directory / "system/fvSolution").read_text()

    (source / "100/k").write_text("changed\n")
    with pytest.raises(CaseIntegrityError, match="no longer matches"):
        provider.run_prepared(step, prepared.directory)


def test_turbulent_precursor_mapping_rejects_resolution_mismatch(tmp_path):
    step = turbulent_pipe_model().step(
        procedure=procedures.steady(relative_tolerance=1e-4, maximum_iterations=300),
        output=outputs.turbulent_internal_flow(),
    )
    source = accepted_precursor(tmp_path, step, cross_section_cells=8)
    provider = OpenFOAMProvider(
        precursor_case=source,
        mesh=OpenFOAMMeshControls(cross_section_cells=16, axial_cells=120),
    )
    with pytest.raises(CaseIntegrityError, match="same cross-section resolution"):
        provider.prepare(step, tmp_path / "target")


def test_turbulent_precursor_mapping_rejects_near_wall_mismatch(tmp_path):
    step = turbulent_pipe_model().step(
        procedure=procedures.steady(relative_tolerance=1e-4, maximum_iterations=300),
        output=outputs.turbulent_internal_flow(),
    )
    source = accepted_precursor(
        tmp_path,
        step,
        cross_section_cells=16,
        nominal_wall_cell_fraction=0.0625,
    )
    provider = OpenFOAMProvider(
        precursor_case=source,
        mesh=OpenFOAMMeshControls(
            cross_section_cells=16,
            axial_cells=120,
            nominal_wall_cell_fraction=0.125,
        ),
    )
    with pytest.raises(CaseIntegrityError, match="same near-wall grading"):
        provider.prepare(step, tmp_path / "target")


def test_openfoam_case_lowering_is_deterministic_and_content_addressed(tmp_path):
    provider = OpenFOAMProvider(mesh=OpenFOAMMeshControls(cross_section_cells=4, axial_cells=20))
    first = provider.prepare(pipe_model().step(), tmp_path / "first")
    second = provider.prepare(pipe_model().step(), tmp_path / "second")

    assert first.case_sha256 == second.case_sha256
    assert first.model_sha256 == second.model_sha256
    assert first.analysis_sha256 == second.analysis_sha256
    assert len(first.files) == 8
    assert all(len(digest) == 64 for digest in first.files.values())
    assert all(
        hashlib.sha256((first.directory / relative).read_bytes()).hexdigest() == digest
        for relative, digest in first.files.items()
    )

    manifest = json.loads((first.directory / "agentcfd-case.json").read_text())
    assert manifest["case_sha256"] == first.case_sha256
    assert manifest["analysis_sha256"] == first.analysis_sha256
    assert manifest["provider"]["execution_boundary"] == "filesystem-and-subprocess"
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "openfoam-case.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)


def test_prepared_case_recovers_mesh_controls_from_verified_input(tmp_path):
    provider = OpenFOAMProvider(
        mesh=OpenFOAMMeshControls(cross_section_cells=4, axial_cells=20)
    )
    prepared = provider.prepare(pipe_model().step(), tmp_path / "case")

    recovered = _mesh_controls_from_case(prepared.directory)

    assert recovered == OpenFOAMMeshControls(
        cross_section_cells=4,
        axial_cells=20,
    )
    assert _unexpected_case_entries(prepared) == ()


def test_prepared_case_rejects_duplicate_manifest_keys(tmp_path):
    provider = OpenFOAMProvider(case_directory=tmp_path / "case")
    step = pipe_model().step()
    prepared = provider.prepare(step)
    manifest = prepared.directory / "agentcfd-case.json"
    manifest.write_text(
        '{"schema":"agentcfd.openfoam-case/0.1",'
        '"schema":"agentcfd.openfoam-case/0.1"}'
    )

    with pytest.raises(CaseIntegrityError, match="missing or invalid"):
        provider.run_prepared(step)


def test_openfoam_grid_study_prepares_same_model_geometrically_similar_cases(tmp_path):
    study = prepare_pipe_grid_study(
        pipe_model(fully_developed=True).step(),
        tmp_path / "study",
    )
    payload = study.to_dict()

    assert payload["refinement_ratio"] == 2.0
    assert [case["expected_cell_count"] for case in payload["cases"]] == [
        12800,
        102400,
        819200,
    ]
    assert len({case["case_sha256"] for case in payload["cases"]}) == 3
    identities = {
        json.loads(
            (study.directory / case["directory"] / "agentcfd-case.json").read_text()
        )["model_sha256"]
        for case in payload["cases"]
    }
    assert identities == {study.model_sha256}
    assert (study.directory / "agentcfd-grid-study.json").is_file()
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "openfoam-grid-study.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_openfoam_grid_study_rejects_non_similar_or_nonempty_plans(tmp_path):
    with pytest.raises(ValueError, match="one refinement ratio"):
        prepare_pipe_grid_study(
            pipe_model(fully_developed=True).step(),
            tmp_path / "invalid",
            cross_section_cells=(4, 8, 12),
        )

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("user data")
    with pytest.raises(FileExistsError, match="not empty"):
        prepare_pipe_grid_study(
            pipe_model(fully_developed=True).step(),
            occupied,
        )
    assert (occupied / "keep.txt").read_text() == "user data"


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


def test_near_wall_grading_solves_geometric_series_and_is_recoverable(tmp_path):
    fraction = 1.0 / 8.0
    ratios = [
        _wall_normal_expansion_ratio(cells, fraction)
        for cells in (8, 16, 32)
    ]
    assert ratios[0] == pytest.approx(1.0)
    assert 0.0 < ratios[2] < ratios[1] < ratios[0]
    for cells, expansion in zip((8, 16, 32), ratios):
        per_cell = expansion ** (1.0 / (cells - 1))
        recovered_fraction = 1.0 / sum(per_cell**index for index in range(cells))
        assert recovered_fraction == pytest.approx(fraction, rel=1.0e-12)

    heights = [
        _nominal_wall_cell_height(
            radius=0.05,
            cross_cells=cells,
            nominal_wall_cell_fraction=fraction,
        )
        for cells in (8, 16, 32)
    ]
    assert heights[0] == pytest.approx(heights[1])
    assert heights[1] == pytest.approx(heights[2])

    prepared = OpenFOAMProvider(
        mesh=OpenFOAMMeshControls(
            cross_section_cells=16,
            axial_cells=120,
            nominal_wall_cell_fraction=fraction,
        )
    ).prepare(turbulent_pipe_model().step(), tmp_path / "graded")
    mesh = (prepared.directory / "system/blockMeshDict").read_text()
    assert "// agentcfdNominalWallCellFraction 0.125" in mesh
    assert mesh.count("simpleGrading (1 1 1)") == 1
    assert _mesh_controls_from_case(prepared.directory) == OpenFOAMMeshControls(
        cross_section_cells=16,
        axial_cells=120,
        nominal_wall_cell_fraction=fraction,
    )


def test_openfoam_fully_developed_inlet_uses_non_compiling_radial_expression(tmp_path):
    case = OpenFOAMProvider().prepare(
        pipe_model(fully_developed=True).step(),
        tmp_path / "case",
    )
    velocity = (case.directory / "0" / "U").read_text()

    assert "type uniformFixedValue;" in velocity
    assert "type expression;" in velocity
    assert "sqr(pos().x()) + sqr(pos().y())" in velocity
    assert "weightSum(" in velocity
    assert "7.853981633974484" in velocity
    assert "codedFixedValue" not in velocity and "exprFixedValue" not in velocity

    reference = pipe_model(fully_developed=True).step().run()
    assert reference.quantities["flow.mean_velocity"].value == pytest.approx(0.01)


def test_openfoam_turbulent_pipe_lowering_is_explicit_and_auditable(tmp_path):
    step = turbulent_pipe_model().step(
        procedure=procedures.steady(relative_tolerance=1.0e-6),
        output=outputs.turbulent_internal_flow(),
    )
    prepared = OpenFOAMProvider(
        mesh=OpenFOAMMeshControls(cross_section_cells=16, axial_cells=120)
    ).prepare(step, tmp_path / "case")

    assert prepared.capability == "openfoam.steady-rans-smooth-circular-pipe"
    assert set(prepared.files) >= {"0/U", "0/p", "0/k", "0/omega", "0/nut"}
    velocity = (prepared.directory / "0/U").read_text()
    turbulence = (prepared.directory / "constant/turbulenceProperties").read_text()
    omega = (prepared.directory / "0/omega").read_text()
    nut = (prepared.directory / "0/nut").read_text()
    control = (prepared.directory / "system/controlDict").read_text()
    solution = (prepared.directory / "system/fvSolution").read_text()

    assert "type flowRateInletVelocity;" in velocity
    assert "extrapolateProfile yes;" in velocity
    assert "RASModel        kOmegaSST;" in turbulence
    assert "type omegaWallFunction;" in omega
    assert "blending binomial;" in omega
    assert "type nutUBlendedWallFunction;" in nut
    assert "type yPlus;" in control
    assert '"(k|omega)"' in solution
    manifest = json.loads((prepared.directory / "agentcfd-case.json").read_text())
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/openfoam-case.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)


def test_openfoam_turbulence_requires_matching_study_inlet_and_reynolds(tmp_path):
    laminar_with_turbulent_inlet = Model(
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=3.0, diameter=0.1),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.turbulent_mean_velocity_inlet(
            1.0,
            intensity=0.05,
            length_scale=0.007,
        ),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )
    with pytest.raises(UnsupportedCaseError, match="requires a turbulent Study"):
        OpenFOAMProvider().prepare(laminar_with_turbulent_inlet.step(), tmp_path / "a")
    with pytest.raises(UnsupportedCaseError, match="turbulent provider range"):
        OpenFOAMProvider().prepare(
            turbulent_pipe_model(velocity=0.02).step(
                output=outputs.turbulent_internal_flow()
            ),
            tmp_path / "b",
        )


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


def test_prepared_openfoam_case_is_verified_before_reuse(tmp_path):
    step = pipe_model().step()
    directory = tmp_path / "case"
    provider = OpenFOAMProvider(case_directory=directory)
    prepared = provider.prepare(step)

    loaded = provider._load_prepared_case(step)
    assert loaded.case_sha256 == prepared.case_sha256
    assert loaded.files == prepared.files

    velocity = directory / "0" / "U"
    velocity.write_text(velocity.read_text() + "\n// changed\n")
    with pytest.raises(CaseIntegrityError, match="has changed: 0/U"):
        provider.run_prepared(step)


def test_prepared_openfoam_case_rejects_model_mismatch_and_path_escape(tmp_path):
    directory = tmp_path / "case"
    provider = OpenFOAMProvider(case_directory=directory)
    provider.prepare(pipe_model().step())

    with pytest.raises(CaseIntegrityError, match="different scientific model"):
        provider.run_prepared(pipe_model(velocity=0.005).step())

    manifest_path = directory / "agentcfd-case.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["../outside"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CaseIntegrityError, match="escapes the case directory"):
        provider.run_prepared(pipe_model().step())


def test_prepared_openfoam_case_rejects_analysis_procedure_mismatch(tmp_path):
    directory = tmp_path / "case"
    provider = OpenFOAMProvider(case_directory=directory)
    provider.prepare(pipe_model().step(procedure=procedures.steady()))

    changed = pipe_model().step(
        procedure=procedures.steady(relative_tolerance=1.0e-6)
    )
    with pytest.raises(CaseIntegrityError, match="different analysis procedure"):
        provider.run_prepared(changed)


def test_prepared_openfoam_case_rejects_mixed_prior_execution_evidence(tmp_path):
    directory = tmp_path / "case"
    provider = OpenFOAMProvider(case_directory=directory)
    step = pipe_model().step()
    provider.prepare(step)
    (directory / "postProcessing").mkdir()

    with pytest.raises(CaseIntegrityError, match="already contains execution output"):
        provider.run_prepared(step)


def test_prepared_openfoam_case_rejects_unrecorded_solver_inputs(tmp_path):
    directory = tmp_path / "case"
    provider = OpenFOAMProvider(case_directory=directory)
    step = pipe_model().step()
    provider.prepare(step)
    (directory / "system" / "fvOptions").write_text("unexpected source term")

    with pytest.raises(CaseIntegrityError, match="unrecorded entries.*system/fvOptions"):
        provider.run_prepared(step)


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


def test_openfoam_timeout_stops_exact_container_and_removes_cidfile(tmp_path, monkeypatch):
    provider = OpenFOAMProvider(
        case_directory=tmp_path / "case",
        container_image="opencfd/openfoam-run:2606",
        timeout_seconds=1,
    )
    monkeypatch.setattr("agentcfd.providers.openfoam.shutil.which", lambda name: "/docker")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            cidfile.write_text("a" * 64)
            raise subprocess.TimeoutExpired(argv, 1, output="partial output")
        if argv[1:3] == ["rm", "--force"]:
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        if argv[1:3] == ["image", "inspect"]:
            payload = [{"Id": f"sha256:{'b' * 64}"}]
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr("agentcfd.providers.openfoam.subprocess.run", fake_run)

    result = provider.run(pipe_model(fully_developed=True).step())

    assert result.status == "failed"
    assert any(argv[1:3] == ["rm", "--force"] and argv[3] == "a" * 64 for argv in calls)
    assert not (tmp_path / "case" / ".agentcfd-blockMesh.cid").exists()
    assert "Stopped timed-out container" in (
        tmp_path / "case" / "log.blockMesh"
    ).read_text()


def test_openfoam_keyboard_interrupt_stops_exact_container(tmp_path, monkeypatch):
    provider = OpenFOAMProvider(
        case_directory=tmp_path / "case",
        container_image="opencfd/openfoam-run:2606",
    )
    monkeypatch.setattr("agentcfd.providers.openfoam.shutil.which", lambda name: "/docker")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            cidfile.write_text("c" * 64)
            raise KeyboardInterrupt
        if argv[1:3] == ["rm", "--force"]:
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr("agentcfd.providers.openfoam.subprocess.run", fake_run)

    with pytest.raises(KeyboardInterrupt):
        provider.run(pipe_model(fully_developed=True).step())

    assert any(
        argv[1:3] == ["rm", "--force"] and argv[3] == "c" * 64
        for argv in calls
    )
    assert not (tmp_path / "case" / ".agentcfd-blockMesh.cid").exists()


def test_openfoam_container_image_identity_is_immutable_and_structured(monkeypatch):
    digest = "a" * 64
    payload = [
        {
            "Id": f"sha256:{digest}",
            "RepoDigests": [f"opencfd/openfoam-run@sha256:{'b' * 64}"],
            "Os": "linux",
            "Architecture": "arm64",
        }
    ]
    monkeypatch.setattr(
        "agentcfd.providers.openfoam.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    identity = _container_image_identity(
        "/usr/bin/docker",
        "opencfd/openfoam-run:2606",
        timeout_seconds=60,
    )

    assert identity["identity_verified"] is True
    assert identity["image_id"] == f"sha256:{digest}"
    assert identity["os"] == "linux"
    assert identity["architecture"] == "arm64"


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
                history = "".join(
                    f"{iteration} {value:.17g}\n" for iteration in range(6, 11)
                )
                (directory / "surfaceFieldValue.dat").write_text(
                    "# Time value\n" + history
                )
            final = case_directory / "10"
            final.mkdir()
            (final / "U").write_text("synthetic velocity field")
            (final / "p").write_text("synthetic pressure field")
            stdout = (
                "version=2606\nTime = 10\n"
                "smoothSolver: Solving for Ux, Initial residual = 1e-9, "
                "Final residual = 1e-10, No Iterations 1\n"
                "GAMG: Solving for p, Initial residual = 1e-9, "
                "Final residual = 1e-10, No Iterations 1\n"
                "SIMPLE solution converged in 10 iterations\nEnd\n"
            )
        elif name == "checkMesh":
            stdout = "    cells: 12800\nMax aspect ratio = 2\nMesh non-orthogonality Max: 3 average: 1\nMax skewness = 0.5\nMesh OK.\nEnd\n"
        else:
            mesh = case_directory / "constant" / "polyMesh"
            mesh.mkdir(parents=True)
            (mesh / "points").write_text("synthetic mesh points")
            stdout = "End\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("agentcfd.providers.openfoam.subprocess.run", fake_run)
    result = provider.run(pipe_model(fully_developed=True).step())

    assert result.status == "completed"
    assert result.converged is True
    assert result.accepted is True
    assert result.quantities["flow.pressure_drop"].value == pytest.approx(pressure_drop)
    assert result.quantities["flow.pressure_drop_relative_error"].value == pytest.approx(
        0.0,
        abs=1.0e-14,
    )
    assert result.quantities["mesh.cell_count"].value == 12800
    assert result.quantities["mesh.expected_cell_count"].value == 12800
    assert result.quantities["runtime.total_wall_seconds"].value >= 0.0
    assert set(result.provenance["command_return_codes"]) == {
        "blockMesh",
        "checkMesh",
        "simpleFoam",
    }
    assert set(result.provenance["command_wall_seconds"]) == {
        "blockMesh",
        "checkMesh",
        "simpleFoam",
    }
    assert result.quantities["flow.reynolds_number"].value == pytest.approx(
        998.2 * 0.01 * 0.1 / 1.002e-3
    )
    assert "flow.laminar_entrance_length_estimate" not in result.quantities
    assert result.scientific_inputs["mesh_controls"] == {
        "cross_section_cells": 8,
        "axial_cells": 40,
        "nominal_wall_cell_fraction": None,
    }
    assert result.scientific_inputs["validation_policy"] == {
        "maximum_relative_mass_imbalance": 1.0e-6,
        "maximum_relative_pressure_error": 0.02,
        "maximum_relative_inlet_flow_error": 0.01,
        "maximum_relative_pressure_drop_drift": 1.0e-4,
        "maximum_relative_turbulent_pressure_drop_drift": 5.0e-4,
        "minimum_steady_samples": 5,
        "minimum_precursor_steady_samples": 50,
        "maximum_mesh_non_orthogonality": 65.0,
        "maximum_mesh_skewness": 4.0,
        "maximum_mesh_aspect_ratio": 50.0,
        "minimum_wall_y_plus": 30.0,
        "maximum_wall_y_plus": 300.0,
        "maximum_relative_turbulent_friction_error": 0.15,
        "maximum_turbulent_outer_residual": 1.0e-3,
        "validated_runtime_versions": ("2606",),
    }
    assert set(result.fields) == {"U", "p"}
    assert result.fields["U"].mesh_sha256 == result.provenance["mesh_sha256"]
    assert result.artifacts["mesh_manifest"].media_type == "application/json"
    mesh_manifest = json.loads(
        (case_directory / "agentcfd-mesh.json").read_text(encoding="utf-8")
    )
    mesh_schema = json.loads(
        (
            Path(__file__).parents[1] / "schemas" / "openfoam-mesh.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(mesh_schema).validate(mesh_manifest)
    assert len(result.histories["flow.pressure_drop"].values) == 5
    assert all(check.passed for check in result.checks)
    assert {
        "maximum-non-orthogonality-limit",
        "maximum-skewness-limit",
        "maximum-aspect-ratio-limit",
    }.issubset({check.name for check in result.checks})
    assert result.provenance["provider_version"] == "2606"
    assert next(
        check for check in result.checks if check.name == "requested-output-completeness"
    ).passed


def test_openfoam_rejects_unsupported_output_request(tmp_path):
    step = pipe_model().step(
        output=outputs.OutputRequest(
            fields=("fluid.temperature",),
            histories=("flow.pressure_drop",),
        )
    )

    with pytest.raises(UnsupportedCaseError, match="fluid.temperature"):
        OpenFOAMProvider().prepare(step, tmp_path / "case")


def test_openfoam_missing_requested_native_field_fails_acceptance(
    tmp_path, monkeypatch
):
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
        if name == "blockMesh":
            mesh = case_directory / "constant" / "polyMesh"
            mesh.mkdir(parents=True)
            (mesh / "points").write_text("synthetic mesh points")
            stdout = "version=2606\nEnd\n"
        elif name == "checkMesh":
            stdout = "version=2606\n    cells: 12800\nMesh OK.\nEnd\n"
        else:
            samples = {
                "agentcfd_inlet_flow": -volume_flow,
                "agentcfd_outlet_flow": volume_flow,
                "agentcfd_inlet_pressure": pressure_drop / 998.2,
                "agentcfd_outlet_pressure": 0.0,
            }
            for function_name, value in samples.items():
                directory = case_directory / "postProcessing" / function_name / "0"
                directory.mkdir(parents=True)
                history = "".join(
                    f"{iteration} {value:.17g}\n" for iteration in range(6, 11)
                )
                (directory / "surfaceFieldValue.dat").write_text(
                    "# Time value\n" + history
                )
            final = case_directory / "10"
            final.mkdir()
            (final / "U").write_text("synthetic velocity field")
            stdout = (
                "version=2606\nTime = 10\n"
                "smoothSolver: Solving for Ux, Initial residual = 1e-9, "
                "Final residual = 1e-10, No Iterations 1\n"
                "GAMG: Solving for p, Initial residual = 1e-9, "
                "Final residual = 1e-10, No Iterations 1\n"
                "SIMPLE solution converged in 10 iterations\nEnd\n"
            )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("agentcfd.providers.openfoam.subprocess.run", fake_run)
    result = provider.run(pipe_model().step())

    completeness = next(
        check for check in result.checks if check.name == "requested-output-completeness"
    )
    assert completeness.passed is False
    assert completeness.value == "fluid.pressure"
    assert "flow.laminar_entrance_length_estimate" in result.quantities
    assert not next(
        check
        for check in result.checks
        if check.name == "pressure-reference-applicability"
    ).passed
    assert result.accepted is False


def test_openfoam_unknown_runtime_version_fails_scientific_acceptance(
    tmp_path, monkeypatch
):
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

    def fake_run(argv, **kwargs):
        name = Path(argv[0]).name
        if name == "blockMesh":
            mesh = case_directory / "constant" / "polyMesh"
            mesh.mkdir(parents=True)
            (mesh / "points").write_text("synthetic mesh points")
        return subprocess.CompletedProcess(
            argv, 1, stdout="version=unknown\n", stderr=""
        )

    monkeypatch.setattr("agentcfd.providers.openfoam.subprocess.run", fake_run)
    result = provider.run(pipe_model().step())

    version_check = next(
        check for check in result.checks if check.name == "openfoam-runtime-version"
    )
    assert version_check.passed is False
    assert version_check.value == "unknown"
    assert result.accepted is False


def test_openfoam_convergence_requires_explicit_solver_marker():
    assert not _solver_converged("Time = 500\nEnd\n")
    assert _solver_converged("SIMPLE solution converged in 42 iterations\nEnd\n")


def test_openfoam_runtime_version_normalizes_official_release_prefix():
    logs = {"simpleFoam": "|  Version:  v2606  |\n"}
    assert _runtime_version(logs, "fallback") == "v2606"
    assert _runtime_version_key("v2606") == "2606"
    assert _runtime_version_key("2606") == "2606"


def test_openfoam_solver_residuals_are_recovered_as_diagnostics():
    quantities, histories = _solver_residual_evidence(
        """Time = 1
smoothSolver: Solving for Ux, Initial residual = 0.1, Final residual = 0.002, No Iterations 2
GAMG: Solving for p, Initial residual = 0.2, Final residual = 0.003, No Iterations 3
Time = 2
smoothSolver: Solving for Ux, Initial residual = 0.01, Final residual = 2e-05, No Iterations 1
GAMG: Solving for p, Initial residual = 0.02, Final residual = 3e-05, No Iterations 2
"""
    )

    assert quantities["solver.initial_residual.Ux"].value == pytest.approx(0.01)
    assert quantities["solver.final_residual.p"].value == pytest.approx(3.0e-5)
    assert quantities["solver.linear_iterations.p"].value == 2
    assert histories["solver.initial_residual.Ux"].abscissa == (1.0, 2.0)
    assert histories["solver.final_residual.Ux"].values == pytest.approx(
        (0.002, 2.0e-5)
    )


def test_outer_residual_check_requires_pressure_and_velocity_below_target():
    quantities, _ = _solver_residual_evidence(
        """
Time = 1
smoothSolver: Solving for Ux, Initial residual = 2e-7, Final residual = 1e-9, No Iterations 2
GAMG: Solving for p, Initial residual = 3e-7, Final residual = 1e-9, No Iterations 2
"""
    )

    assert _outer_residual_check(quantities, tolerance=1.0e-6).passed is True
    assert _outer_residual_check(quantities, tolerance=1.0e-8).passed is False
    assert _outer_residual_check(
        {"solver.initial_residual.p": quantities["solver.initial_residual.p"]},
        tolerance=1.0e-6,
    ).passed is False


def test_axis_aligned_pipe_uses_final_residuals_for_zero_transverse_components():
    quantities, _ = _solver_residual_evidence(
        """
Time = 500
smoothSolver: Solving for Ux, Initial residual = 7e-6, Final residual = 3e-7, No Iterations 2
smoothSolver: Solving for Uy, Initial residual = 8e-6, Final residual = 4e-7, No Iterations 2
smoothSolver: Solving for Uz, Initial residual = 8e-10, Final residual = 8e-10, No Iterations 0
GAMG: Solving for p, Initial residual = 9e-9, Final residual = 9e-9, No Iterations 0
"""
    )

    generic = _outer_residual_check(quantities, tolerance=1.0e-8)
    pipe = _outer_residual_check(
        quantities,
        tolerance=1.0e-6,
        axial_velocity_component="Uz",
    )

    assert generic.passed is False
    assert pipe.passed is True
    assert pipe.value == pytest.approx(4.0e-7)
    assert (
        _outer_residual_check(
            quantities,
            tolerance=1.0e-8,
            axial_velocity_component="Uz",
        ).passed
        is False
    )


def test_bounded_pipe_convergence_requires_independent_evidence_without_marker():
    passed = Check("evidence", True, kind="verification")
    mass = Check("mass-balance", True, kind="verification")
    converged, route = _bounded_pipe_convergence(
        process_ok=True,
        reached_end=True,
        residual_check=passed,
        pressure_stability_check=passed,
        recovery_checks=(mass,),
        explicit_marker=False,
    )

    assert converged is True
    assert route == "axial-residual-pressure-stability-and-conservation"
    unconverged, _ = _bounded_pipe_convergence(
        process_ok=True,
        reached_end=True,
        residual_check=passed,
        pressure_stability_check=passed,
        recovery_checks=(),
        explicit_marker=False,
    )
    assert unconverged is False


def test_pressure_drop_stability_requires_enough_stable_tail_samples():
    policy = OpenFOAMValidationPolicy(
        minimum_steady_samples=3,
        maximum_relative_pressure_drop_drift=1.0e-3,
    )
    stable = History((1.0, 2.0, 3.0), (10.0, 10.005, 10.004), unit="Pa")
    drifting = History((1.0, 2.0, 3.0), (10.0, 10.1, 10.2), unit="Pa")

    assert _pressure_drop_stability_check(stable, policy=policy).passed is True
    assert _pressure_drop_stability_check(drifting, policy=policy).passed is False
    assert _pressure_drop_stability_check(None, policy=policy).passed is False


def test_turbulent_pressure_drop_stability_uses_explicit_rans_limit():
    policy = OpenFOAMValidationPolicy(
        maximum_relative_pressure_drop_drift=1.0e-4,
        maximum_relative_turbulent_pressure_drop_drift=5.0e-4,
    )
    history = History(
        (296.0, 297.0, 298.0, 299.0, 300.0),
        (240.05, 240.10, 240.11, 240.09, 240.08),
        unit="Pa",
    )

    assert _pressure_drop_stability_check(history, policy=policy).passed is False
    assert (
        _pressure_drop_stability_check(history, policy=policy, turbulent=True).passed
        is True
    )


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


def test_openfoam_y_plus_recovery_reads_patch_statistics_and_restarts(tmp_path):
    first = tmp_path / "postProcessing" / "agentcfd_y_plus" / "0"
    second = tmp_path / "postProcessing" / "agentcfd_y_plus" / "100"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "yPlus.dat").write_text(
        "# Time patch min max average\n"
        "1 wall 35 90 70\n"
        "100 wall 40 95 75\n"
    )
    (second / "yPlus.dat").write_text(
        "# Time patch min max average\n"
        "100 wall 41 96 76\n"
        "101 wall 42 97 77\n"
    )

    assert _read_y_plus_series(tmp_path) == {
        "minimum": {1.0: 35.0, 100.0: 41.0, 101.0: 42.0},
        "maximum": {1.0: 90.0, 100.0: 96.0, 101.0: 97.0},
        "average": {1.0: 70.0, 100.0: 76.0, 101.0: 77.0},
    }


def test_turbulent_wall_gate_checks_full_patch_range_not_only_mean(tmp_path):
    directory = tmp_path / "postProcessing" / "agentcfd_y_plus" / "0"
    directory.mkdir(parents=True)
    (directory / "yPlus.dat").write_text(
        "# Time patch min max average\n1 wall 20 100 70\n"
    )

    _, _, checks = _recover_turbulence_data(
        tmp_path,
        density=998.2,
        dynamic_viscosity=1.002e-3,
        mean_velocity=1.0,
        diameter=0.1,
        length=3.0,
        pressure_drop=Quantity(250.0, "Pa"),
        policy=OpenFOAMValidationPolicy(),
    )

    wall_check = next(check for check in checks if check.name == "wall-y-plus-range")
    assert wall_check.passed is False
    assert wall_check.value == 70.0


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


def test_openfoam_mesh_metric_policy_fails_high_or_missing_observables():
    quantities = _mesh_quality_quantities(
        """    Max aspect ratio = 80
    Mesh non-orthogonality Max: 70 average: 20
"""
    )
    checks = _mesh_metric_checks(
        quantities,
        policy=OpenFOAMValidationPolicy(),
    )

    assert {check.name for check in checks if not check.passed} == {
        "maximum-non-orthogonality-limit",
        "maximum-skewness-limit",
        "maximum-aspect-ratio-limit",
    }


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


def test_openfoam_validation_policy_is_explicit_and_controls_acceptance(tmp_path):
    samples = {
        "agentcfd_inlet_flow": -0.01,
        "agentcfd_outlet_flow": 0.0099,
        "agentcfd_inlet_pressure": 0.255,
        "agentcfd_outlet_pressure": 0.0,
    }
    for name, value in samples.items():
        directory = tmp_path / "postProcessing" / name / "0"
        directory.mkdir(parents=True)
        (directory / "surfaceFieldValue.dat").write_text(f"# Time value\n1 {value}\n")

    _, _, strict_checks, _ = _recover_patch_data(
        tmp_path,
        density=1000.0,
        reference_pressure_drop=250.0,
        solver_tolerance=1.0e-8,
        pressure_error_limit=0.01,
        mass_balance_limit=0.001,
    )
    _, _, declared_checks, _ = _recover_patch_data(
        tmp_path,
        density=1000.0,
        reference_pressure_drop=250.0,
        solver_tolerance=1.0e-8,
        pressure_error_limit=0.03,
        mass_balance_limit=0.02,
    )
    _, _, uniform_checks, _ = _recover_patch_data(
        tmp_path,
        density=1000.0,
        reference_pressure_drop=250.0,
        solver_tolerance=1.0e-8,
        pressure_error_limit=0.01,
        mass_balance_limit=0.02,
        pressure_reference_applicable=False,
    )

    assert {check.name for check in strict_checks if not check.passed} == {
        "mass-balance",
        "pressure-drop-reference",
    }
    assert all(check.passed for check in declared_checks)
    assert next(
        check for check in uniform_checks if check.name == "pressure-drop-reference"
    ).passed
    assert not next(
        check
        for check in uniform_checks
        if check.name == "pressure-reference-applicability"
    ).passed
    with pytest.raises(ValueError, match="maximum_relative_pressure_error"):
        OpenFOAMValidationPolicy(maximum_relative_pressure_error=math.nan)
    with pytest.raises(ValueError, match="maximum_mesh_skewness"):
        OpenFOAMValidationPolicy(maximum_mesh_skewness=math.nan)
    with pytest.raises(ValueError, match="minimum_wall_y_plus must be below"):
        OpenFOAMValidationPolicy(minimum_wall_y_plus=300, maximum_wall_y_plus=30)
    with pytest.raises(ValueError, match="unique non-empty"):
        OpenFOAMValidationPolicy(validated_runtime_versions=("2606", "v2606"))
    with pytest.raises(ValueError, match="unique non-empty"):
        OpenFOAMValidationPolicy(validated_runtime_versions=(2606,))


def test_openfoam_patch_recovery_checks_requested_inlet_flow(tmp_path):
    samples = {
        "agentcfd_inlet_flow": -0.009,
        "agentcfd_outlet_flow": 0.009,
        "agentcfd_inlet_pressure": 0.25,
        "agentcfd_outlet_pressure": 0.0,
    }
    for name, value in samples.items():
        directory = tmp_path / "postProcessing" / name / "0"
        directory.mkdir(parents=True)
        (directory / "surfaceFieldValue.dat").write_text(f"# Time value\n1 {value}\n")

    quantities, _, checks, _ = _recover_patch_data(
        tmp_path,
        density=1000.0,
        reference_pressure_drop=250.0,
        solver_tolerance=1.0e-8,
        requested_volume_flow=0.01,
        inlet_flow_error_limit=0.01,
    )

    assert quantities["flow.inlet_flow_relative_error"].value == pytest.approx(0.1)
    assert not next(check for check in checks if check.name == "inlet-flow-target").passed
