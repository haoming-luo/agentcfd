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
    provider = OpenFOAMImportedProvider(source=source)

    with pytest.raises(UnsupportedCaseError, match="strictly inside"):
        provider.validate(outside)
    with pytest.raises(UnsupportedCaseError, match="scalar pressure"):
        provider.validate(vector_surface)


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


def test_checked_in_imported_duct_example_and_evidence_are_valid():
    repository = Path(__file__).resolve().parents[1]
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
    project_plan = example.plan()
    assert project_plan["readiness"]["ready_to_run"] is True
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


def test_imported_flow_provider_recovers_accepted_result(tmp_path, monkeypatch):
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
    assert set(result.fields) == {"U", "p"}
    assert result.provenance["provider_capability"] == (
        "openfoam.steady-laminar-imported-surface"
    )
    assert result.provenance["mesh_sha256"] == result.fields["U"].mesh_sha256
    assert result.provenance["mesh_sha256"] == result.fields["p"].mesh_sha256
