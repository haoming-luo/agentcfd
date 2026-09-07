import json
import hashlib
import os
import shlex
import shutil
from pathlib import Path

import jsonschema
import pytest

from agentcfd import Artifact, Check, FieldRecord, contracts, geometry_io, projects
from agentcfd.cli import entrypoint
from agentcfd.errors import ProjectError


def test_process_liveness_treats_permission_denied_as_existing(monkeypatch):
    def denied(_pid, _signal):
        raise PermissionError("managed process boundary")

    monkeypatch.setattr(projects.os, "kill", denied)

    assert projects._process_is_alive(12345) is True


def test_project_lifecycle_is_one_readable_agent_and_human_workflow(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)

    assert project.manifest.default_provider == "reference"
    assert project.manifest.run_mode == "replace"
    assert project.run_root == root / "output"
    assert "def build" in (root / "case.py").read_text()
    assert (root / "AGENTS.md").is_file()
    assert "output/" in (root / ".gitignore").read_text()

    plan = project.plan()
    assert plan["readiness"] == {
        "model_valid": True,
        "provider_compatible": True,
        "runtime_available": True,
        "portable_io_available": True,
        "input_assets_ready": True,
        "mesh_intent_ready": True,
        "ready_to_run": True,
    }
    assert plan["decisions"]["solver"] == "Hagen-Poiseuille"
    assert plan["decisions"]["portable_formats"] == []
    assert (
        plan["decisions"]["output_plan"]["channels"]["field_frames"]["resolved_count"]
        == 1
    )
    assert plan["plan_sha256"].startswith("sha256:")
    jsonschema.Draft202012Validator(
        contracts.load("solution-plan.schema.json")
    ).validate(plan)

    completed = project.run()
    assert completed.result.accepted is True
    assert completed.field_bundle is None
    assert completed.plan_path.is_file()
    assert completed.result_path.is_file()
    assert (completed.directory / "run.json").is_file()
    assert "Start here" in (completed.directory / "README.md").read_text()
    assert completed.directory == root / "output"
    assert completed.mode == "replace"
    assert not (root / "__pycache__").exists()

    inspection = project.inspect()
    assert inspection["run_count"] == 1
    assert inspection["latest_run"]["run_id"] == completed.run_id
    assert inspection["latest_run"]["trust_level"] == "verified"


def test_imported_internal_flow_init_owns_inputs_and_is_ready_to_plan(tmp_path):
    original = (
        Path(__file__).parents[1]
        / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
    source = tmp_path / "input.stl"
    shutil.copyfile(original, source)
    roles = {"inlet": "inlet", "outlet": "outlet", "walls": "wall"}
    project = projects.init_project(
        tmp_path / "owned-duct",
        provider="openfoam",
        template="imported-internal-flow",
        geometry_path=source,
        geometry_unit="m",
        boundary_roles=roles,
        interior_point_m=(0.5, 0.25, 0.1),
        inlet_velocity_m_s=(0.5, 0.0, 0.0),
        base_size_m=0.05,
        maximum_cells=200_000,
    )

    copied = project.root / "geometry/input.stl"
    inspection = json.loads(
        (project.root / "geometry/inspection.json").read_text()
    )
    role_record = json.loads(
        (project.root / "geometry/boundary-roles.json").read_text()
    )
    plan = project.plan()
    step = project.load_step()

    assert copied.read_bytes() == source.read_bytes()
    assert inspection["source"]["path"] == "geometry/input.stl"
    assert inspection["readiness"]["ready_for_import_setup"] is True
    assert role_record == {
        "schema": "agentcfd.boundary-role-map/0.1",
        "regions": roles,
    }
    assert plan["readiness"]["input_assets_ready"] is True
    assert plan["readiness"]["mesh_intent_ready"] is True
    assert plan["readiness"]["provider_compatible"] is True
    assert plan["decisions"]["imported_mesh_plan"]["maximum_cells"] == 200_000
    assert step.model.boundary_conditions["inlet"].velocity == (0.5, 0.0, 0.0)
    assert step.model.metadata["inlet_velocity_direction"]["status"] == "passed"
    assert "cross_section_cells" not in project.manifest.openfoam

    with pytest.raises(ProjectError, match="Reverse or correct"):
        project.plan(parameters={"velocity_x": -0.5})

    source.write_bytes(source.read_bytes() + b"\nexternal change")
    assert copied.read_bytes() != source.read_bytes()
    assert project.plan()["readiness"]["input_assets_ready"] is True


def test_imported_internal_flow_init_fails_before_writing_unsupported_intent(
    tmp_path,
):
    source = (
        Path(__file__).parents[1]
        / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
    root = tmp_path / "unsupported"

    with pytest.raises(ProjectError, match="does not support roles: farfield"):
        projects.init_project(
            root,
            provider="openfoam",
            template="imported-internal-flow",
            geometry_path=source,
            geometry_unit="m",
            boundary_roles={
                "inlet": "inlet",
                "outlet": "outlet",
                "walls": "farfield",
            },
            interior_point_m=(0.5, 0.25, 0.1),
            inlet_velocity_m_s=(0.5, 0.0, 0.0),
            base_size_m=0.05,
            maximum_cells=200_000,
        )

    assert not root.exists()


def test_imported_internal_flow_rejects_reversed_velocity_before_writing(tmp_path):
    source = (
        Path(__file__).parents[1]
        / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
    root = tmp_path / "reversed-inlet"

    with pytest.raises(geometry_io.GeometryInspectionError, match="Reverse or correct"):
        projects.init_project(
            root,
            provider="openfoam",
            template="imported-internal-flow",
            geometry_path=source,
            geometry_unit="m",
            accept_name_roles=True,
            interior_point_m=(0.5, 0.25, 0.1),
            inlet_velocity_m_s=(-0.5, 0.0, 0.0),
            base_size_m=0.05,
            maximum_cells=200_000,
        )

    assert not root.exists()


def test_cli_initializes_imported_internal_flow_without_manual_case_authoring(
    tmp_path, capsys
):
    example = Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry"
    root = tmp_path / "cli-duct"

    assert (
        entrypoint(
            [
                "init",
                str(root),
                "--template",
                "imported-internal-flow",
                "--geometry",
                str(example / "fluid.stl"),
                "--unit",
                "m",
                "--roles",
                str(example / "boundary-roles.json"),
                "--interior-point-m",
                "0.5",
                "0.25",
                "0.1",
                "--inlet-velocity-m-s",
                "0.5",
                "0",
                "0",
                "--base-size-m",
                "0.05",
                "--maximum-cells",
                "200000",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-initialization.schema.json")
    ).validate(report)
    assert report["template"] == "imported-internal-flow"
    assert report["provider"] == "openfoam"
    assert projects.Project(root).plan()["readiness"]["provider_compatible"] is True
    mass_flow_plan = projects.Project(root).plan(
        parameters={"mass_flow_rate": 49.91}
    )
    assert mass_flow_plan["readiness"]["provider_compatible"] is True
    assert mass_flow_plan["model"]["summary"]["boundaries"]["inlet"] == {
        "type": "mass-flow-inlet",
        "mass_flow_rate": 49.91,
    }
    turbulent_parameters = {
        "turbulence_model": "k-omega-sst",
        "turbulence_intensity": 0.05,
        "turbulence_length_scale": 0.025,
    }
    turbulent_plan = projects.Project(root).plan(parameters=turbulent_parameters)
    assert turbulent_plan["readiness"]["provider_compatible"] is True
    assert turbulent_plan["decisions"]["required_capability"] == (
        "openfoam.steady-rans-imported-surface"
    )
    assert turbulent_plan["model"]["summary"]["boundaries"]["inlet"] == {
        "type": "turbulent-velocity-inlet",
        "velocity": [0.5, 0.0, 0.0],
        "turbulence_intensity": 0.05,
        "turbulence_length_scale": 0.025,
    }
    with pytest.raises(ProjectError, match="RANS mass-flow inlet is not released"):
        projects.Project(root).plan(
            parameters={**turbulent_parameters, "mass_flow_rate": 49.91}
        )
    with pytest.raises(ProjectError, match="violates its declared contract"):
        projects.Project(root).plan(parameters={"base_size": -0.01})
    with pytest.raises(ProjectError, match="must be an integer"):
        projects.Project(root).plan(parameters={"maximum_cells": 10.5})
    with pytest.raises(ProjectError, match="must be one of"):
        projects.Project(root).plan(parameters={"turbulence_model": "invented"})


def test_cli_initializes_imported_flow_with_mass_flow_as_primary_control(
    tmp_path, capsys
):
    example = Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry"
    root = tmp_path / "mass-flow-duct"

    assert (
        entrypoint(
            [
                "init",
                str(root),
                "--template",
                "imported-internal-flow",
                "--geometry",
                str(example / "fluid.stl"),
                "--unit",
                "m",
                "--roles",
                str(example / "boundary-roles.json"),
                "--interior-point-m",
                "0.5",
                "0.25",
                "0.1",
                "--inlet-mass-flow-kg-s",
                "49.91",
                "--base-size-m",
                "0.05",
                "--maximum-cells",
                "200000",
                "--json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["template"] == "imported-internal-flow"
    step = projects.Project(root).load_step()
    assert step.model.boundary_conditions["inlet"].to_dict() == {
        "type": "mass-flow-inlet",
        "mass_flow_rate": 49.91,
    }
    assert projects.Project(root).plan()["readiness"]["provider_compatible"] is True
    case_source = (root / "case.py").read_text()
    assert "mass_flow_rate=49.91" in case_source
    assert "velocity_x=0.0" in case_source


def test_cli_initializes_imported_flow_with_total_pressure_as_primary_control(
    tmp_path, capsys
):
    example = Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry"
    root = tmp_path / "pressure-driven-duct"

    assert (
        entrypoint(
            [
                "init",
                str(root),
                "--template",
                "imported-internal-flow",
                "--geometry",
                str(example / "fluid.stl"),
                "--unit",
                "m",
                "--roles",
                str(example / "boundary-roles.json"),
                "--interior-point-m",
                "0.5",
                "0.25",
                "0.1",
                "--inlet-total-gauge-pressure-pa",
                "10",
                "--base-size-m",
                "0.05",
                "--maximum-cells",
                "200000",
                "--json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "openfoam"
    project = projects.Project(root)
    assert project.load_step().model.boundary_conditions["inlet"].to_dict() == {
        "type": "pressure-inlet",
        "total_gauge_pressure": 10.0,
        "temperature": None,
    }
    assert project.plan()["readiness"]["ready_to_run"] is True
    case_source = (root / "case.py").read_text()
    assert "inlet_total_gauge_pressure=10.0" in case_source
    assert "velocity_x=0.0" in case_source


def test_imported_init_rejects_missing_or_conflicting_inlet_control_before_write(
    tmp_path,
):
    source = (
        Path(__file__).parents[1]
        / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
    common = {
        "provider": "openfoam",
        "template": "imported-internal-flow",
        "geometry_path": source,
        "geometry_unit": "m",
        "accept_name_roles": True,
        "interior_point_m": (0.5, 0.25, 0.1),
        "base_size_m": 0.05,
        "maximum_cells": 200_000,
    }
    missing_root = tmp_path / "missing-inlet"
    conflicting_root = tmp_path / "conflicting-inlet"

    with pytest.raises(ValueError, match="exactly one"):
        projects.init_project(missing_root, **common)
    with pytest.raises(ValueError, match="exactly one"):
        projects.init_project(
            conflicting_root,
            inlet_velocity_m_s=(0.5, 0.0, 0.0),
            inlet_mass_flow_kg_s=49.91,
            **common,
        )

    assert not missing_root.exists()
    assert not conflicting_root.exists()


def test_cli_can_explicitly_accept_unambiguous_name_roles(tmp_path, capsys):
    example = Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry"
    root = tmp_path / "accepted-name-roles"

    assert (
        entrypoint(
            [
                "init",
                str(root),
                "--template",
                "imported-internal-flow",
                "--geometry",
                str(example / "fluid.stl"),
                "--unit",
                "m",
                "--accept-name-roles",
                "--interior-point-m",
                "0.5",
                "0.25",
                "0.1",
                "--inlet-velocity-m-s",
                "0.5",
                "0",
                "0",
                "--base-size-m",
                "0.05",
                "--maximum-cells",
                "200000",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["template"] == "imported-internal-flow"
    assert json.loads((root / "geometry/boundary-roles.json").read_text())["regions"] == {
        "inlet": "inlet",
        "outlet": "outlet",
        "walls": "wall",
    }


def test_name_role_acceptance_fails_closed_before_project_write(tmp_path):
    source = tmp_path / "ambiguous.stl"
    source.write_text(
        "solid face_1\n"
        "facet normal 0 0 1\nouter loop\n"
        "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        "endloop\nendfacet\nendsolid face_1\n"
    )
    root = tmp_path / "should-not-exist"

    with pytest.raises(geometry_io.GeometryInspectionError, match="face_1"):
        projects.init_project(
            root,
            provider="openfoam",
            template="imported-internal-flow",
            geometry_path=source,
            geometry_unit="m",
            accept_name_roles=True,
            interior_point_m=(0.2, 0.2, 0.01),
            inlet_velocity_m_s=(0.5, 0.0, 0.0),
            base_size_m=0.05,
            maximum_cells=10_000,
        )
    assert not root.exists()


def test_versioned_creation_request_resolves_owned_geometry_beside_request(
    tmp_path, capsys
):
    source = (
        Path(__file__).parents[1]
        / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
    request_directory = tmp_path / "request"
    request_directory.mkdir()
    shutil.copyfile(source, request_directory / "fluid.stl")
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "imported-internal-flow",
        "provider": "openfoam",
        "geometry": {
            "path": "fluid.stl",
            "unit": "m",
            "boundary_roles": {
                "inlet": "inlet",
                "outlet": "outlet",
                "walls": "wall",
            },
        },
        "interior_point_m": [0.5, 0.25, 0.1],
        "inlet_velocity_m_s": [0.5, 0.0, 0.0],
        "mesh": {"base_size_m": 0.05, "maximum_cells": 200_000},
    }
    jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    ).validate(request)
    request_path = request_directory / "create.json"
    request_path.write_text(json.dumps(request))
    root = tmp_path / "owned"

    assert (
        entrypoint(
            ["init", str(root), "--request", str(request_path), "--json"]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-initialization.schema.json")
    ).validate(report)
    assert report["request_sha256"] == projects.content_fingerprint(request)
    assert (root / "geometry/fluid.stl").read_bytes() == source.read_bytes()
    assert projects.Project(root).plan()["readiness"]["ready_to_run"] is True


def test_creation_request_can_confirm_unambiguous_name_roles(tmp_path):
    source = Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "imported-internal-flow",
        "provider": "openfoam",
        "geometry": {
            "path": str(source),
            "unit": "m",
            "role_confirmation": "accept-name-suggestions",
        },
        "interior_point_m": [0.5, 0.25, 0.1],
        "inlet_velocity_m_s": [0.5, 0.0, 0.0],
        "mesh": {"base_size_m": 0.05, "maximum_cells": 200_000},
    }
    jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    ).validate(request)

    project = projects.init_project_from_request(tmp_path / "request-project", request)

    assert json.loads(
        (project.root / "geometry/boundary-roles.json").read_text()
    )["regions"] == {"inlet": "inlet", "outlet": "outlet", "walls": "wall"}


def test_creation_request_accepts_mass_flow_and_rejects_ambiguous_controls(tmp_path):
    source = Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "imported-internal-flow",
        "provider": "openfoam",
        "geometry": {
            "path": str(source),
            "unit": "m",
            "role_confirmation": "accept-name-suggestions",
        },
        "interior_point_m": [0.5, 0.25, 0.1],
        "inlet_mass_flow_kg_s": 49.91,
        "mesh": {"base_size_m": 0.05, "maximum_cells": 200_000},
    }
    validator = jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    )
    validator.validate(request)

    project = projects.init_project_from_request(tmp_path / "mass-request", request)
    assert project.load_step().model.boundary_conditions["inlet"].to_dict() == {
        "type": "mass-flow-inlet",
        "mass_flow_rate": 49.91,
    }

    ambiguous = {**request, "inlet_velocity_m_s": [0.5, 0.0, 0.0]}
    assert list(validator.iter_errors(ambiguous))
    ambiguous_root = tmp_path / "ambiguous-request"
    with pytest.raises(ProjectError, match="exactly one"):
        projects.init_project_from_request(ambiguous_root, ambiguous)
    assert not ambiguous_root.exists()

    pressure_request = dict(request)
    pressure_request.pop("inlet_mass_flow_kg_s")
    pressure_request["inlet_total_gauge_pressure_pa"] = 10.0
    validator.validate(pressure_request)
    pressure_project = projects.init_project_from_request(
        tmp_path / "pressure-request",
        pressure_request,
    )
    assert pressure_project.load_step().model.boundary_conditions["inlet"].to_dict() == {
        "type": "pressure-inlet",
        "total_gauge_pressure": 10.0,
        "temperature": None,
    }


def test_creation_request_rejects_unknown_automation_intent_before_writing(tmp_path):
    root = tmp_path / "unknown"
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "industrial-pipe",
        "provider": "reference",
        "solver_magic": True,
    }

    with pytest.raises(ProjectError, match="Unknown project creation request keys"):
        projects.init_project_from_request(root, request)

    assert not root.exists()


def test_project_plan_verifies_imported_geometry_asset_identity(tmp_path):
    root = tmp_path / "imported"
    project = projects.init_project(root)
    asset = root / "geometry" / "fluid.stl"
    asset.parent.mkdir()
    asset.write_bytes(b"content-addressed-test-surface")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    (root / "case.py").write_text(
        f"""from agentcfd import Model, boundaries, fluids, geometry, meshing, studies

def build():
    domain = geometry.ImportedSurface(
        asset="geometry/fluid.stl",
        source_sha256="sha256:{digest}",
        source_format="stl",
        unit="m",
        scale_to_m=1.0,
        boundary_roles=(("inlet", "inlet"), ("outlet", "outlet"), ("walls", "wall")),
        bounds_m=((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
        enclosed_volume_m3=1.0,
        interior_point_m=(0.5, 0.5, 0.5),
    )
    return Model(
        study=studies.internal_flow(),
        domain=domain,
        fluid=fluids.newtonian("water", density=1000.0, dynamic_viscosity=0.001),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(1.0),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    ).step(mesh=meshing.automatic(base_size=0.1, maximum_cells=100000))
"""
    )

    plan = project.plan()
    assert plan["readiness"]["model_valid"] is True
    assert plan["readiness"]["input_assets_ready"] is True
    assert plan["readiness"]["provider_compatible"] is False
    assert (
        plan["decisions"]["required_capability"]
        == "openfoam.steady-laminar-imported-surface"
    )
    assert plan["readiness"]["mesh_intent_ready"] is True
    assert plan["decisions"]["imported_mesh_plan"]["maximum_cells"] == 100_000
    assert {issue["code"] for issue in plan["issues"]} == {"PROVIDER_INCOMPATIBLE"}
    jsonschema.Draft202012Validator(
        contracts.load("solution-plan.schema.json")
    ).validate(plan)

    asset.write_bytes(b"changed")
    changed = project.plan()
    assert changed["readiness"]["input_assets_ready"] is False
    assert "IMPORTED_GEOMETRY_CHANGED" in {issue["code"] for issue in changed["issues"]}

    asset.unlink()
    missing = project.plan()
    assert missing["readiness"]["input_assets_ready"] is False
    assert "IMPORTED_GEOMETRY_MISSING" in {issue["code"] for issue in missing["issues"]}


def test_project_replace_mode_overwrites_only_managed_output(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)
    first = project.run()
    (first.directory / "obsolete.txt").write_text("old")

    second = project.run()

    assert second.directory == first.directory == root / "output"
    assert second.run_id != first.run_id
    assert not (second.directory / "obsolete.txt").exists()
    inspection = project.inspect()
    assert inspection["run_count"] == 1
    assert inspection["latest_run"]["run_id"] == second.run_id


def test_project_replace_mode_recovers_interrupted_owned_output(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps({"schema": "agentcfd.project-run/0.1", "status": "preparing"})
    )
    (project.run_root / "partial.dat").write_text("interrupted")

    completed = project.run()

    assert completed.result.accepted is True
    assert not (project.run_root / "partial.dat").exists()


def test_project_refuses_to_replace_a_live_owned_run(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "status": "running",
                "pid": os.getpid(),
            }
        )
    )

    with pytest.raises(ProjectError, match="active AgentCFD run"):
        project.run()


def test_project_records_repairable_solver_failure(tmp_path, monkeypatch):
    project = projects.init_project(tmp_path / "pipe")

    def fail(_provider, _step):
        raise RuntimeError("synthetic solver failure")

    monkeypatch.setattr("agentcfd.projects.ReferencePipeProvider.run", fail)

    with pytest.raises(RuntimeError, match="synthetic"):
        project.run()

    record = json.loads((project.run_root / "run.json").read_text())
    assert record["status"] == "failed"
    assert record["phase"] == "solver"
    assert record["failure"]["safe_to_retry"] is True
    assert "agentcfd run" in record["failure"]["repair"]


def test_project_refuses_to_publish_a_result_for_another_analysis(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    original = projects.ReferencePipeProvider.run

    def wrong_identity(provider, step):
        result = original(provider, step)
        result.provenance["analysis_sha256"] = "0" * 64
        return result

    monkeypatch.setattr(
        "agentcfd.projects.ReferencePipeProvider.run", wrong_identity
    )

    with pytest.raises(ProjectError, match="different analysis identity"):
        project.run()

    record = json.loads((project.run_root / "run.json").read_text())
    assert record["status"] == "failed"
    assert not (project.run_root / "result.json").exists()


def test_failed_openfoam_result_retains_workspace_and_guides_to_logs(
    tmp_path, monkeypatch
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    def fail(provider, _step):
        provider.case_directory.mkdir(parents=True)
        (provider.case_directory / "log.pimpleFoam").write_text(
            "Time = 0.1\nFOAM FATAL ERROR: synthetic failure\n"
        )
        return projects.SimulationResult(
            status="failed",
            converged=False,
            provider="openfoam",
            quantities={},
            checks=(),
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", fail)

    completed = project.run()
    status = project.status()

    assert completed.solver_workspace is not None
    assert completed.solver_workspace.is_dir()
    assert status["state"] == "failed"
    assert status["next_action"]["command"].startswith("agentcfd diagnose ")

    diagnosis = project.diagnose()
    jsonschema.Draft202012Validator(
        contracts.load("project-diagnosis.schema.json")
    ).validate(diagnosis)
    assert diagnosis["primary_finding"]["code"] == "OPENFOAM_FATAL_ERROR"
    assert diagnosis["observation_cost"]["field_payloads_opened"] == 0
    assert diagnosis["next_action"]["command"].startswith("agentcfd logs ")


def test_keep_workspace_persists_cleanup_protection_from_real_run_path(
    tmp_path, monkeypatch
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    project.manifest_path.write_text(
        project.manifest_path.read_text().replace(
            "export_fields = true", "export_fields = false"
        )
    )
    project = projects.Project(project.root)

    def complete(provider, _step):
        provider.case_directory.mkdir(parents=True)
        (provider.case_directory / "native-field").write_bytes(b"retained")
        return projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", complete)

    completed = project.run(keep_workspace=True)
    marker = json.loads(
        (completed.solver_workspace / ".agentcfd-workspace.json").read_text()
    )
    record = json.loads((completed.directory / "run.json").read_text())

    assert marker["retention_reason"] == "explicit-cli"
    assert marker["protected"] is True
    assert record["workspace_retention"] == {
        "retained": True,
        "reason": "explicit-cli",
        "protected_from_default_cleanup": True,
    }
    project.clean(apply=True)
    assert completed.solver_workspace.is_dir()


def test_summary_only_campaign_skips_portable_fields_and_removes_native_bulk(
    tmp_path, monkeypatch, capsys
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    def complete(provider, _step):
        provider.case_directory.mkdir(parents=True)
        native = provider.case_directory / "native-fields.bin"
        native.write_bytes(b"large-provider-native-payload")
        log = provider.case_directory / "log.pimpleFoam"
        log.write_text("End\n")
        return projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
            artifacts={
                "field_U": Artifact.from_path(native),
                "log_pimpleFoam": Artifact.from_path(
                    log, role="solver-log", media_type="text/plain"
                ),
            },
            fields={
                "fluid.velocity.cell": FieldRecord(
                    unit="m/s",
                    location="cell",
                    artifact=str(native),
                    components=("x", "y", "z"),
                    representation="provider-native",
                )
            },
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", complete)
    monkeypatch.setattr("agentcfd.projects.data_exchange.io_available", lambda: False)

    def reject_export(*_args, **_kwargs):
        raise AssertionError("summary-only campaign must not invoke field export")

    monkeypatch.setattr(
        "agentcfd.projects.data_exchange.export_openfoam_case", reject_export
    )
    points = {"base": {"mean_velocity": 0.5, "baffle_height": 0.12}}
    preview = project.plan_campaign(points, summary_only=True)
    full_preview = project.plan_campaign(points)

    assert preview["result_profile"] == "summary-only"
    assert preview["all_ready"] is True
    assert full_preview["all_ready"] is False
    summary_plan = project.plan(portable_fields=False)
    full_plan = project.plan()
    assert summary_plan["readiness"]["portable_io_available"] is True
    assert summary_plan["decisions"]["result_profile"] == "summary-only"
    assert summary_plan["decisions"]["output_plan"]["estimated_portable_bytes"] == 0
    assert (
        summary_plan["decisions"]["output_plan"]["estimated_temporary_peak_bytes"]
        < full_plan["decisions"]["output_plan"]["estimated_temporary_peak_bytes"]
    )
    assert (
        preview["points"][0]["result_execution_sha256"]
        != full_preview["points"][0]["result_execution_sha256"]
    )
    report = project.run_campaign(points, summary_only=True)
    assert report["points"][0]["error"] is None, report["points"][0]
    run_directory = Path(report["points"][0]["directory"])
    result = json.loads((run_directory / "result.json").read_text())
    marker = json.loads((run_directory / "run.json").read_text())

    jsonschema.Draft202012Validator(
        contracts.load("campaign-sweep.schema.json")
    ).validate(report)
    assert report["result_profile"] == "summary-only"
    assert marker["result_profile"] == "summary-only"
    assert result["provenance"]["result_profile"] == "summary-only"
    assert result["fields"] == {}
    assert "field_U" not in result["artifacts"]
    assert "log_pimpleFoam" in result["artifacts"]
    assert "intentionally keeps summaries" in (run_directory / "README.md").read_text()
    assert not (project.root / ".agentcfd" / "work").exists()
    index = project.campaign_index()
    jsonschema.Draft202012Validator(
        contracts.load("campaign-index.schema.json")
    ).validate(index)
    assert index["runs"][0]["result_profile"] == "summary-only"
    assert project.plan_campaign(points, summary_only=True)["reusable_count"] == 1
    assert project.plan_campaign(points)["reusable_count"] == 0
    request = project.root / "summary-sweep.json"
    request.write_text(
        json.dumps(
            {
                "schema": "agentcfd.campaign-request/0.1",
                "points": [
                    {
                        "name": "base",
                        "parameters": {
                            "mean_velocity": 0.5,
                            "baffle_height": 0.12,
                        },
                    }
                ],
            }
        )
    )
    assert (
        entrypoint(
            [
                "sweep",
                str(project.root),
                str(request),
                "--summary-only",
                "--plan-only",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result_profile"] == "summary-only"

    monkeypatch.setattr("agentcfd.projects.data_exchange.io_available", lambda: True)
    promotion_calls = []

    def publish_full_fields(_project, **kwargs):
        promotion_calls.append(kwargs)
        target = project.root / "campaigns" / "promoted-target"
        target.mkdir(exist_ok=True)
        promoted_result = projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
        )
        result_path = promoted_result.write(target / "result.json")
        plan_path = target / "plan.json"
        plan_path.write_text("{}\n")
        return projects.ProjectRun(
            run_id="promoted-target",
            directory=target,
            result=promoted_result,
            result_path=result_path,
            plan_path=plan_path,
            field_bundle=None,
            mode="campaign",
            solver_workspace=None,
        )

    monkeypatch.setattr(projects.Project, "run", publish_full_fields)
    source_run_id = report["points"][0]["run_id"]
    promotion = project.promote_campaign_run(source_run_id)

    jsonschema.Draft202012Validator(
        contracts.load("campaign-promotion.schema.json")
    ).validate(promotion)
    assert promotion["execution"] == "executed"
    assert promotion["target"]["result_profile"] == "full-fields"
    assert promotion["observation_cost"]["solver_processes_started"] == 1
    assert promotion_calls[0]["portable_fields"] is True
    assert promotion_calls[0]["_promotion_source_run_id"] == source_run_id
    assert entrypoint(["promote", str(project.root), source_run_id, "--json"]) == 0
    assert (
        json.loads(capsys.readouterr().out)["target"]["result_profile"] == "full-fields"
    )


def test_project_logs_are_bounded_and_prefer_retained_workspace(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    run_id = "failed-run"
    project.run_root.mkdir()
    evidence = project.run_root / "evidence"
    evidence.mkdir()
    (evidence / "pimpleFoam.log").write_text("older published log\n")
    workspace = project.root / ".agentcfd" / "work" / run_id / "openfoam"
    workspace.mkdir(parents=True)
    live_log = workspace / "log.pimpleFoam"
    live_log.write_text("".join(f"line {index}\n" for index in range(200)))
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "accepted": False,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    report = project.logs(lines=3)

    jsonschema.Draft202012Validator(
        contracts.load("project-logs.schema.json")
    ).validate(report)
    assert report["source"] == "workspace"
    assert report["returned_lines"] == 3
    assert report["truncated"] is True
    assert report["tail"] == "line 197\nline 198\nline 199\n"
    assert report["available_commands"] == ["pimpleFoam"]


def test_logs_cli_can_select_published_provider_evidence(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    evidence = project.run_root / "evidence"
    evidence.mkdir()
    (evidence / "checkMesh.log").write_text("Mesh OK.\nEnd\n")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "published-log",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "accepted": False,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    assert (
        entrypoint(
            [
                "logs",
                str(project.root),
                "--command",
                "checkMesh",
                "--lines",
                "1",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "published-evidence"
    assert report["tail"] == "End\n"


def test_diagnose_cli_reports_storage_failure_and_safe_preview(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    evidence = project.run_root / "evidence"
    evidence.mkdir()
    (evidence / "simpleFoam.log").write_text(
        "FOAM FATAL ERROR\nNo space left on device\n"
    )
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "failed-storage",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "accepted": False,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    assert entrypoint(["diagnose", str(project.root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["primary_finding"]["code"] == "DISK_SPACE_EXHAUSTED"
    assert report["next_action"]["command"].startswith("agentcfd clean ")


def test_project_resume_stages_identical_interrupted_checkpoint(tmp_path, monkeypatch):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    manifest = project.manifest_path.read_text()
    project.manifest_path.write_text(
        manifest.replace("export_fields = true", "export_fields = false")
    )
    project = projects.Project(project.root)
    step = project.load_step()
    plan = project.plan()
    run_id = "interrupted-run"
    source_case = project.root / ".agentcfd" / "work" / run_id / "openfoam"
    provider = projects.OpenFOAMChannelProvider(case_directory=source_case)
    provider.prepare(step)
    checkpoint = source_case / "0.5"
    checkpoint.mkdir()
    (checkpoint / "U").write_text("velocity")
    (checkpoint / "p").write_text("pressure")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "running",
                "phase": "solver",
                "pid": 99999999,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "execution_sha256": project._execution_fingerprint(
                    plan["model"]["analysis_sha256"], provider="openfoam"
                ),
                "started_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    observed = {}

    def complete(_provider, _step, **kwargs):
        archive = Path(kwargs["restart_archive"])
        observed["archive_exists"] = archive.is_file()
        observed["source_run_id"] = kwargs["source_run_id"]
        observed["analysis"] = kwargs["expected_analysis_sha256"]
        return projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
        )

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", complete)

    recovery = project.recovery()
    jsonschema.Draft202012Validator(
        contracts.load("project-recovery.schema.json")
    ).validate(recovery)
    assert recovery["available"] is True
    assert recovery["source"] == "retained-workspace"
    assert recovery["coordinate"] == {"name": "time", "value": 0.5, "unit": "s"}
    preview = project.clean()
    assert preview["protected_recovery_run_ids"] == [run_id]
    assert preview["candidate_bytes"] == 0

    completed = project.resume()
    record = json.loads((project.run_root / "run.json").read_text())

    assert completed.result.accepted is True
    assert observed == {
        "archive_exists": True,
        "source_run_id": run_id,
        "analysis": plan["model"]["analysis_sha256"],
    }
    assert record["resume"]["source_run_id"] == run_id
    assert not (project.root / ".agentcfd" / "work" / run_id).exists()


def test_project_resume_refuses_changed_analysis(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "old-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "failed",
                "analysis_sha256": "0" * 64,
                "execution_sha256": "1" * 64,
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    with pytest.raises(projects.ProjectError, match="inputs changed"):
        project.resume()


def test_resume_identity_excludes_timeout_and_publication_only_settings(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    step = project.load_step()
    analysis = step.fingerprint()
    first = project._resume_execution_fingerprint(analysis, provider="openfoam")
    full_first = project._execution_fingerprint(analysis, provider="openfoam")
    manifest = project.manifest_path.read_text()
    project.manifest_path.write_text(
        manifest.replace("timeout_seconds = 3600", "timeout_seconds = 7200")
        .replace("export_fields = true", "export_fields = false")
        .replace("keep_workspace = false", "keep_workspace = true")
    )
    changed = projects.Project(project.root)

    assert changed._resume_execution_fingerprint(analysis, provider="openfoam") == first
    assert changed._execution_fingerprint(analysis, provider="openfoam") != full_first


def test_project_campaign_mode_preserves_current_output_and_history(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)
    current = project.run()
    archived = project.run(campaign=True)

    assert current.directory == root / "output"
    assert archived.mode == "campaign"
    assert archived.directory.parent == root / "campaigns"
    assert (current.directory / "run.json").is_file()
    assert (archived.directory / "run.json").is_file()
    assert project.inspect()["run_count"] == 2


def test_explicit_factory_parameters_change_plan_identity_and_reject_typos(
    tmp_path, capsys
):
    project = projects.init_project(tmp_path / "pipe")
    default = project.plan()
    varied = project.plan(parameters={"mean_velocity": 0.03})

    assert varied["project"]["parameters"] == {"mean_velocity": 0.03}
    parameter = next(
        item
        for item in varied["project"]["factory_parameters"]
        if item["name"] == "mean_velocity"
    )
    assert parameter["required"] is False
    assert parameter["default"] == 0.02
    assert parameter["default_type"] == "float"
    assert parameter["overrideable"] is True
    assert parameter["selected"] is True
    assert parameter["current"] == 0.03
    assert parameter["input_contract"] == "json-scalar"
    assert parameter["metadata"] == {
        "kind": "number",
        "label": "Mean inlet velocity",
        "description": "Bulk inlet velocity used by the internal-flow model.",
        "unit": "m/s",
        "minimum": 0.0,
        "maximum": None,
        "exclusive_minimum": True,
        "choices": [],
        "nullable": False,
    }
    assert varied["model"]["reynolds_number"] != default["model"]["reynolds_number"]
    assert varied["plan_sha256"] != default["plan_sha256"]
    jsonschema.Draft202012Validator(
        contracts.load("solution-plan.schema.json")
    ).validate(varied)
    assert (
        entrypoint(
            [
                "plan",
                str(project.root),
                "--param",
                "mean_velocity=0.03",
                "--json",
            ]
        )
        == 0
    )
    cli_plan = json.loads(capsys.readouterr().out)
    assert cli_plan["project"]["parameters"] == {"mean_velocity": 0.03}
    with pytest.raises(ProjectError, match="rejected parameter"):
        project.plan(parameters={"mean_velocty": 0.03})
    with pytest.raises(ProjectError, match="JSON scalar"):
        project.plan(parameters={"mean_velocity": [0.02, 0.03]})


def test_status_discovers_parameter_contract_without_reimporting_case(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    original = projects._load_module
    calls = 0

    def counted(path, root):
        nonlocal calls
        calls += 1
        return original(path, root)

    monkeypatch.setattr(projects, "_load_module", counted)

    status = project.status()

    assert calls == 1
    assert [item["name"] for item in status["parameters"]] == [
        "length",
        "diameter",
        "mean_velocity",
    ]
    assert all(item["overrideable"] for item in status["parameters"])


def test_parameter_set_is_portable_and_cli_values_take_precedence(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    parameter_file = tmp_path / "operating-point.json"
    payload = {
        "schema": "agentcfd.parameter-set/0.1",
        "parameters": {"diameter": 0.08, "mean_velocity": 0.015},
    }
    jsonschema.Draft202012Validator(
        contracts.load("parameter-set.schema.json")
    ).validate(payload)
    parameter_file.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        entrypoint(
            [
                "plan",
                str(project.root),
                "--param-file",
                str(parameter_file),
                "--param",
                "mean_velocity=0.02",
                "--json",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["project"]["parameters"] == {
        "diameter": 0.08,
        "mean_velocity": 0.02,
    }

    assert (
        entrypoint(
            [
                "run",
                str(project.root),
                "--param-file",
                str(parameter_file),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    run_record = json.loads((project.root / "output" / "run.json").read_text())
    assert run_record["parameters"] == payload["parameters"]


def test_parameter_set_rejects_ambiguous_or_nested_json(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"agentcfd.parameter-set/0.1","parameters":{},"parameters":{}}',
        encoding="utf-8",
    )
    assert (
        entrypoint(
            ["plan", str(project.root), "--param-file", str(duplicate), "--json"]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().out)
    assert "duplicate key" in error["error"]["message"]

    nested = tmp_path / "nested.json"
    nested.write_text(
        json.dumps(
            {
                "schema": "agentcfd.parameter-set/0.1",
                "parameters": {"mean_velocity": [0.02, 0.03]},
            }
        ),
        encoding="utf-8",
    )
    assert entrypoint(["check", str(project.root), "--param-file", str(nested)]) == 2
    assert "JSON scalar" in capsys.readouterr().err


def test_campaign_index_and_csv_are_compact_field_free_design_point_tables(
    tmp_path, capsys
):
    project = projects.init_project(tmp_path / "pipe")
    first = project.run(campaign=True)
    second = project.run(campaign=True, parameters={"mean_velocity": 0.03})

    report = project.campaign_index()
    jsonschema.Draft202012Validator(
        contracts.load("campaign-index.schema.json")
    ).validate(report)
    assert report["run_count"] == 2
    assert report["accepted_count"] == 2
    assert report["total_bytes"] is None
    assert report["observation_cost"] == {
        "run_markers_opened": 2,
        "plan_files_opened": 0,
        "result_manifests_opened": 0,
        "field_payloads_opened": 0,
        "recursive_storage_scans": 0,
    }
    assert [row["run_id"] for row in report["runs"]] == sorted(
        [first.run_id, second.run_id]
    )
    assert report["runs"][0]["quantities"]["flow.pressure_drop"]["unit"] == "Pa"
    assert report["runs"][0]["parameters"] == {}
    assert report["runs"][1]["parameters"] == {"mean_velocity": 0.03}

    csv_path, with_storage = project.export_campaign_csv(
        tmp_path / "design-points.csv", include_storage=True
    )
    assert with_storage["total_bytes"] > 0
    assert with_storage["observation_cost"]["recursive_storage_scans"] == 2
    table = csv_path.read_text()
    assert "parameter:mean_velocity" in table.splitlines()[0]
    assert "quantity:flow.pressure_drop [Pa]" in table.splitlines()[0]
    assert first.run_id in table and second.run_id in table

    assert (
        entrypoint(
            [
                "run",
                "project",
                str(project.root),
                "--campaign",
                "--param",
                "mean_velocity=0.025",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert entrypoint(["campaigns", str(project.root), "--json"]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["run_count"] == 3
    assert cli_report["observation_cost"]["field_payloads_opened"] == 0


def test_campaign_sweep_preflights_all_points_and_reuses_accepted_identity(
    tmp_path, capsys
):
    project = projects.init_project(tmp_path / "pipe")
    points = {
        "base": {"mean_velocity": 0.02},
        "faster": {"mean_velocity": 0.03},
        "base-repeat": {"mean_velocity": 0.02},
    }

    preview = project.plan_campaign(points)
    jsonschema.Draft202012Validator(
        contracts.load("campaign-plan.schema.json")
    ).validate(preview)
    assert preview["all_ready"] is True
    assert preview["would_execute_count"] == 2
    assert preview["reusable_count"] == 1
    assert preview["points"][2]["reuse_source"] == "request-duplicate"
    assert preview["observation_cost"]["solver_processes_started"] == 0

    with pytest.raises(ProjectError, match="exceeding the explicit --max-runs 1"):
        project.run_campaign(points, maximum_solver_runs=1)
    assert project.campaign_index()["run_count"] == 0

    report = project.run_campaign(points)

    jsonschema.Draft202012Validator(
        contracts.load("campaign-sweep.schema.json")
    ).validate(report)
    assert report["successful"] is True
    assert report["executed_count"] == 2
    assert report["reused_count"] == 1
    assert report["deduplicated_count"] == 0
    assert report["solver_budget"] == {
        "maximum_runs": None,
        "planned_new_runs": 2,
        "solver_processes_started": 2,
    }
    assert [point["outcome"] for point in report["points"]] == [
        "accepted",
        "accepted",
        "accepted",
    ]
    assert report["points"][2]["run_id"] == report["points"][0]["run_id"]
    progress = json.loads(Path(report["progress"]).read_text())
    assert progress == report
    index = project.campaign_index()
    assert {row["design_point_name"] for row in index["runs"]} == {"base", "faster"}

    after = project.plan_campaign(points)
    assert after["would_execute_count"] == 0
    assert after["reusable_count"] == 3

    before = index["run_count"]
    with pytest.raises(ProjectError, match="No design point was executed"):
        project.run_campaign(
            {
                "valid": {"mean_velocity": 0.025},
                "outside-reference-capability": {"mean_velocity": 1.0},
            }
        )
    assert project.campaign_index()["run_count"] == before

    request = tmp_path / "sweep.json"
    request.write_text(
        json.dumps(
            {
                "schema": "agentcfd.campaign-request/0.1",
                "points": [
                    {"name": "base-again", "parameters": {"mean_velocity": 0.02}}
                ],
            }
        )
    )
    assert (
        entrypoint(
            [
                "sweep",
                str(project.root),
                str(request),
                "--plan-only",
                "--json",
            ]
        )
        == 0
    )
    cli_preview = json.loads(capsys.readouterr().out)
    assert cli_preview["would_execute_count"] == 0
    assert cli_preview["observation_cost"]["solver_processes_started"] == 0
    assert (
        entrypoint(
            [
                "sweep",
                str(project.root),
                str(request),
                "--max-runs",
                "0",
                "--json",
            ]
        )
        == 0
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["executed_count"] == 0
    assert cli_report["reused_count"] == 1

    cached_result = Path(report["points"][0]["directory"]) / "result.json"
    cached_result.unlink()
    missing_payload = project.plan_campaign(
        {"base-with-missing-result": {"mean_velocity": 0.02}}
    )
    assert missing_payload["reusable_count"] == 0
    assert missing_payload["would_execute_count"] == 1


def test_campaign_sweep_records_runtime_failure_and_continues(tmp_path, monkeypatch):
    project = projects.init_project(tmp_path / "pipe")
    original = projects.ReferencePipeProvider.run
    attempts = 0

    def fail_first(provider, step):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic isolated design-point failure")
        return original(provider, step)

    monkeypatch.setattr("agentcfd.projects.ReferencePipeProvider.run", fail_first)
    report = project.run_campaign(
        {
            "first": {"mean_velocity": 0.02},
            "first-copy": {"mean_velocity": 0.02},
            "second": {"mean_velocity": 0.03},
        }
    )

    assert report["complete"] is True
    assert report["successful"] is False
    assert report["failed_count"] == 2
    assert report["accepted_count"] == 1
    assert report["executed_count"] == 2
    assert report["deduplicated_count"] == 1
    assert report["points"][1]["execution"] == "deduplicated"
    assert report["points"][1]["run_id"] == report["points"][0]["run_id"]
    assert report["points"][0]["error"]["type"] == "RuntimeError"
    assert report["points"][0]["run_id"] is not None
    assert Path(report["points"][0]["directory"]).is_dir()
    assert shlex.split(report["points"][0]["diagnose_command"])[-2:] == [
        "--run-id",
        report["points"][0]["run_id"],
    ]
    assert report["points"][2]["outcome"] == "accepted"


def test_historical_campaign_failure_remains_diagnosable_by_run_id(tmp_path, capsys):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    campaigns = project.root / "campaigns"
    failed = campaigns / "failed-point"
    succeeded = campaigns / "later-success"
    (failed / "evidence").mkdir(parents=True)
    succeeded.mkdir(parents=True)
    (failed / "evidence" / "pimpleFoam.log").write_text(
        "Time = 0.1\nFOAM FATAL ERROR: historical synthetic failure\n"
    )
    common = {
        "schema": "agentcfd.project-run/0.1",
        "mode": "campaign",
        "parameters": {"mean_velocity": 0.5, "baffle_height": 0.12},
        "analysis_sha256": plan["model"]["analysis_sha256"],
        "result_execution_sha256": project._result_execution_fingerprint(
            plan["model"]["analysis_sha256"], provider="openfoam"
        ),
    }
    (failed / "run.json").write_text(
        json.dumps(
            {
                **common,
                "run_id": "failed-point",
                "directory": str(failed),
                "status": "failed",
                "accepted": False,
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    (succeeded / "run.json").write_text(
        json.dumps(
            {
                **common,
                "run_id": "later-success",
                "directory": str(succeeded),
                "status": "completed",
                "accepted": True,
                "completed_at": "2026-09-06T00:01:00+00:00",
            }
        )
    )

    report = project.diagnose(run_id="failed-point")

    jsonschema.Draft202012Validator(
        contracts.load("project-diagnosis.schema.json")
    ).validate(report)
    assert report["run_id"] == "failed-point"
    assert report["primary_finding"]["code"] == "OPENFOAM_FATAL_ERROR"
    assert shlex.split(report["next_action"]["command"])[-2:] == [
        "--run-id",
        "failed-point",
    ]
    assert (
        entrypoint(
            [
                "logs",
                str(project.root),
                "--run-id",
                "failed-point",
                "--lines",
                "1",
                "--json",
            ]
        )
        == 0
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["run_id"] == "failed-point"
    assert "historical synthetic failure" in cli_report["tail"]
    with pytest.raises(ProjectError, match="No project run exists"):
        project.logs(run_id="missing-run")


def test_campaign_compaction_is_preview_first_and_preserves_derived_outputs(
    tmp_path, monkeypatch, capsys
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    monkeypatch.setattr("agentcfd.projects.data_exchange.io_available", lambda: True)
    parameters = {"mean_velocity": 0.5, "baffle_height": 0.12}
    plan = project.plan(parameters=parameters, portable_fields=True)
    run_id = "accepted-full-fields"
    run_directory = project.root / "campaigns" / run_id
    fields = run_directory / "fields"
    postprocess = run_directory / "postprocess"
    evidence = run_directory / "evidence"
    fields.mkdir(parents=True)
    postprocess.mkdir()
    evidence.mkdir()
    xdmf = fields / "fields.xdmf"
    h5 = fields / "fields.h5"
    manifest = fields / "manifest.json"
    xdmf.write_text("<Xdmf/>\n")
    h5.write_bytes(b"portable-volume-fields" * 100)
    manifest.write_text("{}\n")
    recipe_manifest = postprocess / "manifest.json"
    recipe_script = postprocess / "midplane.py"
    derived_csv = postprocess / "centerline.csv"
    recipe_manifest.write_text("{}\n")
    recipe_script.write_text("# generated recipe\n")
    derived_csv.write_text("distance_m,fluid.pressure\n0,1\n")
    log = evidence / "pimpleFoam.log"
    log.write_text("End\n")
    result = projects.SimulationResult(
        status="completed",
        converged=True,
        provider="openfoam",
        quantities={},
        checks=(Check("execution", True, kind="runtime"),),
        fields={
            "fluid.pressure.cell": FieldRecord(
                unit="Pa",
                location="cell",
                artifact=str(xdmf),
                components=("scalar",),
                representation="xdmf-hdf5",
            )
        },
        artifacts={
            "fields.xdmf": Artifact.from_path(xdmf, role="portable-field-bundle"),
            "fields.hdf5": Artifact.from_path(h5, role="portable-field-bundle"),
            "fields.manifest": Artifact.from_path(
                manifest, role="portable-field-bundle"
            ),
            "postprocess.manifest": Artifact.from_path(
                recipe_manifest, role="post-processing-recipe-index"
            ),
            "postprocess.midplane": Artifact.from_path(
                recipe_script, role="paraview-python-recipe"
            ),
            "log_pimpleFoam": Artifact.from_path(log, role="solver-log"),
        },
        provenance={"result_profile": "full-fields"},
    )
    result.write(run_directory / "result.json")
    full_identity = project._result_execution_fingerprint(
        plan["model"]["analysis_sha256"],
        provider="openfoam",
        portable_fields=True,
    )
    (run_directory / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "campaign",
                "directory": str(run_directory),
                "status": "completed",
                "accepted": True,
                "result_profile": "full-fields",
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "result_execution_sha256": full_identity,
                "parameters": parameters,
                "design_point_name": "base",
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    (run_directory / "plan.json").write_text(json.dumps(plan))
    (run_directory / "README.md").write_text("# Full result\n")

    assert entrypoint(["compact", str(project.root), run_id, "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("campaign-compaction.schema.json")
    ).validate(preview)
    assert preview["applied"] is False
    assert preview["candidate_bytes"] > 0
    assert h5.is_file()
    assert derived_csv.is_file()

    applied = project.compact_campaign_run(run_id, apply=True)
    compacted_result = projects.read_result_record(
        run_directory / "result.json", verify_artifacts=True
    )
    compacted_marker = json.loads((run_directory / "run.json").read_text())

    assert applied["reclaimed_bytes"] == preview["candidate_bytes"]
    assert not fields.exists()
    assert not recipe_manifest.exists()
    assert not recipe_script.exists()
    assert derived_csv.is_file()
    assert log.is_file()
    assert compacted_result["fields"] == {}
    assert set(compacted_result["artifacts"]) == {"log_pimpleFoam"}
    assert compacted_result["provenance"]["result_profile"] == "summary-only"
    assert compacted_marker["result_profile"] == "summary-only"
    assert compacted_marker["result_execution_sha256"] != full_identity
    with pytest.raises(ProjectError, match="already summary-only"):
        project.compact_campaign_run(run_id)


def test_project_replace_mode_refuses_unmanaged_output(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)
    project.run_root.mkdir()
    (project.run_root / "notes.txt").write_text("user-owned")

    with pytest.raises(ProjectError, match="unmanaged output"):
        project.run()

    assert (project.run_root / "notes.txt").read_text() == "user-owned"


def test_project_init_refuses_to_overwrite_user_directory(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    (root / "notes.txt").write_text("preserve")

    with pytest.raises(FileExistsError, match="not empty"):
        projects.init_project(root)

    assert (root / "notes.txt").read_text() == "preserve"


def test_project_plan_returns_addressable_physics_issue(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan(parameters={"mean_velocity": 1.0})

    assert plan["readiness"]["ready_to_run"] is False
    issue = next(
        issue
        for issue in plan["issues"]
        if issue["code"] == "REFERENCE_REYNOLDS_OUT_OF_RANGE"
    )
    assert issue["severity"] == "error"
    assert "OpenFOAM" in issue["repair"]

    with pytest.raises(ProjectError, match="not ready"):
        project.run(parameters={"mean_velocity": 1.0})


def test_project_plan_rejects_accidental_full_field_frame_explosion(tmp_path):
    project = projects.init_project(tmp_path / "pipe", provider="openfoam")
    case = project.root / "case.py"
    case.write_text(
        case.read_text()
        .replace("studies.internal_flow()", "studies.internal_flow(steady=False)")
        .replace(
            "procedure=procedures.steady(),",
            "procedure=procedures.transient(end_time=10.0, initial_time_step=0.001),",
        )
        .replace(
            "output=outputs.standard(),",
            "output=outputs.animation(every=0.001, maximum_frames=100),",
        )
    )

    plan = project.plan()

    assert plan["readiness"]["ready_to_run"] is False
    issue = next(
        item for item in plan["issues"] if item["code"] == "OUTPUT_POLICY_INVALID"
    )
    assert "10000 full-field frames" in issue["message"]


def test_project_paths_cannot_escape_root(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    manifest = project.manifest_path.read_text().replace(
        'entrypoint = "case.py"',
        'entrypoint = "../case.py"',
    )
    project.manifest_path.write_text(manifest)

    with pytest.raises(ProjectError, match="escapes"):
        projects.Project(project.root)


def test_project_discovers_nearest_manifest_from_nested_directories_and_files(tmp_path):
    outer = projects.init_project(tmp_path / "outer")
    inner = projects.init_project(outer.root / "input" / "nested-project")
    deep = inner.root / "output" / "fields"
    deep.mkdir(parents=True)

    assert projects.Project(deep).root == inner.root
    assert projects.Project.discover(deep).root == inner.root
    assert projects.Project(inner.entrypoint).root == inner.root


def test_project_next_action_uses_dot_from_any_nested_working_directory(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe with spaces")
    nested = project.root / "input" / "cad"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    report = projects.Project.discover().status()

    assert report["root"] == str(project.root)
    assert shlex.split(report["next_action"]["command"]) == ["agentcfd", "run", "."]


def test_project_cli_init_check_run_and_inspect(tmp_path, capsys):
    root = tmp_path / "cli-pipe"

    assert entrypoint(["init", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "reference"
    assert entrypoint(["check", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert entrypoint(["run", "project", str(root), "--json"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["accepted"] is True
    assert Path(run["result"]).is_file()
    assert entrypoint(["inspect", str(root), "--json"]) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection["run_count"] == 1


def test_baffle_channel_template_selects_openfoam_and_plans_cleanly(tmp_path, capsys):
    root = tmp_path / "wake"

    assert (
        entrypoint(["init", str(root), "--template", "baffle-channel", "--json"]) == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["provider"] == "openfoam"
    plan = projects.Project(root).plan()
    assert plan["readiness"]["provider_compatible"] is True
    assert plan["decisions"]["solver"] == "pimpleFoam"
    output_plan = plan["decisions"]["output_plan"]
    assert output_plan["estimated_mesh_cells"] == 23880
    assert output_plan["estimate_calibration"]["safety_factor"] == 1.25
    assert (
        output_plan["estimated_temporary_peak_bytes"]
        >= 1.25 * output_plan["estimate_calibration"]["raw_staging_bytes"]
    )
    assert [
        item["name"]
        for item in plan["decisions"]["output_plan"]["channels"]["views"]["definitions"]
    ] == ["midplane-vorticity", "wake-streamlines", "centerline-pressure"]


def test_baffle_channel_template_rejects_reference_provider(tmp_path):
    with pytest.raises(ValueError, match="requires provider='openfoam'"):
        projects.init_project(
            tmp_path / "wake", template="baffle-channel", provider="reference"
        )


def test_agentfem_style_run_alias_targets_current_project(tmp_path, capsys):
    root = tmp_path / "short-run"
    projects.init_project(root)

    assert entrypoint(["run", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "reference-pipe"


def test_project_status_guides_ready_complete_and_modified_workflows(tmp_path):
    project = projects.init_project(tmp_path / "pipe")

    ready = project.status()
    jsonschema.Draft202012Validator(
        contracts.load("project-status.schema.json")
    ).validate(ready)
    invalid_parameter_contract = json.loads(json.dumps(ready))
    invalid_parameter_contract["parameters"][0]["metadata"]["unit"] = 42
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(
            contracts.load("project-status.schema.json")
        ).validate(invalid_parameter_contract)
    assert ready["state"] == "ready"
    assert ready["next_action"]["command"].startswith("agentcfd run ")
    assert ready["postprocess"]["primary"] is None

    project.run()
    complete = project.status()
    assert complete["state"] == "complete"
    assert complete["next_action"]["command"].startswith("agentcfd view ")
    assert complete["postprocess"]["result"].endswith("output/result.json")

    case = project.entrypoint
    case.write_text(
        case.read_text().replace('name="water-pipe"', 'name="water-pipe-v2"')
    )
    modified = project.status()
    assert modified["state"] == "modified"
    assert modified["inputs_changed"] is True
    assert "Inputs changed" in modified["next_action"]["reason"]


def test_result_summary_is_lightweight_filterable_and_cli_visible(
    tmp_path, capsys
):
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()

    report = project.result_summary(quantities=("flow.pressure_drop",))

    jsonschema.Draft202012Validator(
        contracts.load("result-summary.schema.json")
    ).validate(report)
    assert report["run_id"] == completed.run_id
    assert set(report["quantities"]) == {"flow.pressure_drop"}
    assert "flow.mass_flow_rate" in report["available"]["quantities"]
    assert report["observation_cost"]["field_payloads_opened"] == 0
    assert report["artifact_integrity"]["verified"] is False

    assert (
        entrypoint(
            [
                "result",
                str(project.root),
                "--quantity",
                "flow.pressure_drop",
                "--json",
            ]
        )
        == 0
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert set(cli_report["quantities"]) == {"flow.pressure_drop"}

    assert entrypoint(["result", str(project.root)]) == 0
    human = capsys.readouterr().out
    assert "Flow results:\n" in human
    assert "  flow.mass_flow_rate:" in human
    assert "Other results:\n" in human
    assert " [-]" in human

    with pytest.raises(ProjectError, match="Unknown result quantities"):
        project.result_summary(quantities=("flow.misspelled",))


def test_project_doctor_combines_health_resource_and_energy_truthfulness(
    tmp_path, capsys
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    report = project.doctor()

    jsonschema.Draft202012Validator(
        contracts.load("project-doctor.schema.json")
    ).validate(report)
    assert report["resource_estimate"]["estimated_mesh_cells"] == 23880
    assert report["resource_estimate"]["nominal_solver_steps"] == 400
    assert report["resource_estimate"]["cell_updates_proxy"] == 19_104_000
    assert report["resource_estimate"]["energy"]["status"] == "not-measured"
    assert report["observation_cost"]["field_payloads_opened"] == 0

    expected_exit = 0 if report["healthy"] else 3
    assert entrypoint(["doctor", str(project.root), "--json"]) == expected_exit
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["resource_estimate"]["cell_updates_proxy"] == 19_104_000
    assert (
        cli_report["resource_estimate"]["energy"]
        == report["resource_estimate"]["energy"]
    )


def test_project_status_next_command_preserves_paths_with_spaces(tmp_path):
    project = projects.init_project(tmp_path / "pipe with spaces")

    command = project.status()["next_action"]["command"]

    assert shlex.split(command) == ["agentcfd", "run", str(project.root)]


def test_project_status_is_portable_when_a_legacy_run_directory_moves(tmp_path):
    original = projects.init_project(tmp_path / "original")
    original.run()
    record_path = original.run_root / "run.json"
    record = json.loads(record_path.read_text())
    record.pop("analysis_sha256", None)
    record.pop("execution_sha256", None)
    record_path.write_text(json.dumps(record))
    moved_root = tmp_path / "moved project"
    shutil.copytree(original.root, moved_root)

    report = projects.Project(moved_root).status()

    assert report["state"] == "complete"
    assert report["inputs_changed"] is False
    assert report["postprocess"]["result"] == str(moved_root / "output" / "result.json")


def test_project_status_detects_operational_provider_setting_changes(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    analysis_sha = plan["model"]["analysis_sha256"]
    execution_sha = project._execution_fingerprint(analysis_sha, provider="openfoam")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "prior-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": analysis_sha,
                "execution_sha256": execution_sha,
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text().replace("keep_workspace = false", "keep_workspace = true")
    )

    report = projects.Project(project.root).status()

    assert report["inputs_changed"] is True
    assert report["state"] in {"modified", "blocked"}


def test_project_status_does_not_invalidate_result_for_timeout_or_workspace_policy(
    tmp_path,
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    analysis_sha = plan["model"]["analysis_sha256"]
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "modern-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": analysis_sha,
                "execution_sha256": project._execution_fingerprint(
                    analysis_sha, provider="openfoam"
                ),
                "result_execution_sha256": project._result_execution_fingerprint(
                    analysis_sha, provider="openfoam"
                ),
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        .replace("keep_workspace = false", "keep_workspace = true")
        .replace("timeout_seconds = 3600", "timeout_seconds = 7200")
    )

    report = projects.Project(project.root).status()

    assert report["inputs_changed"] is False
    assert report["state"] == "complete"


def test_completed_result_remains_viewable_when_optional_runtime_is_absent(
    tmp_path, monkeypatch
):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    analysis_sha = plan["model"]["analysis_sha256"]
    project.run_root.mkdir()
    (project.run_root / "result.json").write_text("{}")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "portable-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": analysis_sha,
                "execution_sha256": project._execution_fingerprint(
                    analysis_sha, provider="openfoam"
                ),
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr("agentcfd.projects.data_exchange.io_available", lambda: False)

    report = project.status()

    assert report["readiness"]["portable_io_available"] is False
    assert report["state"] == "complete"
    assert report["next_action"]["command"].startswith("agentcfd view ")


def test_project_status_marks_dead_in_progress_record_as_interrupted(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "interrupted",
                "directory": str(project.run_root),
                "status": "running",
                "pid": 999_999_999,
                "plan_sha256": project.plan()["plan_sha256"],
                "completed_at": None,
            }
        )
    )

    report = project.status()

    assert report["state"] == "interrupted"
    assert report["active"] is False
    assert report["next_action"]["command"].startswith("agentcfd run ")


def test_project_status_observes_live_openfoam_progress_without_field_reads(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    run_id = "live-progress"
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": run_id,
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "running",
                "phase": "solver",
                "pid": os.getpid(),
                "started_at": "2026-09-06T00:00:00+00:00",
                "completed_at": None,
            }
        )
    )
    case = project.root / ".agentcfd" / "work" / run_id / "openfoam"
    case.mkdir(parents=True)
    (case / "0.5").mkdir()
    (case / "log.pimpleFoam").write_text(
        """Time = 0.75
Courant Number mean: 0.12 max: 0.48
smoothSolver: Solving for Ux, Initial residual = 2e-4, Final residual = 3e-7, No Iterations 2
GAMG: Solving for p, Initial residual = 8e-4, Final residual = 9e-7, No Iterations 3
"""
    )
    for name, value in {
        "agentcfd_inlet_flow": -0.01,
        "agentcfd_outlet_flow": 0.00999,
        "agentcfd_inlet_pressure": 0.25,
        "agentcfd_outlet_pressure": 0.05,
    }.items():
        monitor = case / "postProcessing" / name / "0"
        monitor.mkdir(parents=True)
        (monitor / "surfaceFieldValue.dat").write_text(
            f"# Time value\n0.5 {value}\n0.75 {value}\n"
        )

    report = project.status(include_storage=True)

    jsonschema.Draft202012Validator(
        contracts.load("project-status.schema.json")
    ).validate(report)
    progress = report["progress"]
    assert report["state"] == "running"
    assert report["next_action"]["command"].startswith("agentcfd watch ")
    assert progress["current_command"] == "pimpleFoam"
    assert progress["coordinate"] == {
        "name": "physical_time",
        "unit": "s",
        "current": 0.75,
        "target": 2.0,
        "fraction": 0.375,
    }
    assert progress["latest_residuals"]["p"]["initial"] == pytest.approx(8e-4)
    assert progress["courant_number"]["maximum"] == pytest.approx(0.48)
    assert progress["monitors"]["relative_mass_imbalance"] == pytest.approx(0.001)
    assert progress["monitors"]["pressure_drop"] == pytest.approx(200.0)
    assert progress["workspace"]["native_time_directory_count"] == 1
    assert progress["workspace"]["bytes"] > 0
    assert progress["observation_cost"]["field_payloads_opened"] == 0
    assert progress["observation_cost"]["monitor_bytes_read"] > 0
    assert progress["estimated_remaining"]["minimum_seconds"] >= 0


def test_project_status_rereads_atomic_completion_before_reporting_interrupted(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    marker = project.run_root / "run.json"
    initial = {
        "schema": "agentcfd.project-run/0.1",
        "run_id": "finishing",
        "mode": "replace",
        "directory": str(project.run_root),
        "status": "exporting",
        "phase": "portable-fields",
        "pid": 12345,
        "analysis_sha256": plan["model"]["analysis_sha256"],
        "started_at": "2026-09-06T00:00:00+00:00",
        "completed_at": None,
    }
    marker.write_text(json.dumps(initial))
    (project.run_root / "result.json").write_text("{}")

    def finish_during_probe(_pid):
        marker.write_text(
            json.dumps(
                {
                    **initial,
                    "status": "completed",
                    "phase": "complete",
                    "pid": None,
                    "accepted": True,
                    "completed_at": "2026-09-06T00:00:01+00:00",
                }
            )
        )
        return False

    monkeypatch.setattr(projects, "_process_is_alive", finish_during_probe)

    report = project.status()

    assert report["state"] == "complete"
    assert report["latest_run"]["phase"] == "complete"
    assert report["progress"] is None


def test_watch_follows_running_project_until_atomic_completion(
    tmp_path, monkeypatch, capsys
):
    project = projects.init_project(tmp_path / "pipe")
    plan = project.plan()
    project.run_root.mkdir()
    marker = project.run_root / "run.json"
    initial = {
        "schema": "agentcfd.project-run/0.1",
        "run_id": "watched",
        "mode": "replace",
        "directory": str(project.run_root),
        "status": "running",
        "phase": "solver",
        "pid": os.getpid(),
        "analysis_sha256": plan["model"]["analysis_sha256"],
        "started_at": "2026-09-06T00:00:00+00:00",
        "completed_at": None,
    }
    marker.write_text(json.dumps(initial))

    def complete(_interval):
        (project.run_root / "result.json").write_text("{}")
        marker.write_text(
            json.dumps(
                {
                    **initial,
                    "status": "completed",
                    "phase": "complete",
                    "pid": None,
                    "accepted": True,
                    "completed_at": "2026-09-06T00:00:01+00:00",
                }
            )
        )

    monkeypatch.setattr("agentcfd.cli.time.sleep", complete)

    assert entrypoint(["watch", str(project.root), "--interval", "0.2", "--json"]) == 0
    snapshots = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [snapshot["state"] for snapshot in snapshots] == ["running", "complete"]
    assert snapshots[0]["next_action"]["command"].startswith("agentcfd watch ")
    assert snapshots[1]["postprocess"]["result"].endswith("output/result.json")


def test_project_status_surfaces_failed_acceptance_checks(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "result.json").write_text("{}")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "review-me",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": False,
                "failed_checks": ["mass-balance"],
                "plan_sha256": project.plan()["plan_sha256"],
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )

    report = project.status()

    assert report["state"] == "review"
    assert "mass-balance" in report["next_action"]["reason"]
    assert report["postprocess"]["primary"].endswith("result.json")


def test_storage_inventory_and_clean_preserve_results(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run()
    workspace = project.root / ".agentcfd" / "work" / "debug-run"
    workspace.mkdir(parents=True)
    (workspace / "native-field").write_bytes(b"temporary")
    mesh_cache = project.root / ".agentcfd" / "mesh-cache" / "identity"
    mesh_cache.mkdir(parents=True)
    (mesh_cache / "points").write_bytes(b"reusable-mesh")

    inventory = project.storage()
    jsonschema.Draft202012Validator(
        contracts.load("project-storage.schema.json")
    ).validate(inventory)
    assert inventory["reclaimable_bytes"] == len(b"temporary")
    assert inventory["categories"]["mesh_cache"]["bytes"] == len(b"reusable-mesh")
    assert inventory["next_action"]["command"].endswith(" --apply")

    preview = project.clean()
    jsonschema.Draft202012Validator(
        contracts.load("project-clean.schema.json")
    ).validate(preview)
    assert preview["applied"] is False
    assert (workspace / "native-field").is_file()
    applied = project.clean(apply=True)
    assert applied["reclaimed_bytes"] == len(b"temporary")
    assert not workspace.exists()
    assert (mesh_cache / "points").is_file()
    assert (project.run_root / "result.json").is_file()

    cache_preview = project.clean(include_cache=True)
    assert cache_preview["include_cache"] is True
    assert cache_preview["candidate_bytes"] == len(b"reusable-mesh")
    assert (mesh_cache / "points").is_file()
    cache_cleanup = project.clean(apply=True, include_cache=True)
    assert cache_cleanup["reclaimed_bytes"] == len(b"reusable-mesh")
    assert not mesh_cache.exists()
    assert (project.run_root / "result.json").is_file()


def test_clean_protects_live_run_workspace(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run_root.mkdir()
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "live-run",
                "status": "running",
                "pid": os.getpid(),
            }
        )
    )
    live = project.root / ".agentcfd" / "work" / "live-run"
    stale = project.root / ".agentcfd" / "work" / "stale-run"
    live.mkdir(parents=True)
    stale.mkdir()
    (live / "state").write_bytes(b"live")
    (stale / "state").write_bytes(b"stale")

    inventory = project.storage()
    assert inventory["reclaimable_bytes"] == len(b"stale")
    report = project.clean(apply=True)

    assert report["protected_active_run_ids"] == ["live-run"]
    assert live.is_dir()
    assert not stale.exists()
    assert report["reclaimed_bytes"] == len(b"stale")


def test_clean_requires_explicit_scope_to_remove_retained_workspace(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    retained = project.root / ".agentcfd" / "work" / "retained-run"
    retained.mkdir(parents=True)
    (retained / "native-field").write_bytes(b"retained")
    (retained / ".agentcfd-workspace.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.workspace/0.1",
                "run_id": "retained-run",
                "retention_reason": "explicit-cli",
                "protected": True,
            }
        )
    )

    inventory = project.storage()
    assert inventory["reclaimable_bytes"] == 0
    assert inventory["categories"]["temporary_workspaces"][
        "protected_retained_run_ids"
    ] == ["retained-run"]
    protected = project.clean(apply=True)
    assert protected["protected_retained_run_ids"] == ["retained-run"]
    assert retained.is_dir()

    preview = project.clean(include_retained=True)
    assert preview["candidate_bytes"] > 0
    removed = project.clean(apply=True, include_retained=True)
    assert removed["reclaimed_bytes"] > 0
    assert not retained.exists()


def test_status_storage_clean_and_view_cli_are_human_and_agent_friendly(
    tmp_path, capsys
):
    root = tmp_path / "pipe"
    projects.init_project(root).run()

    assert entrypoint(["status", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"
    assert entrypoint(["storage", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["managed_bytes"] > 0
    assert entrypoint(["clean", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert entrypoint(["view", str(root), "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["kind"] == "result-json"
    jsonschema.Draft202012Validator(
        contracts.load("project-view.schema.json")
    ).validate(view)


def test_status_summarizes_xdmf_without_loading_field_payload(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run()
    fields = project.run_root / "fields"
    fields.mkdir()
    (fields / "fields.xdmf").write_text("<Xdmf/>")
    (fields / "manifest.json").write_text(
        json.dumps(
            {
                "axis": {
                    "name": "time",
                    "unit": "s",
                    "physical_time": True,
                    "values": [0.1, 0.2],
                },
                "fields": [
                    {
                        "name": "fluid.velocity",
                        "export_name": "fluid.velocity.point",
                        "association": "point",
                        "unit": "m/s",
                        "components": ["x", "y", "z"],
                    }
                ],
                "output_selection": {"profile": "visualization"},
                "storage": {"actual_portable_bytes": 1024},
            }
        )
    )

    summary = project.status()["postprocess"]["field_summary"]

    assert summary["frame_count"] == 2
    assert summary["axis"]["last"] == 0.2
    assert summary["fields"][0]["association"] == "point"
    assert summary["portable_display"] == "1.00 KiB"


def test_json_mode_returns_structured_repairable_error(tmp_path, capsys):
    missing = tmp_path / "missing"

    assert entrypoint(["status", str(missing), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(contracts.load("error.schema.json")).validate(
        payload
    )
    assert payload["schema"] == "agentcfd.error/0.1"
    assert payload["error"]["code"] == "FILE_NOT_FOUND_ERROR"
    assert payload["error"]["repair"]
