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
    projects,
    studies,
)
from agentcfd.cli import entrypoint
from agentcfd.errors import UnsupportedCaseError
from agentcfd.providers import (
    execute_imported_mesh,
    plan_imported_mesh,
    prepare_imported_mesh,
)


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
        inlet=boundaries.mean_velocity_inlet(0.5),
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
