import json
import math
import struct

import jsonschema
import pytest

from agentcfd import (
    Model,
    boundaries,
    contracts,
    fluids,
    geometry,
    geometry_io,
    studies,
)
from agentcfd.cli import entrypoint


def _ascii_stl(name, triangles):
    lines = [f"solid {name}"]
    for triangle in triangles:
        lines.extend(("  facet normal 0 0 0", "    outer loop"))
        lines.extend(
            f"      vertex {vertex[0]} {vertex[1]} {vertex[2]}" for vertex in triangle
        )
        lines.extend(("    endloop", "  endfacet"))
    lines.append(f"endsolid {name}")
    return "\n".join(lines) + "\n"


def _named_tetra_stl():
    a = (0.0, 0.0, 0.0)
    b = (1.0, 0.0, 0.0)
    c = (0.0, 1.0, 0.0)
    d = (0.0, 0.0, 1.0)
    return "".join(
        (
            _ascii_stl("inlet", ((a, c, b),)),
            _ascii_stl("outlet", ((a, b, d),)),
            _ascii_stl("walls", ((a, d, c), (b, c, d))),
        )
    )


def test_geometry_region_normalization_is_previewed_atomic_and_verified(
    tmp_path, capsys
):
    source = tmp_path / "equipment.stl"
    source_text = "".join(
        (
            _ascii_stl(
                "inlet-main",
                (((0, 0, 0), (0, 1, 0), (0, 0, 1)),),
            ),
            _ascii_stl(
                "inlet main",
                (((1, 0, 0), (1, 0, 1), (1, 1, 0)),),
            ),
            _ascii_stl(
                "2 outlet face",
                (((0, 0, 0), (1, 0, 0), (0, 0, 1)),),
            ),
        )
    )
    source.write_text(source_text, encoding="utf-8")

    blocked = geometry_io.inspect_geometry(
        source,
        unit="m",
        require_watertight=False,
    )
    assert blocked["readiness"]["geometry_ready"] is False
    assert any(
        issue["code"] == "BOUNDARY_REGION_NAMES_UNSAFE"
        for issue in blocked["issues"]
    )

    plan = geometry_io.plan_region_normalization(source)
    jsonschema.Draft202012Validator(
        contracts.load("geometry-region-normalization.schema.json")
    ).validate(plan)
    mapping = {
        record["source"]: record["normalized"] for record in plan["regions"]
    }
    assert plan["output"] is None
    assert plan["changed_count"] == 3
    assert mapping["2 outlet face"].startswith("region_2_outlet_face")
    assert mapping["inlet-main"] != mapping["inlet main"]
    assert all(
        record["collision_resolved"]
        for record in plan["regions"]
        if record["source"] in {"inlet-main", "inlet main"}
    )

    output, report = geometry_io.normalize_geometry_regions(
        source, tmp_path / "equipment-normalized.stl"
    )
    jsonschema.Draft202012Validator(
        contracts.load("geometry-region-normalization.schema.json")
    ).validate(report)
    assert source.read_text(encoding="utf-8") == source_text
    assert report["source_modified"] is False
    assert report["geometry_coordinates_modified"] is False
    assert set(report["output"]["region_names"]) == set(mapping.values())
    source_vertices = [
        line.strip() for line in source_text.splitlines() if "vertex" in line
    ]
    output_vertices = [
        line.strip() for line in output.read_text().splitlines() if "vertex" in line
    ]
    assert output_vertices == source_vertices
    inspected = geometry_io.inspect_geometry(
        output,
        unit="m",
        require_watertight=False,
    )
    assert inspected["readiness"]["geometry_ready"] is True
    assert set(inspected["surface"]["region_names"]) == set(mapping.values())

    assert entrypoint(["geometry-normalize", str(source), "--json"]) == 0
    cli_plan = json.loads(capsys.readouterr().out)
    assert cli_plan["regions"] == plan["regions"]
    cli_output = tmp_path / "cli-normalized.stl"
    assert (
        entrypoint(
            ["geometry-normalize", str(source), str(cli_output), "--json"]
        )
        == 0
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["output"]["path"] == str(cli_output)
    assert "geometry-region-normalization.schema.json" in contracts.available()

    with pytest.raises(FileExistsError, match="already exists"):
        geometry_io.normalize_geometry_regions(source, output)
    with pytest.raises(geometry_io.GeometryInspectionError, match="new output path"):
        geometry_io.normalize_geometry_regions(source, source)


def test_obj_region_normalization_preserves_geometry_records(tmp_path):
    source = tmp_path / "duct.obj"
    source.write_text(
        "o supply-air\n"
        "v 0 0 0\n"
        "v 0 1 0\n"
        "v 0 0 1\n"
        "f 1 2 3\n"
        "g return air\n"
        "f 1 3 2\n",
        encoding="utf-8",
    )
    output, report = geometry_io.normalize_geometry_regions(
        source, tmp_path / "duct-normalized.obj"
    )

    assert {record["normalized"] for record in report["regions"]} == {
        "return_air",
        "supply_air",
    }
    lines = output.read_text(encoding="utf-8").splitlines()
    assert "o supply_air" in lines
    assert "g return_air" in lines
    assert lines.count("f 1 2 3") == 1
    assert lines.count("f 1 3 2") == 1


def test_closed_ascii_stl_reports_si_bounds_topology_and_volume(tmp_path, capsys):
    a = (0.0, 0.0, 0.0)
    b = (1.0, 0.0, 0.0)
    c = (0.0, 1.0, 0.0)
    d = (0.0, 0.0, 1.0)
    surface = tmp_path / "fluid.stl"
    surface.write_text(
        _ascii_stl("fluid", ((a, c, b), (a, b, d), (a, d, c), (b, c, d)))
    )

    report = geometry_io.inspect_geometry(surface, unit="mm")

    jsonschema.Draft202012Validator(
        contracts.load("geometry-inspection.schema.json")
    ).validate(report)
    assert report["readiness"] == {
        "geometry_ready": True,
        "boundary_roles_ready": False,
        "ready_for_import_setup": False,
        "agentcfd_imported_mesh_lowering_available": True,
        "ready_to_mesh": False,
    }
    assert report["source"]["encoding"] == "ascii"
    assert report["surface"]["triangle_count"] == 4
    assert report["surface"]["unique_vertex_count"] == 4
    assert report["surface"]["boundary_edge_count"] == 0
    assert report["surface"]["orientation_conflict_count"] == 0
    assert report["surface"]["watertight"] is True
    assert report["surface"]["region_names"] == ["fluid"]
    assert report["surface"]["connected_component_count"] == 1
    assert report["surface"]["connected_components"][0]["triangle_count"] == 4
    assert report["surface"]["connected_components"][0]["area_fraction"] == 1.0
    assert report["surface"]["dimensions_m"] == [0.001, 0.001, 0.001]
    assert math.isclose(report["surface"]["enclosed_volume_m3"], 1.0e-9 / 6.0)
    fluid_metric = report["surface"]["region_metrics"]["fluid"]
    assert fluid_metric["triangle_count"] == 4
    assert fluid_metric["area_m2"] == pytest.approx(
        (1.5 + math.sqrt(3.0) / 2.0) * 1.0e-6
    )
    assert fluid_metric["normal_coherence"] == pytest.approx(0.0, abs=1.0e-15)
    assert report["observation_cost"]["external_processes_started"] == 0
    assert entrypoint(["geometry-check", str(surface), "--unit", "mm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["surface"]["watertight"] is True

    confirmed = geometry_io.inspect_geometry(
        surface,
        unit="mm",
        boundary_roles={"fluid": "wall"},
    )
    assert confirmed["readiness"]["boundary_roles_ready"] is True
    assert confirmed["readiness"]["ready_for_import_setup"] is True
    assert confirmed["next_action"]["kind"] == "model-setup"
    roles = tmp_path / "roles.json"
    roles.write_text(
        json.dumps(
            {
                "schema": "agentcfd.boundary-role-map/0.1",
                "regions": {"fluid": "wall"},
            }
        )
    )
    assert (
        entrypoint(
            [
                "geometry-check",
                str(surface),
                "--unit",
                "mm",
                "--roles",
                str(roles),
                "--json",
            ]
        )
        == 0
    )
    cli = json.loads(capsys.readouterr().out)
    assert cli["boundary_roles"]["confirmed"] == {"fluid": "wall"}


def test_disconnected_surface_components_are_quantified_for_review(tmp_path):
    a = (0.0, 0.0, 0.0)
    b = (1.0, 0.0, 0.0)
    c = (0.0, 1.0, 0.0)
    d = (0.0, 0.0, 1.0)
    offset = (3.0, 0.0, 0.0)

    def shifted(point):
        return tuple(point[index] + offset[index] for index in range(3))

    tetrahedron = ((a, c, b), (a, b, d), (a, d, c), (b, c, d))
    surface = tmp_path / "two-shells.stl"
    surface.write_text(
        _ascii_stl("walls", tetrahedron)
        + _ascii_stl(
            "internal_baffle",
            tuple(tuple(shifted(vertex) for vertex in face) for face in tetrahedron),
        )
    )

    report = geometry_io.inspect_geometry(surface, unit="mm")

    jsonschema.Draft202012Validator(
        contracts.load("geometry-inspection.schema.json")
    ).validate(report)
    assert report["surface"]["watertight"] is True
    assert report["surface"]["connected_component_count"] == 2
    components = report["surface"]["connected_components"]
    assert [component["triangle_count"] for component in components] == [4, 4]
    assert {tuple(component["region_names"]) for component in components} == {
        ("walls",),
        ("internal_baffle",),
    }
    assert sum(component["area_fraction"] for component in components) == pytest.approx(1.0)
    assert all(component["area_m2"] > 0.0 for component in components)
    assert any(
        issue["code"] == "DISCONNECTED_SURFACE_COMPONENTS"
        and issue["severity"] == "warning"
        for issue in report["issues"]
    )
    assert report["readiness"]["geometry_ready"] is True


def test_confirmed_closed_surface_becomes_portable_model_intent(tmp_path, capsys):
    surface = tmp_path / "fluid.stl"
    surface.write_text(_named_tetra_stl())
    roles = {
        "inlet": "inlet",
        "outlet": "outlet",
        "walls": "wall",
    }
    report = geometry_io.inspect_geometry(
        surface,
        unit="mm",
        internal_flow=True,
        boundary_roles=roles,
    )
    jsonschema.Draft202012Validator(
        contracts.load("geometry-inspection.schema.json")
    ).validate(report)
    inlet_metric = report["surface"]["region_metrics"]["inlet"]
    assert inlet_metric["area_m2"] == pytest.approx(0.5e-6)
    assert inlet_metric["mean_unit_normal"] == pytest.approx([0.0, 0.0, -1.0])
    assert inlet_metric["normal_coherence"] == pytest.approx(1.0)
    inward = geometry_io.assess_inlet_velocity_direction(report, (0.0, 0.0, 2.0))
    jsonschema.Draft202012Validator(
        contracts.load("inlet-direction-assessment.schema.json")
    ).validate(inward)
    assert inward["status"] == "passed"
    assert inward["regions"][0]["inward_normal_velocity_m_s"] == pytest.approx(2.0)
    outward = geometry_io.assess_inlet_velocity_direction(report, (0.0, 0.0, -2.0))
    assert outward["status"] == "failed"
    assert outward["regions"][0]["alignment_cosine"] == pytest.approx(-1.0)
    with pytest.raises(geometry_io.GeometryInspectionError, match="Reverse or correct"):
        geometry_io.validate_inlet_velocity_direction(report, (0.0, 0.0, -2.0))
    domain = geometry.imported_surface_from_inspection(
        report,
        asset="geometry/fluid.stl",
        interior_point_m=(0.0001, 0.0001, 0.0001),
    )
    model = Model(
        name="imported-duct",
        study=studies.internal_flow(),
        domain=domain,
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(1.0),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )

    model.validate()
    assert domain.asset == "geometry/fluid.stl"
    assert domain.to_dict()["boundary_roles"] == roles
    assert domain.to_dict()["interior_point_m"] == [0.0001, 0.0001, 0.0001]
    assert str(surface) not in str(domain.to_dict())
    assert len(model.fingerprint()) == 64

    role_path = tmp_path / "roles.json"
    role_path.write_text(
        json.dumps({"schema": "agentcfd.boundary-role-map/0.1", "regions": roles})
    )
    output = tmp_path / "inspection.json"
    assert (
        entrypoint(
            [
                "geometry-check",
                str(surface),
                "--unit",
                "mm",
                "--roles",
                str(role_path),
                "--internal-flow",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    human = capsys.readouterr().out
    assert "watertight true" in human
    assert "inlet inlet: area 5e-07 m^2 | mean normal (0, 0, -1)" in human
    assert json.loads(output.read_text())["source"]["sha256"] == domain.source_sha256


def test_imported_volume_intent_rejects_open_surface_report(tmp_path):
    surface = tmp_path / "open.stl"
    surface.write_text(
        _ascii_stl(
            "walls",
            (((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),),
        )
    )
    report = geometry_io.inspect_geometry(
        surface,
        unit="m",
        require_watertight=False,
        boundary_roles={"walls": "wall"},
    )

    with pytest.raises(ValueError, match="watertight surface"):
        geometry.imported_surface_from_inspection(report, asset="geometry/open.stl")


def test_imported_surface_interior_point_must_be_inside_bounds():
    with pytest.raises(ValueError, match="strictly inside"):
        geometry.ImportedSurface(
            asset="geometry/fluid.stl",
            source_sha256="sha256:" + "0" * 64,
            source_format="stl",
            unit="m",
            scale_to_m=1.0,
            boundary_roles=(("walls", "wall"),),
            bounds_m=((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
            enclosed_volume_m3=1.0,
            interior_point_m=(0.0, 0.5, 0.5),
        )


def test_internal_flow_role_map_requires_exact_complete_inlet_and_outlet(tmp_path):
    obj = tmp_path / "duct.obj"
    obj.write_text(
        "o inlet_main\n"
        "v 0 0 0\n"
        "v 0 1 0\n"
        "v 0 0 1\n"
        "f 1 2 3\n"
        "o outlet_main\n"
        "v 1 0 0\n"
        "v 1 0 1\n"
        "v 1 1 0\n"
        "f 4 5 6\n"
        "o walls\n"
    )
    incomplete = geometry_io.inspect_geometry(
        obj,
        unit="m",
        require_watertight=False,
        internal_flow=True,
        boundary_roles={"inlet_main": "inlet", "outlet_main": "outlet"},
    )
    assert incomplete["readiness"]["geometry_ready"] is True
    assert incomplete["readiness"]["boundary_roles_ready"] is False
    assert any(
        issue["code"] == "BOUNDARY_REGIONS_UNMAPPED" for issue in incomplete["issues"]
    )
    complete = geometry_io.inspect_geometry(
        obj,
        unit="m",
        require_watertight=False,
        internal_flow=True,
        boundary_roles={
            "inlet_main": "inlet",
            "outlet_main": "outlet",
            "walls": "wall",
        },
    )
    assert complete["readiness"]["boundary_roles_ready"] is True
    assert complete["boundary_roles"]["suggestions"]["inlet_main"]["role"] == "inlet"
    assert complete["boundary_roles"]["suggestions"]["outlet_main"]["role"] == "outlet"


def test_internal_flow_rejects_named_flow_region_without_faces(tmp_path):
    obj = tmp_path / "empty-inlet.obj"
    obj.write_text(
        "o inlet\n"
        "o outlet\n"
        "v 0 0 0\n"
        "v 0 1 0\n"
        "v 0 0 1\n"
        "f 1 2 3\n"
    )

    report = geometry_io.inspect_geometry(
        obj,
        unit="m",
        require_watertight=False,
        internal_flow=True,
        boundary_roles={"inlet": "inlet", "outlet": "outlet"},
    )

    assert report["readiness"]["boundary_roles_ready"] is False
    assert any(
        issue["code"] == "FLOW_BOUNDARY_REGION_EMPTY"
        for issue in report["issues"]
    )


def test_name_role_suggestions_require_explicit_complete_acceptance(tmp_path):
    surface = tmp_path / "named.stl"
    surface.write_text(_named_tetra_stl())
    inspection = geometry_io.inspect_geometry(
        surface,
        unit="m",
        internal_flow=True,
    )

    assert geometry_io.accept_name_role_suggestions(inspection) == {
        "inlet": "inlet",
        "outlet": "outlet",
        "walls": "wall",
    }

    ambiguous = tmp_path / "ambiguous.stl"
    ambiguous.write_text(_ascii_stl("face_1", (((0, 0, 0), (1, 0, 0), (0, 1, 0)),)))
    ambiguous_inspection = geometry_io.inspect_geometry(
        ambiguous,
        unit="m",
        require_watertight=False,
    )
    with pytest.raises(geometry_io.GeometryInspectionError, match="face_1"):
        geometry_io.accept_name_role_suggestions(ambiguous_inspection)


def test_open_surface_requires_units_and_can_be_intentionally_allowed(tmp_path):
    surface = tmp_path / "open.stl"
    surface.write_text(
        _ascii_stl("", (((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),))
    )

    blocked = geometry_io.inspect_geometry(surface)
    codes = {issue["code"] for issue in blocked["issues"]}

    assert blocked["readiness"]["geometry_ready"] is False
    assert {"GEOMETRY_UNIT_UNDECLARED", "SURFACE_NOT_WATERTIGHT"} <= codes
    allowed = geometry_io.inspect_geometry(surface, unit="m", require_watertight=False)
    assert allowed["readiness"]["geometry_ready"] is True
    assert allowed["surface"]["boundary_edge_count"] == 3
    assert allowed["surface"]["enclosed_volume_m3"] is None
    assert any(
        issue["code"] == "BOUNDARY_REGIONS_UNNAMED" for issue in allowed["issues"]
    )


def test_explicit_vertex_tolerance_can_close_tessellation_roundoff(tmp_path):
    a = (0.0, 0.0, 0.0)
    b = (1.0, 0.0, 0.0)
    c = (0.0, 1.0, 0.0)
    d = (0.0, 0.0, 1.0)
    perturbed_b = (1.0 + 1.0e-9, 0.0, 0.0)
    surface = tmp_path / "roundoff.stl"
    surface.write_text(
        _ascii_stl(
            "fluid",
            ((a, c, b), (a, perturbed_b, d), (a, d, c), (b, c, d)),
        )
    )

    exact = geometry_io.inspect_geometry(surface, unit="m")
    merged = geometry_io.inspect_geometry(surface, unit="m", merge_tolerance=1.0e-6)

    assert exact["surface"]["watertight"] is False
    assert merged["surface"]["watertight"] is True
    assert merged["policy"]["merge_tolerance_native"] == 1.0e-6
    assert any(
        issue["code"] == "VERTEX_MERGE_TOLERANCE_APPLIED" for issue in merged["issues"]
    )


def test_binary_stl_and_topology_memory_guard_are_deterministic(tmp_path):
    binary = tmp_path / "triangle.stl"
    binary.write_bytes(
        b"AgentCFD binary STL".ljust(80, b"\0")
        + struct.pack("<I", 1)
        + struct.pack(
            "<12fH",
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0,
        )
    )
    binary_report = geometry_io.inspect_geometry(
        binary, unit="m", require_watertight=False
    )
    assert binary_report["source"]["encoding"] == "binary"
    assert binary_report["surface"]["triangle_count"] == 1

    many = tmp_path / "many.stl"
    triangles = tuple(
        ((0.0, 0.0, float(index)), (1.0, 0.0, float(index)), (0.0, 1.0, float(index)))
        for index in range(3)
    )
    many.write_text(_ascii_stl("layers", triangles))
    guarded = geometry_io.inspect_geometry(
        many,
        unit="m",
        require_watertight=False,
        topology_triangle_limit=2,
    )
    assert guarded["surface"]["topology_complete"] is False
    assert guarded["surface"]["unique_vertex_count"] is None
    assert guarded["surface"]["watertight"] is None
    assert guarded["surface"]["connected_component_count"] is None
    assert guarded["surface"]["connected_components"] is None
    assert any(
        issue["code"] == "TOPOLOGY_SCAN_LIMIT_REACHED" for issue in guarded["issues"]
    )


def test_obj_regions_polygon_triangulation_and_cad_fail_closed(tmp_path):
    obj = tmp_path / "plate.obj"
    obj.write_text("o inlet\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n")
    report = geometry_io.inspect_geometry(obj, unit="cm", require_watertight=False)
    assert report["surface"]["triangle_count"] == 2
    assert report["surface"]["region_names"] == ["inlet"]
    assert any(
        issue["code"] == "OBJ_POLYGONS_TRIANGULATED" for issue in report["issues"]
    )

    step = tmp_path / "valve.step"
    step.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    cad = geometry_io.inspect_geometry(step, unit="mm")
    assert cad["source"]["format"] == "step"
    assert cad["readiness"]["geometry_ready"] is False
    assert cad["issues"][0]["code"] == "CAD_TESSELLATION_REQUIRED"
