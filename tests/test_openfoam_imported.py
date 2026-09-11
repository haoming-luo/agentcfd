import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from agentcfd import (
    Model,
    boundaries,
    contracts,
    fluids,
    geometry,
    meshing,
    outputs,
    projects,
    studies,
)
from agentcfd.cli import entrypoint
from agentcfd.errors import UnsupportedCaseError
from agentcfd.providers import (
    execute_imported_mesh,
    OpenFOAMImportedProvider,
    plan_imported_mesh,
    prepare_imported_mesh,
)
from agentcfd.providers.openfoam_reports import recover_reports, report_recovered


def _step(payload: bytes, **domain_overrides):
    settings = {
        "asset": "geometry/fluid.stl",
        "source_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "source_format": "stl",
        "unit": "mm",
        "scale_to_m": 0.001,
        "boundary_roles": (
            ("inlet", "inlet"),
            ("outlet", "outlet"),
            ("walls", "wall"),
        ),
        "bounds_m": ((0.0, 0.0, 0.0), (0.1, 0.05, 0.025)),
        "enclosed_volume_m3": 0.000125,
        "interior_point_m": (0.05, 0.025, 0.0125),
    }
    settings.update(domain_overrides)
    domain = geometry.ImportedSurface(**settings)
    model = Model(
        name="imported-duct",
        study=studies.internal_flow(),
        domain=domain,
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.velocity_inlet((0.5, 0.0, 0.0)),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )
    return model.step(
        mesh=meshing.automatic(
            base_size=0.01,
            local_sizing=(meshing.refine("inlet", size=0.0025),),
            maximum_cells=100_000,
        )
    )


def _turbulent_step(payload: bytes):
    laminar = _step(payload)
    domain = laminar.model.domain
    model = Model(
        name="imported-rans-duct",
        study=studies.internal_flow(
            turbulence="k-omega-sst",
            wall_treatment="blended-wall-functions",
        ),
        domain=domain,
        fluid=laminar.model.fluid,
    ).boundaries(
        inlet=boundaries.turbulent_velocity_inlet(
            (5.0, 0.0, 0.0),
            intensity=0.05,
            length_scale=0.005,
        ),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )
    return model.step(
        mesh=laminar.mesh,
        output=outputs.turbulent_internal_flow(),
    )


def _mass_flow_step(payload: bytes):
    base = _step(payload)
    model = Model(
        name="imported-mass-flow-duct",
        study=studies.internal_flow(),
        domain=base.model.domain,
        fluid=base.model.fluid,
    ).boundaries(
        inlet=boundaries.mass_flow_inlet(49.91),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )
    return model.step(mesh=base.mesh, output=base.output)


def _pressure_driven_step(payload: bytes):
    base = _step(payload)
    model = Model(
        name="imported-pressure-driven-duct",
        study=studies.internal_flow(),
        domain=base.model.domain,
        fluid=base.model.fluid,
    ).boundaries(
        inlet=boundaries.pressure_inlet(10.0),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )
    return model.step(mesh=base.mesh, output=base.output)


def _imported_project(root, payload):
    project = projects.init_project(root, provider="openfoam")
    asset = root / "geometry" / "fluid.stl"
    asset.parent.mkdir()
    asset.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (root / "case.py").write_text(
        f"""from agentcfd import Model, boundaries, fluids, geometry, meshing, studies

def build(*, base_size=0.01):
    domain = geometry.ImportedSurface(
        asset="geometry/fluid.stl",
        source_sha256="sha256:{digest}",
        source_format="stl",
        unit="mm",
        scale_to_m=0.001,
        boundary_roles=(("inlet", "inlet"), ("outlet", "outlet"), ("walls", "wall")),
        bounds_m=((0.0, 0.0, 0.0), (0.1, 0.05, 0.025)),
        enclosed_volume_m3=0.000125,
        interior_point_m=(0.05, 0.025, 0.0125),
    )
    model = Model(
        study=studies.internal_flow(), domain=domain,
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.5),
        outlet=boundaries.pressure_outlet(), walls=boundaries.no_slip_wall(),
    )
    return model.step(mesh=meshing.automatic(base_size=base_size, maximum_cells=100000))
"""
    )
    return project


def test_imported_mesh_plan_is_bounded_and_schema_valid():
    plan = plan_imported_mesh(_step(b"surface")).to_dict()

    assert plan["background_cells"] == [14, 9, 7]
    assert plan["background_cell_count"] == 882
    assert plan["maximum_cells"] == 100_000
    assert plan["surface_levels"] == {"inlet": 2, "outlet": 1, "walls": 1}
    assert plan["interior_point_m"] == [0.05, 0.025, 0.0125]
    assert plan["add_layers"] is False
    assert plan["plan_sha256"].startswith("sha256:")
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-mesh-plan.schema.json")
    ).validate(plan)


def test_imported_mesh_prepare_writes_scaled_surface_and_hard_budget(tmp_path):
    payload = b"solid placeholder\nendsolid placeholder\n"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)

    prepared = prepare_imported_mesh(
        _step(payload),
        source=source,
        directory=tmp_path / "case",
    )

    assert (
        prepared.case_sha256
        == prepare_imported_mesh(
            _step(payload),
            source=source,
            directory=tmp_path / "case-copy",
        ).case_sha256
    )
    assert (
        prepared.directory / "constant" / "triSurface" / "imported-fluid.stl"
    ).read_bytes() == payload
    block = (prepared.directory / "system" / "blockMeshDict").read_text()
    snappy = (prepared.directory / "system" / "snappyHexMeshDict").read_text()
    assert "(14 9 7)" in block
    assert "scale 0.001" in snappy
    assert "maxGlobalCells 100000" in snappy
    assert "locationInMesh (0.050000000000000003" in snappy
    assert "inlet { name inlet; }" in snappy
    assert "patchInfo { type wall; }" in snappy
    assert "addLayers false" in snappy
    assert (prepared.directory / "agentcfd-imported-mesh.json").is_file()


def test_imported_flow_lowers_validated_compact_reports(tmp_path):
    payload = b"solid placeholder\nendsolid placeholder\n"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    base = _step(payload)
    reports = (
        outputs.probe("middle velocity", at=(0.05, 0.025, 0.0125), every=5),
        outputs.surface_report(
            "outlet pressure",
            region="outlet",
            field="fluid.pressure",
            operation="area-average",
            every=5,
        ),
        outputs.force_report("wall drag", regions=("walls",), every=5),
    )
    step = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(reports=reports),
    )

    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
    )
    prepared = provider.prepare(step)
    control = (prepared.directory / "system/controlDict").read_text()

    assert "middle_velocity" in control
    assert "type probes;" in control
    assert "outlet_pressure" in control
    assert "operation areaAverage;" in control
    assert "wall_drag" in control
    assert "type forces;" in control


def test_imported_flow_lowers_total_pressure_loss_report_without_field_frames(tmp_path):
    payload = b"solid placeholder\nendsolid placeholder\n"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    base = _step(payload)
    report = outputs.pressure_loss(
        "valve loss",
        inlet="inlet",
        outlet="outlet",
        every=5,
    )
    step = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(reports=(report,)),
    )

    prepared = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
    ).prepare(step)
    control = (prepared.directory / "system/controlDict").read_text()

    assert control.count("type pressure;") == 1
    assert "mode total;" in control
    assert "result agentcfd_total_pressure;" in control
    assert control.count("operation weightedAverage;") == 2
    assert control.count("weightField phi;") == 2
    assert control.count("writeArea true;") == 2
    assert "agentcfd_loss_valve_loss_inlet" in control
    assert "agentcfd_loss_valve_loss_outlet" in control
    assert "writeControl none;" in control


def test_pressure_loss_report_recovers_compact_engineering_quantities(tmp_path):
    payload = b"surface"
    base = _step(payload)
    report = outputs.pressure_loss(
        "device-loss",
        inlet="inlet",
        outlet="outlet",
    )
    step = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(reports=(report,)),
    )
    files = {
        "agentcfd_inlet_flow/0/surfaceFieldValue.dat": "10 -0.002\n20 -0.002\n",
        "agentcfd_loss_device_loss_inlet/0/surfaceFieldValue.dat": (
            "# Area : 0.001\n# Time Area weightedAverage(p)\n"
            "10 0.001 1120\n20 0.001 1110\n"
        ),
        "agentcfd_loss_device_loss_outlet/0/surfaceFieldValue.dat": (
            "# Area : 0.001\n# Time Area weightedAverage(p)\n"
            "10 0.001 1000\n20 0.001 1010\n"
        ),
    }
    for relative, content in files.items():
        path = tmp_path / "postProcessing" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    quantities, histories, artifacts = {}, {}, {}

    recover_reports(step, tmp_path, quantities, histories, artifacts)

    prefix = "report.device-loss"
    assert quantities[f"{prefix}.total_pressure_loss"].value == pytest.approx(100.0)
    assert quantities[f"{prefix}.reference_area"].value == pytest.approx(0.001)
    assert quantities[f"{prefix}.reference_bulk_velocity"].value == pytest.approx(2.0)
    assert quantities[f"{prefix}.reference_dynamic_pressure"].value == pytest.approx(
        1996.4
    )
    assert quantities[f"{prefix}.loss_coefficient"].value == pytest.approx(
        100.0 / 1996.4
    )
    assert "Total-pressure loss divided" in quantities[
        f"{prefix}.loss_coefficient"
    ].description
    assert histories[f"{prefix}.loss_coefficient"].unit == "1"
    assert report_recovered(report, histories) is True
    assert len(artifacts) == 2


def test_imported_flow_recovers_reports_with_units_and_total_force(tmp_path):
    payload = b"surface"
    base = _step(payload)
    reports = (
        outputs.probe("middle", at=(0.05, 0.025, 0.0125)),
        outputs.surface_report(
            "outlet-average",
            region="outlet",
            field="fluid.pressure",
        ),
        outputs.surface_report(
            "wall-load",
            region="walls",
            field="fluid.pressure",
            operation="area-integral",
        ),
        outputs.force_report("drag", regions=("walls",)),
    )
    step = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(reports=reports),
    )
    files = {
        "middle/0/U": "1 (0.2 -0.1 0.05)\n",
        "middle/0/p": "1 0.3\n",
        "outlet_average/0/surfaceFieldValue.dat": "1 0.4\n",
        "wall_load/0/surfaceFieldValue.dat": "1 0.5\n",
        # pressure + viscous + porous force vectors, then moment vectors.
        "drag/0/force.dat": "1 (10 0 0) (2 0 0) (0.5 0 0) (0 0 0)\n",
    }
    for relative, content in files.items():
        path = tmp_path / "postProcessing" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    quantities, histories, artifacts = {}, {}, {}

    recover_reports(step, tmp_path, quantities, histories, artifacts)

    assert quantities["probe.middle.fluid.pressure.value"].value == pytest.approx(
        299.46
    )
    assert quantities["report.outlet-average"].unit == "Pa"
    assert quantities["report.wall-load"].unit == "N"
    assert quantities["report.drag"].value == pytest.approx(12.5)
    assert histories["probe.middle.fluid.velocity.x"].abscissa_name == (
        "solver_iteration"
    )
    assert histories["probe.middle.fluid.velocity.x"].abscissa_unit == "1"
    assert all(report_recovered(report, histories) for report in reports)
    assert len(artifacts) == len(files)


def test_imported_flow_rejects_unsafe_or_unsupported_report_intent(tmp_path):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    base = _step(payload)
    outside = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(
            reports=(outputs.probe("outside", at=(1.0, 1.0, 1.0)),)
        ),
    )
    vector_surface = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(
            reports=(
                outputs.surface_report(
                    "velocity",
                    region="outlet",
                    field="fluid.velocity",
                ),
            )
        ),
    )
    reversed_loss = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(
            reports=(
                outputs.pressure_loss(
                    "reversed-loss", inlet="outlet", outlet="inlet"
                ),
            )
        ),
    )
    internal_name_collision = base.model.step(
        mesh=base.mesh,
        output=outputs.standard(
            reports=(
                outputs.pressure_loss(
                    "device", inlet="inlet", outlet="outlet"
                ),
                outputs.surface_report(
                    "agentcfd-loss-device-inlet",
                    region="inlet",
                    field="fluid.pressure",
                ),
            )
        ),
    )
    provider = OpenFOAMImportedProvider(source=source)

    with pytest.raises(UnsupportedCaseError, match="strictly inside"):
        provider.validate(outside)
    with pytest.raises(UnsupportedCaseError, match="scalar pressure"):
        provider.validate(vector_surface)
    with pytest.raises(UnsupportedCaseError, match="inlet-role"):
        provider.validate(reversed_loss)
    with pytest.raises(UnsupportedCaseError, match="names collide"):
        provider.validate(internal_name_collision)


def test_imported_mesh_fails_closed_on_unsupported_or_unsafe_intent(tmp_path):
    payload = b"surface"
    with pytest.raises(UnsupportedCaseError, match="interior_point_m"):
        plan_imported_mesh(_step(payload, interior_point_m=None))

    base = _step(payload)
    layered = base.model.step(
        mesh=meshing.automatic(
            base_size=0.01,
            boundary_layers=(meshing.layers("walls", count=3, first_height=0.0005),),
        )
    )
    with pytest.raises(UnsupportedCaseError, match="Boundary-layer"):
        plan_imported_mesh(layered)

    too_many = base.model.step(
        mesh=meshing.automatic(base_size=0.001, maximum_cells=1_000)
    )
    with pytest.raises(UnsupportedCaseError, match="Background mesh requires"):
        plan_imported_mesh(too_many)

    source = tmp_path / "source.stl"
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source_sha256"):
        prepare_imported_mesh(base, source=source, directory=tmp_path / "case")

    source.write_bytes(payload)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "user.txt").write_text("preserve")
    with pytest.raises(FileExistsError, match="not empty"):
        prepare_imported_mesh(base, source=source, directory=occupied)
    assert (occupied / "user.txt").read_text() == "preserve"


def test_imported_mesh_execution_gates_geometry_budget_and_quality(
    tmp_path, monkeypatch
):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    prepared = prepare_imported_mesh(
        _step(payload), source=source, directory=tmp_path / "case"
    )
    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.shutil.which", lambda name: f"/{name}"
    )

    def completed(argv, **kwargs):
        executable = argv[0].rsplit("/", 1)[-1]
        if executable == "checkMesh":
            kwargs["stdout"].write(
                "    cells:            4200\n"
                "Max aspect ratio = 12.5\n"
                "Mesh non-orthogonality Max: 42 average: 8\n"
                "Max skewness = 1.2\n"
                "Mesh OK.\n"
            )
        else:
            kwargs["stdout"].write("End\n")
        kwargs["stdout"].write("| Version: 2606  |\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.subprocess.run", completed
    )
    result = execute_imported_mesh(prepared)

    assert result.accepted is True
    assert result.return_codes == {
        "blockMesh": 0,
        "snappyHexMesh-check": 0,
        "snappyHexMesh": 0,
        "checkMesh": 0,
    }
    assert result.quantities["mesh.cell_count"]["value"] == 4200.0
    record = result.to_dict()
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-mesh-result.schema.json")
    ).validate(record)
    assert (prepared.directory / "agentcfd-imported-mesh-result.json").is_file()


def test_imported_mesh_cli_plans_and_prepares_without_openfoam(tmp_path, capsys):
    root = tmp_path / "project"
    _imported_project(root, b"surface")

    assert entrypoint(["mesh", str(root), "--plan-only", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["background_cell_count"] == 882

    target = tmp_path / "prepared"
    assert (
        entrypoint(
            [
                "mesh",
                str(root),
                "--output",
                str(target),
                "--prepare-only",
                "--param",
                "base_size=0.02",
                "--json",
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["mesh_plan"]["background_cells"] == [9, 7, 6]
    assert (target / "system" / "snappyHexMeshDict").is_file()

    (root / "geometry" / "fluid.stl").write_bytes(b"changed")
    assert entrypoint(["mesh", str(root), "--plan-only"]) == 2
    assert "bytes changed" in capsys.readouterr().err


def test_checked_in_imported_duct_example_and_evidence_are_valid(monkeypatch):
    monkeypatch.setattr(projects.shutil, "which", lambda _command: "/mock/runtime")
    repository = Path(__file__).resolve().parents[1]
    assert "*.stl -text" in (repository / ".gitattributes").read_text()
    example = projects.Project(repository / "examples" / "imported_duct_mesh")
    step = example.load_step()
    step.model.validate()
    assert plan_imported_mesh(step).background_cell_count == 2688
    record = json.loads(
        (repository / "docs" / "openfoam-v2606-imported-duct-mesh.json").read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-mesh-result.schema.json")
    ).validate(record)
    assert record["accepted"] is True
    flow_record = json.loads(
        (repository / "docs" / "openfoam-v2606-imported-duct-flow.json").read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-flow-evidence.schema.json")
    ).validate(flow_record)
    pressure_loss_record = json.loads(
        (repository / "docs" / "openfoam-v2606-pressure-loss-report.json").read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-pressure-loss-evidence.schema.json")
    ).validate(pressure_loss_record)
    assert pressure_loss_record["pressure_loss_report"]["loss_coefficient"] > 0.0
    assert pressure_loss_record["portable_fields"]["frames"] == 1
    rans_record = json.loads(
        (repository / "docs" / "openfoam-v2606-imported-duct-rans.json").read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-flow-evidence.schema.json")
    ).validate(rans_record)
    assert rans_record["wall_y_plus"]["maximum"] <= 300.0
    assert rans_record["wall_y_plus"]["minimum"] >= 30.0
    mass_flow_record = json.loads(
        (
            repository
            / "docs"
            / "openfoam-v2606-imported-duct-mass-flow.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-flow-evidence.schema.json")
    ).validate(mass_flow_record)
    assert mass_flow_record["inlet_mass_flow"]["relative_error"] <= 1.0e-4
    pressure_record = json.loads(
        (
            repository
            / "docs"
            / "openfoam-v2606-imported-duct-pressure-driven.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-flow-evidence.schema.json")
    ).validate(pressure_record)
    assert pressure_record["pressure_control"]["recovered_mass_flow_kg_s"] > 0.0
    assert pressure_record["flow"]["relative_mass_imbalance"] <= 1.0e-4
    project_plan = example.plan()
    assert project_plan["readiness"]["provider_compatible"] is True
    assert project_plan["readiness"]["input_assets_ready"] is True
    assert project_plan["readiness"]["mesh_intent_ready"] is True
    assert project_plan["decisions"]["output_plan"]["estimated_mesh_cells"] == 200_000


def test_imported_flow_provider_requires_vector_direction_and_prepares_case(tmp_path):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    step = _step(payload)
    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
        container_image="opencfd/openfoam-run:2606",
    )

    provider.validate(step)
    prepared = provider.prepare(step)
    velocity = (prepared.directory / "0" / "U").read_text()
    assert "value uniform (0.5 0 0);" in velocity
    assert (
        "maxGlobalCells 100000"
        in (prepared.directory / "system" / "snappyHexMeshDict").read_text()
    )
    assert (prepared.directory / "agentcfd-imported-flow-case.json").is_file()

    scalar_step = _step(payload)
    scalar_step.model.boundaries(inlet=boundaries.mean_velocity_inlet(0.5))
    with pytest.raises(UnsupportedCaseError, match="direction is never guessed"):
        OpenFOAMImportedProvider(source=source).validate(scalar_step)


def test_imported_flow_provider_lowers_total_to_static_pressure_drive(tmp_path):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "pressure-case",
    )

    prepared = provider.prepare(_pressure_driven_step(payload))
    velocity = (prepared.directory / "0/U").read_text()
    pressure = (prepared.directory / "0/p").read_text()

    assert velocity.count("type pressureInletOutletVelocity;") == 2
    assert "type totalPressure;" in pressure
    assert "rho none;" in pressure
    assert "p0 uniform 0.010018032" in pressure
    assert "type fixedValue;" in pressure

    with_temperature = _pressure_driven_step(payload)
    with_temperature.model.boundaries(
        inlet=boundaries.pressure_inlet(10.0, temperature=300.0)
    )
    with pytest.raises(UnsupportedCaseError, match="does not accept an inlet temperature"):
        OpenFOAMImportedProvider(source=source).validate(with_temperature)

    reversed_pressure = _pressure_driven_step(payload)
    reversed_pressure.model.boundaries(inlet=boundaries.pressure_inlet(-1.0))
    with pytest.raises(UnsupportedCaseError, match="above outlet static"):
        OpenFOAMImportedProvider(source=source).validate(reversed_pressure)


def test_imported_rans_provider_lowers_explicit_vector_turbulence_intent(tmp_path):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
    )

    prepared = provider.prepare(_turbulent_step(payload))

    assert "value uniform (5 0 0);" in (prepared.directory / "0/U").read_text()
    assert "RASModel        kOmegaSST;" in (
        prepared.directory / "constant/turbulenceProperties"
    ).read_text()
    assert "type kqRWallFunction;" in (prepared.directory / "0/k").read_text()
    assert "type omegaWallFunction;" in (prepared.directory / "0/omega").read_text()
    assert "type nutUBlendedWallFunction;" in (
        prepared.directory / "0/nut"
    ).read_text()
    assert "type yPlus;" in (prepared.directory / "system/controlDict").read_text()
    manifest = json.loads(
        (prepared.directory / "agentcfd-imported-flow-case.json").read_text()
    )
    assert manifest["capability"] == "openfoam.steady-rans-imported-surface"


def test_imported_rans_requires_matching_study_inlet_and_wall_treatment(tmp_path):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    provider = OpenFOAMImportedProvider(source=source)
    turbulent = _turbulent_step(payload)

    wrong_inlet_model = Model(
        name="wrong-inlet",
        study=turbulent.model.study,
        domain=turbulent.model.domain,
        fluid=turbulent.model.fluid,
    ).boundaries(
        inlet=boundaries.velocity_inlet((5.0, 0.0, 0.0)),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )
    wrong_inlet = wrong_inlet_model.step(
        mesh=turbulent.mesh,
        output=turbulent.output,
    )
    with pytest.raises(UnsupportedCaseError, match="turbulent_velocity_inlet"):
        provider.validate(wrong_inlet)

    wrong_treatment_model = Model(
        name="wrong-treatment",
        study=studies.internal_flow(
            turbulence="k-omega-sst",
            wall_treatment="wall-resolved",
        ),
        domain=turbulent.model.domain,
        fluid=turbulent.model.fluid,
    ).boundaries(**turbulent.model.boundary_conditions)
    wrong_treatment = wrong_treatment_model.step(
        mesh=turbulent.mesh,
        output=turbulent.output,
    )
    with pytest.raises(UnsupportedCaseError, match="blended-wall-functions"):
        provider.validate(wrong_treatment)


def test_imported_flow_provider_recovers_accepted_result(tmp_path, monkeypatch):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
        mesh_cache_directory=tmp_path / "mesh-cache",
    )
    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.shutil.which", lambda name: f"/{name}"
    )

    commands = []

    def completed(argv, **kwargs):
        executable = argv[0].rsplit("/", 1)[-1]
        commands.append(executable)
        case = Path(kwargs["stdout"].name).parent
        if executable == "blockMesh":
            poly_mesh = case / "constant" / "polyMesh"
            poly_mesh.mkdir(parents=True)
            for name in ("points", "faces", "owner", "neighbour", "boundary"):
                (poly_mesh / name).write_text(name)
        if executable == "checkMesh":
            kwargs["stdout"].write(
                "    cells:            4200\n"
                "Max aspect ratio = 12.5\n"
                "Mesh non-orthogonality Max: 42 average: 8\n"
                "Max skewness = 1.2\n"
                "Mesh OK.\n"
            )
        elif executable == "simpleFoam":
            final = case / "10"
            final.mkdir()
            for name in ("U", "p"):
                (final / name).write_bytes((case / "0" / name).read_bytes())
            values = {
                "agentcfd_inlet_flow": -0.05,
                "agentcfd_outlet_flow": 0.05,
                "agentcfd_inlet_pressure": 0.1,
                "agentcfd_outlet_pressure": 0.0,
            }
            if case.name == "case-direction-fail":
                values["agentcfd_inlet_flow"] = 0.05
                values["agentcfd_outlet_flow"] = -0.05
            for name, value in values.items():
                folder = case / "postProcessing" / name / "0"
                folder.mkdir(parents=True)
                (folder / "surfaceFieldValue.dat").write_text(f"10 {value}\n")
            kwargs["stdout"].write(
                "Time = 10\nSIMPLE solution converged in 10 iterations\nEnd\n"
            )
        else:
            kwargs["stdout"].write("End\n")
        kwargs["stdout"].write("| Version: 2606  |\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.subprocess.run", completed
    )
    result = provider.run(_step(payload))

    assert result.accepted is True
    assert result.quantity("flow.relative_mass_imbalance").value == 0.0
    assert result.quantity("flow.pressure_drop").value == pytest.approx(99.82)
    assert result.quantity("flow.inlet_volume_flow_rate").value == pytest.approx(0.05)
    assert result.quantity("flow.outlet_mass_flow_rate").value == pytest.approx(49.91)
    assert set(result.fields) == {"U", "p"}
    assert result.provenance["provider_capability"] == (
        "openfoam.steady-laminar-imported-surface"
    )
    assert result.provenance["mesh_sha256"] == result.fields["U"].mesh_sha256
    assert result.provenance["mesh_sha256"] == result.fields["p"].mesh_sha256
    assert result.provenance["mesh_acquisition"] == "generated"

    second = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case-2",
        mesh_cache_directory=tmp_path / "mesh-cache",
    ).run(_step(payload))

    assert second.accepted is True
    assert second.provenance["mesh_acquisition"] == "cache-hit"
    assert second.provenance["mesh_sha256"] == result.provenance["mesh_sha256"]
    assert commands.count("blockMesh") == 1
    assert commands.count("snappyHexMesh") == 2
    assert commands.count("checkMesh") == 1
    assert commands.count("simpleFoam") == 2

    cached_points = next((tmp_path / "mesh-cache").glob("*/polyMesh/points"))
    cached_points.write_text("corrupted")
    repaired = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case-3",
        mesh_cache_directory=tmp_path / "mesh-cache",
    ).run(_step(payload))

    assert repaired.accepted is True
    assert repaired.provenance["mesh_acquisition"] == "generated"
    assert commands.count("blockMesh") == 2
    assert commands.count("snappyHexMesh") == 4
    assert commands.count("checkMesh") == 2
    assert commands.count("simpleFoam") == 3

    mass_flow_provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case-4",
        mesh_cache_directory=tmp_path / "mesh-cache",
    )
    mass_flow = mass_flow_provider.run(_mass_flow_step(payload))
    velocity_field = (tmp_path / "case-4/0/U").read_text()

    assert mass_flow.accepted is True
    assert mass_flow.provenance["mesh_acquisition"] == "cache-hit"
    assert "type flowRateInletVelocity;" in velocity_field
    assert "volumetricFlowRate constant 0.049999999999999996;" in velocity_field
    assert mass_flow.quantity("flow.inlet_mass_flow_rate").value == pytest.approx(
        49.91
    )
    assert mass_flow.quantity(
        "flow.inlet_mass_flow_relative_error"
    ).value == pytest.approx(0.0)
    assert next(
        check for check in mass_flow.checks if check.name == "mass-flow-inlet-target"
    ).passed is True
    assert commands.count("simpleFoam") == 4

    pressure_driven = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case-5",
        mesh_cache_directory=tmp_path / "mesh-cache",
    ).run(_pressure_driven_step(payload))

    assert pressure_driven.accepted is True
    assert pressure_driven.quantity("flow.inlet_mass_flow_rate").value == pytest.approx(
        49.91
    )
    assert pressure_driven.quantity(
        "reference.flow.total_to_static_pressure_difference"
    ).value == pytest.approx(10.0)
    assert commands.count("simpleFoam") == 5

    wrong_direction = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case-direction-fail",
        mesh_cache_directory=tmp_path / "mesh-cache",
    ).run(_step(payload))

    assert wrong_direction.accepted is False
    assert next(
        check
        for check in wrong_direction.checks
        if check.name == "inlet-outlet-flow-direction"
    ).passed is False


def test_imported_flow_returns_failed_result_when_meshing_stops_early(
    tmp_path, monkeypatch
):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
    )
    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.shutil.which", lambda name: f"/{name}"
    )

    def failed(argv, **kwargs):
        kwargs["stdout"].write("FOAM FATAL ERROR\nsynthetic mesh failure\n")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.subprocess.run", failed
    )

    result = provider.run(_step(payload))

    assert result.status == "failed"
    assert result.accepted is False
    assert result.provenance["mesh_acquisition"] == "generated"
    assert next(check for check in result.checks if check.name == "mesh-quality").passed is False


@pytest.mark.parametrize(
    ("minimum_y_plus", "maximum_y_plus", "accepted"),
    ((38.0, 220.0, True), (18.0, 220.0, False), (38.0, 320.0, False)),
)
def test_imported_rans_provider_recovers_fields_and_gates_y_plus(
    tmp_path,
    monkeypatch,
    minimum_y_plus,
    maximum_y_plus,
    accepted,
):
    payload = b"surface"
    source = tmp_path / "source.stl"
    source.write_bytes(payload)
    provider = OpenFOAMImportedProvider(
        source=source,
        case_directory=tmp_path / "case",
    )
    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.shutil.which", lambda name: f"/{name}"
    )

    def completed(argv, **kwargs):
        executable = argv[0].rsplit("/", 1)[-1]
        case = Path(kwargs["stdout"].name).parent
        if executable == "blockMesh":
            poly_mesh = case / "constant/polyMesh"
            poly_mesh.mkdir(parents=True)
            for name in ("points", "faces", "owner", "neighbour", "boundary"):
                (poly_mesh / name).write_text(name)
        if executable == "checkMesh":
            kwargs["stdout"].write(
                "    cells:            4200\n"
                "Max aspect ratio = 12.5\n"
                "Mesh non-orthogonality Max: 42 average: 8\n"
                "Max skewness = 1.2\nMesh OK.\n"
            )
        elif executable == "simpleFoam":
            final = case / "10"
            final.mkdir()
            for name in ("U", "p", "k", "omega", "nut"):
                (final / name).write_bytes((case / "0" / name).read_bytes())
            values = {
                "agentcfd_inlet_flow": -0.05,
                "agentcfd_outlet_flow": 0.05,
                "agentcfd_inlet_pressure": 0.1,
                "agentcfd_outlet_pressure": 0.0,
            }
            for name, value in values.items():
                folder = case / "postProcessing" / name / "0"
                folder.mkdir(parents=True)
                (folder / "surfaceFieldValue.dat").write_text(f"10 {value}\n")
            y_plus = case / "postProcessing/agentcfd_y_plus/0/yPlus.dat"
            y_plus.parent.mkdir(parents=True)
            y_plus.write_text(
                f"10 walls {minimum_y_plus:g} {maximum_y_plus:g} 74\n"
            )
            kwargs["stdout"].write(
                "Time = 10\nSIMPLE solution converged in 10 iterations\nEnd\n"
            )
        else:
            kwargs["stdout"].write("End\n")
        kwargs["stdout"].write("| Version: 2606  |\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "agentcfd.providers.openfoam_imported.subprocess.run", completed
    )

    result = provider.run(_turbulent_step(payload))

    assert result.accepted is accepted
    assert set(result.fields) == {"U", "p", "k", "omega", "nut"}
    assert result.quantity("wall.y_plus.minimum").value == minimum_y_plus
    assert result.quantity("wall.y_plus.maximum").value == maximum_y_plus
    assert result.histories["wall.y_plus.average"].abscissa_name == (
        "solver_iteration"
    )
    assert result.provenance["provider_capability"] == (
        "openfoam.steady-rans-imported-surface"
    )
    assert next(
        check for check in result.checks if check.name == "wall-y-plus-range"
    ).passed is accepted
