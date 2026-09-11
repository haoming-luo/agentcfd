import json

import jsonschema
import pytest

from agentcfd import contracts, geometry_generation, projects
from agentcfd.cli import entrypoint


_OPTIONS = {
    "diameter_m": 0.1,
    "bend_radius_m": 0.15,
    "inlet_length_m": 0.3,
    "outlet_length_m": 0.4,
    "cross_section_segments": 24,
    "bend_segments": 12,
    "inlet_segments": 3,
    "outlet_segments": 4,
}


def test_circular_elbow_plan_is_exact_deterministic_and_non_mutating(tmp_path):
    output = tmp_path / "equipment" / "elbow.stl"
    first = geometry_generation.plan_circular_elbow_stl(output, **_OPTIONS)
    second = geometry_generation.plan_circular_elbow_stl(output, **_OPTIONS)

    jsonschema.Draft202012Validator(
        contracts.load("generated-geometry.schema.json")
    ).validate(first)
    assert first == second
    assert not output.exists()
    assert first["artifact"]["written"] is False
    assert first["geometry"]["triangle_count"] == 2 * 24 * (3 + 12 + 4 + 1)
    assert first["geometry"]["region_triangle_counts"] == {
        "inlet": 24,
        "walls": 2 * 24 * (3 + 12 + 4),
        "outlet": 24,
    }
    assert first["recommendations"]["boundary_roles"] == {
        "inlet": "inlet",
        "outlet": "outlet",
        "walls": "wall",
    }
    accuracy = first["geometry"]["tessellation_accuracy"]
    assert accuracy["cross_section_area_error_percent"] == pytest.approx(1.1384, rel=1.0e-4)
    assert first["recommendations"]["mesh_starting_point"] == {
        "base_size_m": 0.0125,
        "maximum_cells": 2_000_000,
        "status": "starting-point-not-accuracy-guarantee",
    }
    assert first["recommendations"]["project_initialization"]["geometry_path"] is None
    assert "--diameter-m 0.10000000000000001" in first["next_action"]["command"]
    assert "generated-geometry.schema.json" in contracts.available()


def test_circular_elbow_default_station_counts_adapt_to_dimensions(tmp_path):
    report = geometry_generation.plan_circular_elbow_stl(
        tmp_path / "elbow.stl",
        diameter_m=0.1,
        bend_radius_m=0.15,
        inlet_length_m=0.3,
        outlet_length_m=0.4,
    )
    parameters = report["geometry"]["parameters"]
    assert parameters["bend_segments"] == 12
    assert parameters["inlet_segments"] == 12
    assert parameters["outlet_segments"] == 16


def test_circular_elbow_is_watertight_oriented_and_has_named_boundaries(tmp_path):
    output, report = geometry_generation.write_circular_elbow_stl(
        tmp_path / "elbow.stl", **_OPTIONS
    )
    jsonschema.Draft202012Validator(
        contracts.load("generated-geometry.schema.json")
    ).validate(report)
    assert report["artifact"]["written"] is True
    assert report["recommendations"]["project_initialization"]["geometry_path"] == str(output)
    assert output.stat().st_size == report["artifact"]["size_bytes"]

    inspection = __import__("agentcfd").geometry_io.inspect_geometry(
        output,
        unit="m",
        boundary_roles={"inlet": "inlet", "outlet": "outlet", "walls": "wall"},
        internal_flow=True,
    )
    surface = inspection["surface"]
    assert inspection["readiness"]["geometry_ready"] is True
    assert inspection["readiness"]["boundary_roles_ready"] is True
    assert surface["watertight"] is True
    assert surface["orientation_conflict_count"] == 0
    assert surface["connected_component_count"] == 1
    assert surface["signed_volume_native"] > 0.0
    assert surface["region_names"] == ["inlet", "outlet", "walls"]
    assert surface["region_metrics"]["inlet"]["mean_unit_normal"] == pytest.approx(
        [-1.0, 0.0, 0.0], abs=1.0e-12
    )
    assert surface["region_metrics"]["outlet"]["mean_unit_normal"] == pytest.approx(
        [0.0, 1.0, 0.0], abs=1.0e-12
    )

    with pytest.raises(FileExistsError, match="already exists"):
        geometry_generation.write_circular_elbow_stl(output, **_OPTIONS)


def test_circular_elbow_cli_plans_then_writes(tmp_path, capsys):
    output = tmp_path / "elbow.stl"
    arguments = [
        "geometry-create", "elbow", str(output),
        "--diameter-m", "0.1", "--bend-radius-m", "0.15",
        "--inlet-length-m", "0.3", "--outlet-length-m", "0.4",
        "--cross-section-segments", "16", "--bend-segments", "8",
        "--inlet-segments", "2", "--outlet-segments", "2", "--json",
    ]
    assert entrypoint([*arguments, "--plan-only"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["artifact"]["written"] is False
    assert not output.exists()

    assert entrypoint(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["artifact"]["written"] is True
    assert output.is_file()

    project = tmp_path / "project"
    point = report["recommendations"]["interior_point_m"]
    assert entrypoint(
        [
            "init", str(project), "--template", "imported-internal-flow",
            "--provider", "openfoam", "--geometry", str(output), "--unit", "m",
            "--accept-name-roles", "--interior-point-m", *(str(value) for value in point),
            "--inlet-velocity-m-s", "1", "0", "0", "--base-size-m", "0.0125",
            "--maximum-cells", "500000", "--json",
        ]
    ) == 0
    capsys.readouterr()
    solution_plan = projects.Project(project).plan()
    assert solution_plan["readiness"]["ready_to_run"] is True
    assert set(projects.Project(project).load_step().model.boundary_conditions) == {
        "inlet", "outlet", "walls"
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"bend_radius_m": 0.05}, "self-intersecting"),
        ({"diameter_m": 0.0}, "positive"),
        ({"cross_section_segments": 7}, "at least 8"),
        ({"bend_segments": 513}, "must not exceed"),
    ],
)
def test_circular_elbow_rejects_unsafe_parameters(tmp_path, updates, message):
    options = {**_OPTIONS, **updates}
    with pytest.raises(ValueError, match=message):
        geometry_generation.plan_circular_elbow_stl(
            tmp_path / "elbow.stl", **options
        )
