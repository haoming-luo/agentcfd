import json
from pathlib import Path

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
    assert first["recommendations"]["project_initialization"] == {
        "template": "industrial-elbow",
        "provider": "openfoam",
        "geometry_unit": "m",
        "geometry_path": None,
        "required_user_decisions": [
            "project_directory",
            "inlet_velocity_mass_flow_or_total_pressure",
        ],
    }
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
    readiness = solution_plan["readiness"]
    assert readiness["model_valid"] is True
    assert readiness["provider_compatible"] is True
    assert readiness["portable_io_available"] is True
    assert readiness["input_assets_ready"] is True
    assert readiness["mesh_intent_ready"] is True
    assert readiness["ready_to_run"] is readiness["runtime_available"]
    assert set(projects.Project(project).load_step().model.boundary_conditions) == {
        "inlet", "outlet", "walls"
    }


def test_industrial_elbow_project_init_and_geometry_sync_are_one_workflow(
    tmp_path, capsys
):
    project_path = tmp_path / "plant-elbow"
    assert entrypoint(
        [
            "init",
            str(project_path),
            "--template",
            "industrial-elbow",
            "--diameter-m",
            "0.1",
            "--bend-radius-m",
            "0.15",
            "--inlet-length-m",
            "0.3",
            "--outlet-length-m",
            "0.4",
            "--inlet-velocity-m-s",
            "1",
            "0",
            "0",
            "--json",
        ]
    ) == 0
    initialization = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-initialization.schema.json")
    ).validate(initialization)
    assert initialization["template"] == "industrial-elbow"
    assert initialization["provider"] == "openfoam"
    assert initialization["generated_geometry"]["synchronized"] is True
    assert initialization["generated_geometry"]["artifact"]["path"] == (
        "geometry/fluid.stl"
    )

    project = projects.Project(project_path)
    assert project.manifest.template == "industrial-elbow"
    assert project.manifest.generated_geometry_spec == "geometry/spec.json"
    geometry_directory = project_path / "geometry"
    spec_path = geometry_directory / "spec.json"
    generation_path = geometry_directory / "generation.json"
    inspection_path = geometry_directory / "inspection.json"
    asset_path = geometry_directory / "fluid.stl"
    spec = json.loads(spec_path.read_text())
    generation = json.loads(generation_path.read_text())
    inspection = json.loads(inspection_path.read_text())
    jsonschema.Draft202012Validator(
        contracts.load("generated-geometry-spec.schema.json")
    ).validate(spec)
    jsonschema.Draft202012Validator(
        contracts.load("generated-geometry.schema.json")
    ).validate(generation)
    state = project.sync_generated_geometry()
    jsonschema.Draft202012Validator(
        contracts.load("generated-geometry-sync.schema.json")
    ).validate(state)
    assert state["synchronized"] is True
    assert state["applied"] is False
    assert inspection["source"]["path"] == "geometry/fluid.stl"
    assert generation["artifact"]["path"] == "geometry/fluid.stl"
    plan = project.plan()
    assert plan["project"]["template"] == "industrial-elbow"
    assert plan["decisions"]["generated_geometry"]["synchronized"] is True
    assert project.snapshot()["project"]["template"] == "industrial-elbow"
    assert project.load_step().mesh.base_size == pytest.approx(0.0125)
    assert project.load_step().mesh.maximum_cells == 2_000_000
    assert "geometry/spec.json" in (project_path / "README.md").read_text()
    assert "Never edit `geometry/fluid.stl`" in (
        project_path / "AGENTS.md"
    ).read_text()

    generation["recommendations"]["interior_point_m"][0] += 0.001
    generation_path.write_text(json.dumps(generation, indent=2, sort_keys=True) + "\n")
    tampered = project.sync_generated_geometry()
    assert tampered["synchronized"] is False
    assert tampered["generation_record"]["matches_expected"] is False
    generation["recommendations"]["interior_point_m"][0] -= 0.001
    generation_path.write_text(json.dumps(generation, indent=2, sort_keys=True) + "\n")
    assert project.sync_generated_geometry()["synchronized"] is True

    previous_bytes = asset_path.read_bytes()
    previous_interior = project.load_step().model.domain.interior_point_m
    spec["parameters"]["bend_radius_m"] = 0.2
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    status = project.status()
    assert status["state"] == "blocked"
    assert status["readiness"]["input_assets_ready"] is False
    assert status["next_action"]["command"].startswith("agentcfd geometry-sync ")
    assert status["next_action"]["command"].endswith(" --apply")
    assert any(
        issue["code"] == "GENERATED_GEOMETRY_OUT_OF_DATE"
        for issue in status["issues"]
    )
    actions = project.actions()
    jsonschema.Draft202012Validator(
        contracts.load("project-actions.schema.json")
    ).validate(actions)
    geometry_action = next(
        item for item in actions["actions"] if item["operation"] == "geometry-sync"
    )
    assert geometry_action["recommended"] is True
    assert geometry_action["mutates_project"] is True
    assert geometry_action["starts_solver"] is False

    assert entrypoint(["geometry-sync", str(project_path), "--json"]) == 3
    preview = json.loads(capsys.readouterr().out)
    assert preview["synchronized"] is False
    assert preview["artifact"]["current_sha256"] != preview["artifact"]["expected_sha256"]
    assert asset_path.read_bytes() == previous_bytes

    assert entrypoint(
        ["geometry-sync", str(project_path), "--apply", "--json"]
    ) == 0
    refreshed = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("generated-geometry-sync.schema.json")
    ).validate(refreshed)
    assert refreshed["applied"] is True
    assert refreshed["synchronized"] is True
    assert asset_path.read_bytes() != previous_bytes
    assert project.plan()["readiness"]["input_assets_ready"] is True
    assert project.load_step().model.domain.interior_point_m != previous_interior
    assert json.loads(generation_path.read_text())["next_action"]["command"] == (
        "agentcfd status ."
    )
    relocated = tmp_path / "relocated-elbow"
    project_path.rename(relocated)
    assert projects.Project(relocated).sync_generated_geometry()["synchronized"] is True


def test_industrial_elbow_project_request_is_versioned_and_rejects_bad_geometry(
    tmp_path,
):
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "industrial-elbow",
        "provider": "openfoam",
        "generated_geometry": {
            "type": "circular-elbow-90deg",
            "diameter_m": 0.1,
            "bend_radius_m": 0.15,
            "inlet_length_m": 0.3,
            "outlet_length_m": 0.4,
        },
        "inlet_velocity_m_s": [1.0, 0.0, 0.0],
        "mesh": {"base_size_m": 0.0125, "maximum_cells": 500_000},
    }
    jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    ).validate(request)
    project = projects.init_project_from_request(tmp_path / "requested", request)
    assert project.sync_generated_geometry()["synchronized"] is True
    assert project.load_step().mesh.maximum_cells == 500_000

    bad = json.loads(json.dumps(request))
    bad["generated_geometry"]["bend_radius_m"] = 0.05
    root = tmp_path / "bad"
    with pytest.raises(projects.ProjectError, match="self-intersecting"):
        projects.init_project_from_request(root, bad)
    assert not root.exists()


def test_geometry_spec_filename_does_not_opt_an_unmanaged_project_into_sync(tmp_path):
    project = projects.init_project(tmp_path / "ordinary")
    geometry_directory = project.root / "geometry"
    geometry_directory.mkdir()
    (geometry_directory / "spec.json").write_text("{}\n")
    reopened = projects.Project(project.root)

    assert reopened.manifest.generated_geometry_spec is None
    assert reopened.plan()["decisions"]["generated_geometry"] is None
    with pytest.raises(projects.ProjectError, match="no managed geometry"):
        reopened.sync_generated_geometry()


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


def test_checked_in_generated_elbow_openfoam_mesh_evidence_is_valid():
    repository = Path(__file__).resolve().parents[1]
    record = json.loads(
        (repository / "docs/openfoam-v2606-generated-elbow-mesh.json").read_text()
    )
    jsonschema.Draft202012Validator(
        contracts.load("openfoam-imported-mesh-result.schema.json")
    ).validate(record)
    assert record["accepted"] is True
    assert record["quantities"]["mesh.policy_violation_count"]["value"] == 0.0
    assert any(
        check["code"] == "IMPORTED_MESH_LIMIT_POLICY_VIOLATION_COUNT"
        and check["status"] == "passed"
        for check in record["checks"]
    )
