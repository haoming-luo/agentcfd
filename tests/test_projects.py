import json
import hashlib
import math
import os
import shlex
import shutil
import threading
import time
import zipfile
from pathlib import Path

import jsonschema
import pytest

from agentcfd import (
    Artifact,
    Check,
    FieldRecord,
    contracts,
    geometry_io,
    interoperability,
    open_scientific_dataset,
    projects,
)
from agentcfd.cli import _watch_summary, entrypoint
from agentcfd.errors import ProjectError


def _mock_openfoam_runtime(monkeypatch):
    """Keep project-lifecycle unit tests independent of host Docker installs."""

    monkeypatch.setattr(projects.shutil, "which", lambda _command: "/mock/runtime")


def test_process_liveness_treats_permission_denied_as_existing(monkeypatch):
    def denied(_pid, _signal):
        raise PermissionError("managed process boundary")

    monkeypatch.setattr(projects.os, "kill", denied)

    assert projects._posix_process_is_alive(12345) is True


def test_process_liveness_dispatches_to_windows_without_os_kill(monkeypatch):
    observed = []

    def missing(pid):
        observed.append(pid)
        return False

    monkeypatch.setattr(projects.os, "name", "nt")
    monkeypatch.setattr(projects, "_windows_process_is_alive", missing)

    assert projects._process_is_alive(12345) is False
    assert observed == [12345]


def test_project_lifecycle_is_one_readable_agent_and_human_workflow(tmp_path):
    root = tmp_path / "pipe"
    project = projects.init_project(root)

    assert project.manifest.default_provider == "reference"
    assert project.manifest.template == "industrial-pipe"
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
    assert plan["decisions"]["thermal_preflight"]["status"] == "not-requested"
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


def test_project_plan_exposes_thermal_first_law_preflight(tmp_path):
    project = projects.init_project(tmp_path / "heated-pipe", provider="openfoam")
    project.entrypoint.write_text(
        """from agentcfd import Model, boundaries, fluids, geometry, outputs, studies

def build():
    model = Model(
        study=studies.internal_flow(energy=True),
        domain=geometry.circular_pipe(length=2.0, diameter=0.1),
        fluid=fluids.newtonian(
            \"water\", density=1000.0, dynamic_viscosity=0.001,
            specific_heat=4000.0, thermal_conductivity=0.6,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.01, temperature=300.0),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(
            thermal=boundaries.heat_flux_into_fluid(100.0)
        ),
    )
    return model.step(output=outputs.thermal_internal_flow())
""",
        encoding="utf-8",
    )

    plan = projects.Project(project.root).plan()
    preflight = plan["decisions"]["thermal_preflight"]
    expected_heat = 100.0 * math.pi * 0.1 * 2.0
    expected_flow = 1000.0 * 0.01 * math.pi * 0.1**2 / 4.0
    assert preflight["status"] == "calculated"
    assert preflight["calculation"]["heat_rate_into_fluid"] == pytest.approx(
        expected_heat
    )
    assert preflight["calculation"]["mass_flow_rate"] == pytest.approx(expected_flow)
    assert preflight["calculation"][
        "estimated_bulk_temperature_change"
    ] == pytest.approx(expected_heat / (expected_flow * 4000.0))
    jsonschema.Draft202012Validator(
        contracts.load("solution-plan.schema.json")
    ).validate(plan)


def test_heated_pipe_template_is_ready_and_exposes_editable_operating_point(
    tmp_path,
    capsys,
):
    root = tmp_path / "heated-template"
    assert entrypoint(["init", str(root), "--template", "heated-pipe", "--json"]) == 0
    initialization = json.loads(capsys.readouterr().out)
    project = projects.Project(root)
    plan = project.plan()
    parameters = {item["name"]: item for item in project.parameter_contract()}

    assert initialization["provider"] == "openfoam"
    assert project.manifest.openfoam["cross_section_cells"] == 16
    assert project.manifest.openfoam["axial_cells"] == 80
    assert plan["readiness"]["provider_compatible"] is True
    assert plan["decisions"]["required_capability"] == (
        "openfoam.steady-laminar-heated-circular-pipe"
    )
    assert plan["decisions"]["solver"] == "simpleFoam + scalarTransport(T)"
    assert plan["decisions"]["thermal_preflight"]["status"] == "calculated"
    assert "thermal.temperature" in plan["decisions"]["outputs"]["fields"]
    assert "thermal.energy_balance" in plan["decisions"]["outputs"]["histories"]
    assert parameters["wall_heat_flux"]["metadata"]["unit"] == "W/m^2"
    assert "not a steam" in (root / "README.md").read_text()


def test_imported_internal_flow_init_owns_inputs_and_is_ready_to_plan(tmp_path):
    original = (
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
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
    inspection = json.loads((project.root / "geometry/inspection.json").read_text())
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


def test_imported_init_requires_explicit_multiple_component_review(tmp_path):
    def solid(name, triangles):
        lines = [f"solid {name}"]
        for triangle in triangles:
            lines.extend(("facet normal 0 0 0", "outer loop"))
            lines.extend(
                f"vertex {point[0]} {point[1]} {point[2]}" for point in triangle
            )
            lines.extend(("endloop", "endfacet"))
        lines.append(f"endsolid {name}")
        return "\n".join(lines) + "\n"

    a = (0.0, 0.0, 0.0)
    b = (1.0, 0.0, 0.0)
    c = (0.0, 1.0, 0.0)
    d = (0.0, 0.0, 1.0)
    shift = lambda point: (point[0] + 3.0, point[1], point[2])
    source = tmp_path / "equipment.stl"
    source.write_text(
        solid("inlet", ((a, c, b),))
        + solid("outlet", ((a, b, d),))
        + solid("walls", ((a, d, c), (b, c, d)))
        + solid(
            "baffle",
            tuple(
                tuple(shift(point) for point in triangle)
                for triangle in ((a, c, b), (a, b, d), (a, d, c), (b, c, d))
            ),
        ),
        encoding="utf-8",
    )
    common = {
        "provider": "openfoam",
        "template": "imported-internal-flow",
        "geometry_path": source,
        "geometry_unit": "m",
        "boundary_roles": {
            "inlet": "inlet",
            "outlet": "outlet",
            "walls": "wall",
            "baffle": "wall",
        },
        "interior_point_m": (0.1, 0.1, 0.1),
        "inlet_velocity_m_s": (0.0, 0.0, 0.5),
        "base_size_m": 0.1,
        "maximum_cells": 100_000,
    }

    blocked_root = tmp_path / "blocked"
    with pytest.raises(ProjectError, match="explicit multiple-component acceptance"):
        projects.init_project(blocked_root, **common)
    assert not blocked_root.exists()

    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "imported-internal-flow",
        "provider": "openfoam",
        "geometry": {
            "path": str(source),
            "unit": "m",
            "boundary_roles": common["boundary_roles"],
            "accept_multiple_components": True,
        },
        "interior_point_m": list(common["interior_point_m"]),
        "inlet_velocity_m_s": list(common["inlet_velocity_m_s"]),
        "mesh": {
            "base_size_m": common["base_size_m"],
            "maximum_cells": common["maximum_cells"],
        },
    }
    jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    ).validate(request)
    project = projects.init_project_from_request(tmp_path / "accepted", request)
    inspection = json.loads((project.root / "geometry/inspection.json").read_text())
    domain = project.load_step().model.domain
    assert inspection["policy"]["accept_multiple_components"] is True
    assert domain.connected_component_count == 2
    assert domain.multiple_components_accepted is True


def test_imported_internal_flow_init_fails_before_writing_unsupported_intent(
    tmp_path,
):
    source = (
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
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
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
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
                "--role",
                "inlet=inlet",
                "--role",
                "outlet=outlet",
                "--role",
                "walls=wall",
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
    project = projects.Project(root)
    assert project.plan()["readiness"]["provider_compatible"] is True
    assert project.load_step().output.reports[0].to_dict() == {
        "type": "pressure-loss-report",
        "name": "system-loss",
        "inlet": "inlet",
        "outlet": "outlet",
        "reference_area": None,
        "averaging": "mass-flow",
        "every": 1,
    }
    assert project.load_step().output.reports[1].to_dict() == {
        "type": "flow-uniformity-report",
        "name": "outlet-quality",
        "region": "outlet",
        "every": 1,
    }
    mass_flow_plan = project.plan(parameters={"mass_flow_rate": 49.91})
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
    turbulent_plan = project.plan(parameters=turbulent_parameters)
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


def test_imported_project_initializes_multi_outlet_decision_reports(tmp_path):
    original = (
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
    ).read_text()
    old_outlet = """solid outlet
  facet normal 1 0 0
    outer loop
      vertex 1 0 0
      vertex 1 0.5 0
      vertex 1 0.5 0.2
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 1 0 0
      vertex 1 0.5 0.2
      vertex 1 0 0.2
    endloop
  endfacet
endsolid outlet
"""
    split_outlet = """solid branch_a
  facet normal 1 0 0
    outer loop
      vertex 1 0 0
      vertex 1 0.5 0
      vertex 1 0.5 0.2
    endloop
  endfacet
endsolid branch_a
solid branch_b
  facet normal 1 0 0
    outer loop
      vertex 1 0 0
      vertex 1 0.5 0.2
      vertex 1 0 0.2
    endloop
  endfacet
endsolid branch_b
"""
    assert old_outlet in original
    source = tmp_path / "split-duct.stl"
    source.write_text(original.replace(old_outlet, split_outlet))
    root = tmp_path / "multi-outlet-project"

    project = projects.init_project(
        root,
        provider="openfoam",
        template="imported-internal-flow",
        geometry_path=source,
        geometry_unit="m",
        boundary_roles={
            "inlet": "inlet",
            "branch_a": "outlet",
            "branch_b": "outlet",
            "walls": "wall",
        },
        interior_point_m=(0.5, 0.25, 0.1),
        inlet_velocity_m_s=(0.5, 0.0, 0.0),
        base_size_m=0.05,
        maximum_cells=200_000,
    )

    step = project.load_step()
    assert project.plan()["readiness"]["provider_compatible"] is True
    assert step.output.reports[0].to_dict() == {
        "type": "flow-distribution-report",
        "name": "flow-split",
        "inlet": "inlet",
        "outlets": ["branch_a", "branch_b"],
        "target_fractions": {},
        "every": 1,
    }
    assert [report.name for report in step.output.reports] == ["flow-split"]


def test_cli_materializes_multi_outlet_targets_and_design_limit(tmp_path, capsys):
    example = Path(__file__).parents[1] / "examples/imported_split_duct_geometry"
    root = tmp_path / "targeted-split"

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
                "--outlet-target",
                "branch_b=0.6",
                "--outlet-target",
                "branch_a=0.4",
                "--maximum-fraction-error",
                "0.02",
                "--json",
            ]
        )
        == 0
    )
    json.loads(capsys.readouterr().out)

    step = projects.Project(root).load_step()
    report = step.output.reports[0]
    assert report.to_dict()["target_fractions"] == {
        "branch_a": 0.4,
        "branch_b": 0.6,
    }
    assert [criterion.to_dict() for criterion in step.output.criteria] == [
        {
            "type": "quantity-criterion",
            "name": "flow-split-target",
            "quantity": "report.flow-split.maximum_fraction_error",
            "unit": "1",
            "minimum": None,
            "maximum": 0.02,
        }
    ]
    project_source = (root / "case.py").read_text()
    assert "targets={'branch_a': 0.4, 'branch_b': 0.6}" in project_source
    assert "maximum=0.02" in project_source


def test_flow_targets_fail_closed_before_project_creation(tmp_path):
    example = Path(__file__).parents[1] / "examples/imported_split_duct_geometry"
    common = {
        "provider": "openfoam",
        "template": "imported-internal-flow",
        "geometry_path": example / "fluid.stl",
        "geometry_unit": "m",
        "boundary_roles": {
            "inlet": "inlet",
            "branch_a": "outlet",
            "branch_b": "outlet",
            "walls": "wall",
        },
        "interior_point_m": (0.5, 0.25, 0.1),
        "inlet_velocity_m_s": (0.5, 0.0, 0.0),
        "base_size_m": 0.05,
        "maximum_cells": 200_000,
    }
    partial_root = tmp_path / "partial-targets"
    with pytest.raises(ValueError, match="every declared outlet"):
        projects.init_project(
            partial_root,
            **common,
            outlet_target_fractions={"branch_a": 1.0},
        )
    assert not partial_root.exists()

    missing_target_root = tmp_path / "limit-without-targets"
    with pytest.raises(ValueError, match="requires complete outlet target"):
        projects.init_project(
            missing_target_root,
            **common,
            maximum_fraction_error=0.01,
        )
    assert not missing_target_root.exists()


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
    tmp_path, capsys, monkeypatch
):
    _mock_openfoam_runtime(monkeypatch)
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
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
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
    assert json.loads((root / "geometry/boundary-roles.json").read_text())[
        "regions"
    ] == {
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
    tmp_path, capsys, monkeypatch
):
    _mock_openfoam_runtime(monkeypatch)
    source = (
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
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
        entrypoint(["init", str(root), "--request", str(request_path), "--json"]) == 0
    )

    report = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-initialization.schema.json")
    ).validate(report)
    assert report["request_sha256"] == projects.content_fingerprint(request)
    assert (root / "geometry/fluid.stl").read_bytes() == source.read_bytes()
    assert projects.Project(root).plan()["readiness"]["ready_to_run"] is True


def test_creation_request_can_confirm_unambiguous_name_roles(tmp_path):
    source = (
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
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

    assert json.loads((project.root / "geometry/boundary-roles.json").read_text())[
        "regions"
    ] == {"inlet": "inlet", "outlet": "outlet", "walls": "wall"}


def test_creation_request_materializes_multi_outlet_decision_intent(tmp_path):
    example = Path(__file__).parents[1] / "examples/imported_split_duct_geometry"
    request = json.loads((example / "project-request.json").read_text())
    jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    ).validate(request)

    project = projects.init_project_from_request(
        tmp_path / "request-split",
        request,
        base_directory=example,
    )
    step = project.load_step()

    assert step.output.reports[0].to_dict()["target_fractions"] == {
        "branch_a": 0.5,
        "branch_b": 0.5,
    }
    assert step.output.criteria[0].maximum == 0.001


def test_creation_request_materializes_heated_pipe_defaults(tmp_path):
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "heated-pipe",
        "provider": "openfoam",
        "parameters": {
            "length": 1.25,
            "diameter": 0.2,
            "mean_velocity": 0.015,
            "inlet_temperature": 310.0,
            "wall_heat_flux": -75.0,
        },
    }
    validator = jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    )
    validator.validate(request)

    project = projects.init_project_from_request(tmp_path / "heated-request", request)
    step = project.load_step()

    assert step.model.domain.length == pytest.approx(1.25)
    assert step.model.domain.diameter == pytest.approx(0.2)
    assert step.model.boundary_conditions["inlet"].velocity == pytest.approx(0.015)
    assert step.model.boundary_conditions["inlet"].temperature == pytest.approx(310.0)
    assert step.model.boundary_conditions[
        "wall"
    ].thermal.heat_flux_into_fluid == pytest.approx(-75.0)
    assert project.plan()["decisions"]["thermal_preflight"]["status"] == "calculated"
    assert "length=1.25" in project.entrypoint.read_text()


def test_creation_request_rejects_invalid_heated_pipe_defaults_before_writing(
    tmp_path,
):
    request = {
        "schema": "agentcfd.project-creation-request/0.1",
        "template": "heated-pipe",
        "provider": "openfoam",
        "parameters": {"wall_heat_flux": 0.0},
    }
    validator = jsonschema.Draft202012Validator(
        contracts.load("project-creation-request.schema.json")
    )
    assert list(validator.iter_errors(request))

    root = tmp_path / "invalid-heated-request"
    with pytest.raises(ProjectError, match="wall_heat_flux must be non-zero"):
        projects.init_project_from_request(root, request)
    assert not root.exists()


def test_creation_request_accepts_mass_flow_and_rejects_ambiguous_controls(tmp_path):
    source = (
        Path(__file__).parents[1] / "examples/imported_duct_mesh/geometry/fluid.stl"
    )
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
    assert pressure_project.load_step().model.boundary_conditions[
        "inlet"
    ].to_dict() == {
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


def test_project_publishes_final_run_record_atomically(tmp_path, monkeypatch):
    project = projects.init_project(tmp_path / "pipe")
    original = projects._write_json_atomic
    atomic_paths = []

    def observe(path, payload):
        atomic_paths.append(Path(path))
        original(path, payload)

    monkeypatch.setattr(projects, "_write_json_atomic", observe)

    completed = project.run()

    run_path = completed.directory / "run.json"
    assert run_path in atomic_paths
    assert not run_path.with_suffix(".json.tmp").exists()
    assert json.loads(run_path.read_text())["status"] == "completed"


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

    monkeypatch.setattr("agentcfd.projects.ReferencePipeProvider.run", wrong_identity)

    with pytest.raises(ProjectError, match="different analysis identity"):
        project.run()

    record = json.loads((project.run_root / "run.json").read_text())
    assert record["status"] == "failed"
    assert not (project.run_root / "result.json").exists()


def test_failed_openfoam_result_retains_workspace_and_guides_to_logs(
    tmp_path, monkeypatch
):
    _mock_openfoam_runtime(monkeypatch)
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    observed = {}

    def fail(provider, _step, **kwargs):
        observed["checkpoint_archive"] = kwargs["checkpoint_archive"]
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
    assert observed["checkpoint_archive"] == project.run_root / "evidence" / "restart.zip"
    assert status["state"] == "failed"
    assert status["next_action"]["command"].startswith("agentcfd diagnose ")

    diagnosis = project.diagnose()
    jsonschema.Draft202012Validator(
        contracts.load("project-diagnosis.schema.json")
    ).validate(diagnosis)
    assert diagnosis["primary_finding"]["code"] == "OPENFOAM_FATAL_ERROR"
    assert diagnosis["observation_cost"]["field_payloads_opened"] == 0
    assert diagnosis["next_action"]["command"].startswith("agentcfd logs ")


def test_successful_project_preserves_bounded_field_conversion_log(
    tmp_path, monkeypatch
):
    _mock_openfoam_runtime(monkeypatch)
    monkeypatch.setattr("agentcfd.projects.data_exchange.io_available", lambda: True)
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    observed_progress = {}

    def complete(provider, _step, **_kwargs):
        provider.case_directory.mkdir(parents=True)
        return projects.SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={},
            checks=(Check("execution", True, kind="runtime"),),
        )

    def export(case_directory, output_directory, **_kwargs):
        case_directory = Path(case_directory)
        output_directory = Path(output_directory)
        output_directory.mkdir(parents=True)
        xdmf = output_directory / "fields.xdmf"
        hdf5 = output_directory / "fields.h5"
        manifest = output_directory / "manifest.json"
        xdmf.write_text("<Xdmf/>\n")
        hdf5.write_bytes(b"portable-fields")
        manifest.write_text(json.dumps({"fields": []}) + "\n")
        (case_directory / "log.foamToVTK").write_text(
            "=== native times 0.5,1 ===\nconverted\n"
        )
        _kwargs["_progress_callback"](
            {
                "schema": "agentcfd.field-export-progress/0.1",
                "phase": "writing",
                "completed_frames": 0,
                "total_frames": 2,
                "fraction": 0.0,
                "batch_index": 1,
                "batch_count": 1,
                "current_batch_frames": 2,
                "maximum_batch_frames": 4,
            }
        )
        observed_progress.update(
            json.loads((project.run_root / "run.json").read_text())["field_export"]
        )
        return projects.data_exchange.FieldBundle(
            directory=output_directory,
            xdmf=xdmf,
            hdf5=hdf5,
            npz=None,
            manifest=manifest,
            frame_count=2,
            times=(0.5, 1.0),
        )

    def recipes(run_directory, *_args, **_kwargs):
        manifest = Path(run_directory) / "postprocess" / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text("{}\n")
        return manifest, ()

    monkeypatch.setattr("agentcfd.projects.OpenFOAMChannelProvider.run", complete)
    monkeypatch.setattr("agentcfd.projects.data_exchange.export_openfoam_case", export)
    monkeypatch.setattr(
        "agentcfd.projects.postprocessing.publish_paraview_recipes",
        recipes,
    )

    completed = project.run()

    published = project.run_root / "evidence" / "foamToVTK.log"
    assert published.read_text().endswith("converted\n")
    artifact = completed.result.artifacts["log_foamToVTK"]
    assert Path(artifact.path) == published
    assert artifact.role == "field-conversion-log"
    assert completed.solver_workspace is None
    assert observed_progress["completed_frames"] == 0
    assert observed_progress["updated_at"].endswith("+00:00")
    log_report = project.logs(command="foamToVTK", lines=1)
    assert log_report["source"] == "published-evidence"
    assert log_report["tail"] == "converted\n"


def test_keep_workspace_persists_cleanup_protection_from_real_run_path(
    tmp_path, monkeypatch
):
    _mock_openfoam_runtime(monkeypatch)
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    project.manifest_path.write_text(
        project.manifest_path.read_text().replace(
            "export_fields = true", "export_fields = false"
        )
    )
    project = projects.Project(project.root)

    def complete(provider, _step, **_kwargs):
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
    _mock_openfoam_runtime(monkeypatch)
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )

    def complete(provider, _step, **_kwargs):
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
    assert entrypoint(["plan", str(project.root), "--summary-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decisions"]["result_profile"] == (
        "summary-only"
    )
    assert entrypoint(["run", str(project.root), "--summary-only", "--json"]) == 0
    capsys.readouterr()
    single_marker = json.loads((project.root / "output" / "run.json").read_text())
    assert single_marker["result_profile"] == "summary-only"
    assert not (project.root / "output" / "fields.h5").exists()
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
    _mock_openfoam_runtime(monkeypatch)
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


def test_project_resume_refuses_changed_analysis(tmp_path, monkeypatch):
    _mock_openfoam_runtime(monkeypatch)
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


def test_params_command_exports_a_complete_validated_operating_point(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    output = tmp_path / "operating-points" / "gentle-flow.json"

    assert (
        entrypoint(
            [
                "params",
                str(project.root),
                "--param",
                "mean_velocity=0.015",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("parameter-set.schema.json")
    ).validate(payload)
    assert json.loads(output.read_text()) == payload
    assert payload["parameters"] == {
        "length": 10.0,
        "diameter": 0.05,
        "mean_velocity": 0.015,
    }
    assert (
        project.plan(parameters=payload["parameters"])["readiness"]["ready_to_run"]
        is True
    )

    assert entrypoint(["params", str(project.root)]) == 0
    human = capsys.readouterr().out
    assert "Validated operating point" in human
    assert "mean_velocity=0.02 m/s" in human

    assert (
        entrypoint(["params", str(project.root), "--output", str(output), "--json"])
        == 2
    )
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["type"] == "FileExistsError"


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


def test_campaign_dataset_is_verified_atomic_and_records_exclusions(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    case = project.entrypoint
    case.write_text(
        case.read_text(encoding="utf-8").replace(
            "output=outputs.standard(),",
            """output=outputs.standard(
            criteria=(
                outputs.require(
                    "pressure-budget",
                    quantity="flow.pressure_drop",
                    unit="Pa",
                    maximum=4.0,
                ),
            ),
        ),""",
        ),
        encoding="utf-8",
    )
    low = project.run(campaign=True, parameters={"mean_velocity": 0.01})
    medium = project.run(campaign=True, parameters={"mean_velocity": 0.03})
    high = project.run(campaign=True, parameters={"mean_velocity": 0.04})
    assert low.result.accepted is True
    assert medium.result.accepted is True
    assert high.result.accepted is False

    target = tmp_path / "datasets" / "pressure-map"
    output, manifest = project.export_campaign_dataset(
        target,
        inputs=("diameter", "mean_velocity"),
        outputs=("flow.pressure_drop",),
    )

    assert output == target
    jsonschema.Draft202012Validator(
        contracts.load("scientific-dataset.schema.json")
    ).validate(manifest)
    assert "scientific-dataset.schema.json" in contracts.available()
    assert json.loads((target / "manifest.json").read_text()) == manifest
    assert manifest["sample_count"] == 2
    assert manifest["excluded_count"] == 1
    assert manifest["excluded"][0]["run_id"] == high.run_id
    assert manifest["samples"]["sha256"] == hashlib.sha256(
        (target / "samples.jsonl").read_bytes()
    ).hexdigest()
    samples = [
        json.loads(line)
        for line in (target / "samples.jsonl").read_text().splitlines()
    ]
    assert [sample["case_id"] for sample in samples] == [
        f"agentcfd-{low.run_id}",
        f"agentcfd-{medium.run_id}",
    ]
    assert [sample["inputs"]["mean_velocity"] for sample in samples] == [
        0.01,
        0.03,
    ]
    for sample in samples:
        jsonschema.Draft202012Validator(
            contracts.load("scientific-sample.schema.json")
        ).validate(sample)

    verification = interoperability.verify_scientific_dataset(target)
    jsonschema.Draft202012Validator(
        contracts.load("scientific-dataset-verification.schema.json")
    ).validate(verification)
    assert verification["verified"] is True
    assert verification["sample_count"] == 2
    assert "scientific-dataset-verification.schema.json" in contracts.available()
    assert entrypoint(["verify", "dataset", str(target), "--json"]) == 0
    cli_verification = json.loads(capsys.readouterr().out)
    assert cli_verification["verified"] is True

    dataset = open_scientific_dataset(target)
    assert dataset.input_names == ("diameter", "mean_velocity")
    assert dataset.output_names == ("flow.pressure_drop",)
    assert dataset.sample_count == 2
    x_rows, y_rows = dataset.matrices()
    assert x_rows == ((0.05, 0.01), (0.05, 0.03))
    assert y_rows[0][0] == pytest.approx(1.28256)
    x_array, y_array = dataset.to_numpy()
    assert x_array.shape == (2, 2)
    assert y_array.shape == (2, 1)
    inspection = dataset.inspect(preview=1)
    jsonschema.Draft202012Validator(
        contracts.load("scientific-dataset-inspection.schema.json")
    ).validate(inspection)
    assert inspection["matrix_shapes"] == {"X": [2, 2], "Y": [2, 1]}
    assert inspection["inputs"][0]["constant"] is True
    assert inspection["inputs"][1]["minimum"] == 0.01
    assert inspection["inputs"][1]["maximum"] == 0.03
    assert inspection["outputs"][0]["unit"] == "Pa"
    assert inspection["preview"][0]["case_id"] == f"agentcfd-{low.run_id}"
    assert "scientific-dataset-inspection.schema.json" in contracts.available()
    assert (
        entrypoint(
            ["dataset", "inspect", str(target), "--preview", "1", "--json"]
        )
        == 0
    )
    cli_inspection = json.loads(capsys.readouterr().out)
    assert cli_inspection["verified"] is True
    assert len(cli_inspection["preview"]) == 1

    training_plan = dataset.training_plan(validation_fraction=0.5, seed=17)
    jsonschema.Draft202012Validator(
        contracts.load("training-plan.schema.json")
    ).validate(training_plan)
    assert training_plan == dataset.training_plan(validation_fraction=0.5, seed=17)
    assert training_plan["source"]["samples_sha256"] == manifest["samples"]["sha256"]
    assert training_plan["split"]["train_count"] == 1
    assert training_plan["split"]["validation_count"] == 1
    split_ids = (
        training_plan["split"]["train_case_ids"]
        + training_plan["split"]["validation_case_ids"]
    )
    assert set(split_ids) == {f"agentcfd-{low.run_id}", f"agentcfd-{medium.run_id}"}
    assert training_plan["normalization"]["inputs"][0]["constant"] is True
    assert training_plan["normalization"]["inputs"][0]["scale"] == 1.0
    assert training_plan["normalization"]["inputs"][1]["offset"] == pytest.approx(0.02)
    assert training_plan["normalization"]["inputs"][1]["scale"] == pytest.approx(0.01)
    assert "training-plan.schema.json" in contracts.available()
    plan_path = tmp_path / "training-plan.json"
    assert (
        entrypoint(
            [
                "dataset",
                "plan",
                str(target),
                "--validation-fraction",
                "0.5",
                "--seed",
                "17",
                "--output",
                str(plan_path),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == training_plan
    assert json.loads(plan_path.read_text()) == training_plan
    with pytest.raises(ValueError, match="strictly between"):
        dataset.training_plan(validation_fraction=1.0)

    with pytest.raises(ProjectError, match="already exists"):
        project.export_campaign_dataset(
            target,
            outputs=("flow.pressure_drop",),
        )

    cli_target = tmp_path / "datasets" / "pressure-map-cli"
    assert (
        entrypoint(
            [
                "export",
                "dataset",
                str(project.root),
                str(cli_target),
                "--input",
                "mean_velocity",
                "--output-quantity",
                "flow.pressure_drop",
                "--json",
            ]
        )
        == 0
    )
    cli_manifest = json.loads(capsys.readouterr().out)
    assert cli_manifest["sample_count"] == 2
    assert (cli_target / "samples.jsonl").is_file()

    with (target / "samples.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    tampered = interoperability.verify_scientific_dataset(target)
    assert tampered["verified"] is False
    assert tampered["checks"][1]["code"] == "SAMPLE_PAYLOAD_INTEGRITY"
    assert tampered["checks"][1]["passed"] is False
    with pytest.raises(ValueError, match="changed after it was opened"):
        dataset.matrices()
    with pytest.raises(ValueError, match="Scientific dataset is not verified"):
        open_scientific_dataset(target)


def test_training_plan_detects_repeated_float_as_constant(tmp_path):
    samples = [
        {
            "case_id": f"case-{index}",
            "inputs": {"diameter": 0.05, "velocity": velocity},
            "outputs": {"pressure": pressure},
        }
        for index, (velocity, pressure) in enumerate(
            ((0.01, 1.0), (0.02, 2.0), (0.03, 3.0)), start=1
        )
    ]
    samples_path = tmp_path / "samples.jsonl"
    samples_path.write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples),
        encoding="utf-8",
    )
    manifest = {
        "schema": "agentcfd.scientific-dataset/0.1",
        "sample_schema": "agentcae.scientific-sample/0.1.0",
        "sample_count": 3,
        "inputs": [
            {"name": "diameter", "metadata": {"unit": "m"}},
            {"name": "velocity", "metadata": {"unit": "m/s"}},
        ],
        "outputs": [{"name": "pressure", "unit": "Pa"}],
        "samples": {
            "path": "samples.jsonl",
            "sha256": hashlib.sha256(samples_path.read_bytes()).hexdigest(),
        },
    }
    reader = interoperability.ScientificDatasetReader(tmp_path, manifest, {})

    plan = reader.training_plan(validation_fraction=1 / 3, seed=17)

    diameter = plan["normalization"]["inputs"][0]
    assert diameter["constant"] is True
    assert diameter["offset"] == 0.05
    assert diameter["scale"] == 1.0


def test_campaign_operating_map_is_unit_aware_accepted_and_field_free(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    project.run(
        campaign=True,
        parameters={"mean_velocity": 0.01},
        design_point_name="low-flow",
    )
    project.run(
        campaign=True,
        parameters={"mean_velocity": 0.03},
        design_point_name="high-flow",
    )
    original_case = project.entrypoint.read_text(encoding="utf-8")
    project.entrypoint.write_text(
        "raise RuntimeError('campaign map must not import current case.py')\n"
        + original_case,
        encoding="utf-8",
    )

    report = project.campaign_operating_map(
        x_parameter="mean_velocity",
        y_quantity="flow.pressure_drop",
    )
    jsonschema.Draft202012Validator(
        contracts.load("campaign-operating-map.schema.json")
    ).validate(report)
    assert report["x_axis"] == {
        "source": "project-parameter",
        "name": "mean_velocity",
        "label": "Mean inlet velocity",
        "unit": "m/s",
    }
    assert report["y_axis"]["unit"] == "Pa"
    assert report["y_axis"]["label"] == "Pressure drop"
    assert [point["x"] for point in report["points"]] == [0.01, 0.03]
    assert report["accepted_count"] == 2
    assert report["connected_accepted_curve"] is True
    assert report["observation_cost"]["plan_files_opened"] == 2
    assert report["observation_cost"]["result_manifests_opened"] == 0
    assert report["observation_cost"]["field_payloads_opened"] == 0
    assert report["artifact_integrity"]["verified"] is False
    assert report["artifact"] is None

    plot = tmp_path / "pressure-loss-map.svg"
    _, rendered = project.export_campaign_operating_map(
        plot,
        x_parameter="mean_velocity",
        y_quantity="flow.pressure_drop",
        title="Pipe operating map",
    )
    jsonschema.Draft202012Validator(
        contracts.load("campaign-operating-map.schema.json")
    ).validate(rendered)
    svg = plot.read_text(encoding="utf-8")
    assert "Pipe operating map" in svg
    assert "Mean inlet velocity [m/s]" in svg
    assert "Pressure drop [Pa]" in svg
    assert "<polyline" in svg
    assert rendered["artifact"]["bytes"] == plot.stat().st_size
    assert len(rendered["artifact"]["sha256"]) == 64

    cli_plot = tmp_path / "cli-map.svg"
    assert (
        entrypoint(
            [
                "campaigns",
                str(project.root),
                "--plot-svg",
                str(cli_plot),
                "--x-parameter",
                "mean_velocity",
                "--y-quantity",
                "flow.pressure_drop",
                "--json",
            ]
        )
        == 0
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["artifact"]["path"] == str(cli_plot)
    assert cli_report["observation_cost"]["field_payloads_opened"] == 0

    first_marker = sorted((project.root / "campaigns").glob("*/run.json"))[0]
    marker_record = json.loads(first_marker.read_text(encoding="utf-8"))
    marker_record["accepted"] = False
    first_marker.write_text(json.dumps(marker_record), encoding="utf-8")
    filtered = project.campaign_operating_map(
        x_parameter="mean_velocity",
        y_quantity="flow.pressure_drop",
    )
    assert filtered["point_count"] == 1
    assert filtered["exclusions"][0]["reason"] == "result-not-accepted"
    inclusive = project.campaign_operating_map(
        x_parameter="mean_velocity",
        y_quantity="flow.pressure_drop",
        accepted_only=False,
    )
    assert inclusive["point_count"] == 2
    assert inclusive["accepted_count"] == 1
    assert inclusive["connected_accepted_curve"] is False
    assert inclusive["warnings"]


def test_campaign_operating_map_rejects_incomplete_or_ambiguous_axes(tmp_path):
    project = projects.init_project(tmp_path / "pipe")
    project.run(campaign=True, parameters={"mean_velocity": 0.02})

    with pytest.raises(ProjectError, match="Unknown project parameter"):
        project.campaign_operating_map(
            x_parameter="velocity_typo",
            y_quantity="flow.pressure_drop",
        )
    with pytest.raises(ProjectError, match="Unknown campaign quantity"):
        project.campaign_operating_map(
            x_parameter="mean_velocity",
            y_quantity="flow.not-a-quantity",
        )
    with pytest.raises(ProjectError, match=".svg suffix"):
        project.export_campaign_operating_map(
            tmp_path / "map.png",
            x_parameter="mean_velocity",
            y_quantity="flow.pressure_drop",
        )


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
                "--max-parallel",
                "2",
                "--json",
            ]
        )
        == 0
    )
    cli_preview = json.loads(capsys.readouterr().out)
    assert cli_preview["would_execute_count"] == 0
    assert cli_preview["observation_cost"]["solver_processes_started"] == 0
    assert cli_preview["execution_policy"]["requested_parallel_runs"] == 2
    assert cli_preview["execution_policy"]["effective_parallel_runs"] == 0
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


def test_campaign_sweep_runs_with_bounded_parallelism_and_preserves_records(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    original = projects.ReferencePipeProvider.run
    active = 0
    maximum_active = 0
    guard = threading.Lock()

    def observed_run(provider, step):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.03)
            return original(provider, step)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(projects.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(projects.ReferencePipeProvider, "run", observed_run)
    points = {
        "slow": {"mean_velocity": 0.01},
        "base": {"mean_velocity": 0.02},
        "fast": {"mean_velocity": 0.03},
        "faster": {"mean_velocity": 0.04},
        "base-copy": {"mean_velocity": 0.02},
    }

    preview = project.plan_campaign(points, maximum_parallel_runs=3)
    assert preview["execution_policy"]["requested_parallel_runs"] == 3
    assert preview["execution_policy"]["effective_parallel_runs"] == 3
    assert preview["execution_policy"]["automatic_retries"] == 0
    assert preview["execution_policy"]["maximum_attempts_per_identity"] == 1
    assert preview["execution_policy"]["memory_estimate_available"] is False
    assert preview["execution_policy"]["parallel_ready"] is True

    report = project.run_campaign(points, maximum_parallel_runs=3)

    jsonschema.Draft202012Validator(
        contracts.load("campaign-sweep.schema.json")
    ).validate(report)
    assert maximum_active == 3
    assert report["complete"] is True
    assert report["successful"] is True
    assert report["executed_count"] == 4
    assert report["reused_count"] == 1
    assert report["execution_policy"]["requested_parallel_runs"] == 3
    assert report["execution_policy"]["effective_parallel_runs"] == 3
    assert report["execution_policy"]["automatic_retries"] == 0
    assert [row["name"] for row in report["points"]] == list(points)
    assert report["points"][-1]["run_id"] == report["points"][1]["run_id"]
    run_ids = {
        row["run_id"] for row in report["points"] if row["execution"] == "executed"
    }
    assert len(run_ids) == 4
    performance = json.loads(
        (project.root / ".agentcfd" / "performance.json").read_text()
    )
    assert len(performance["samples"]) == 4


def test_campaign_parallelism_rejects_ambiguous_fail_fast_and_invalid_limits(
    tmp_path, monkeypatch
):
    project = projects.init_project(tmp_path / "pipe")
    points = {
        "base": {"mean_velocity": 0.02},
        "fast": {"mean_velocity": 0.03},
    }
    monkeypatch.setattr(projects.os, "cpu_count", lambda: 4)

    with pytest.raises(ProjectError, match="integer from 1 through 32"):
        project.plan_campaign(points, maximum_parallel_runs=0)
    with pytest.raises(ProjectError, match="cannot be combined"):
        project.run_campaign(
            points,
            maximum_parallel_runs=2,
            fail_fast=True,
        )
    assert project.campaign_index()["run_count"] == 0


def test_openfoam_campaign_parallelism_fails_closed_on_aggregate_storage(
    tmp_path, monkeypatch
):
    _mock_openfoam_runtime(monkeypatch)
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    points = {
        "base": {"mean_velocity": 0.5, "baffle_height": 0.12},
        "faster": {"mean_velocity": 0.6, "baffle_height": 0.12},
    }
    monkeypatch.setattr(projects.os, "cpu_count", lambda: 4)

    preview = project.plan_campaign(
        points,
        summary_only=True,
        maximum_parallel_runs=2,
    )
    policy = preview["execution_policy"]
    assert policy["temporary_storage_estimates_complete"] is True
    assert policy["concurrent_temporary_peak_bytes"] > 0
    assert policy["storage_admission_bytes"] >= policy[
        "concurrent_temporary_peak_bytes"
    ]
    assert policy["temporary_storage_within_budget"] is True

    disk = projects.shutil.disk_usage(project.root)
    monkeypatch.setattr(
        projects.shutil,
        "disk_usage",
        lambda _path: disk._replace(free=1),
    )
    with pytest.raises(ProjectError, match="exceeds currently available disk"):
        project.run_campaign(
            points,
            summary_only=True,
            maximum_parallel_runs=2,
        )
    assert project.campaign_index()["run_count"] == 0


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


def test_human_status_parameter_hint_targets_selected_project(tmp_path, capsys):
    root = tmp_path / "pipe with spaces"
    projects.init_project(root)

    assert entrypoint(["status", str(root)]) == 0

    output = capsys.readouterr().out
    expected = f"agentcfd run {shlex.quote(str(root))} --param NAME=JSON"
    assert f"change with: {expected}" in output
    assert "change with: agentcfd run ." not in output


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
    calibration = output_plan["estimate_calibration"]
    assert calibration["safety_factor"] == 1.25
    assert calibration["maximum_managed_vtu_frames"] == 4
    assert calibration["raw_staging_bytes"] == (
        output_plan["estimated_portable_bytes"]
        + calibration["native_solver_field_bytes"]
        + calibration["maximum_vtu_batch_bytes"]
    )
    assert (
        output_plan["estimated_temporary_peak_bytes"]
        >= 1.25 * calibration["raw_staging_bytes"]
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
    assert Path(complete["postprocess"]["result"]).parts[-2:] == (
        "output",
        "result.json",
    )

    case = project.entrypoint
    case.write_text(
        case.read_text().replace('name="water-pipe"', 'name="water-pipe-v2"')
    )
    modified = project.status()
    assert modified["state"] == "modified"
    assert modified["inputs_changed"] is True
    assert "Inputs changed" in modified["next_action"]["reason"]


def test_result_summary_is_lightweight_filterable_and_cli_visible(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run()

    report = project.result_summary(quantities=("flow.pressure_drop",))

    summary_path = completed.directory / "summary.json"
    assert summary_path.is_file()
    assert summary_path.stat().st_size < completed.result_path.stat().st_size
    jsonschema.Draft202012Validator(
        contracts.load("result-summary.schema.json")
    ).validate(report)
    assert report["schema"] == "agentcfd.result-summary/0.3"
    assert report["summary"] == str(summary_path)
    assert report["source_result"]["bytes"] == completed.result_path.stat().st_size
    assert report["run_id"] == completed.run_id
    assert set(report["quantities"]) == {"flow.pressure_drop"}
    assert "flow.mass_flow_rate" in report["available"]["quantities"]
    assert report["observation_cost"]["field_payloads_opened"] == 0
    assert report["observation_cost"]["result_json_bytes_read"] == 0
    assert report["observation_cost"]["summary_json_bytes_read"] == (
        summary_path.stat().st_size
    )
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


def test_result_summary_and_human_cli_distinguish_design_requirements(tmp_path, capsys):
    project = projects.init_project(tmp_path / "constrained-pipe")
    case = project.entrypoint
    source = case.read_text()
    case.write_text(
        source.replace(
            "output=outputs.standard(),",
            """output=outputs.standard(
            criteria=(
                outputs.require(
                    "pressure-budget",
                    quantity="flow.pressure_drop",
                    unit="Pa",
                    maximum=0.0,
                ),
            ),
        ),""",
        )
    )

    completed = project.run()
    report = project.result_summary()

    assert completed.result.converged is True
    assert completed.result.trust_level == "verified"
    assert completed.result.accepted is False
    assert len(report["requirements"]) == 1
    assert report["requirements"][0]["name"] == "requirement.pressure-budget"
    assert report["requirements"][0]["passed"] is False
    assert "unmet design requirements" in report["next_action"]["reason"]
    jsonschema.Draft202012Validator(
        contracts.load("result-summary.schema.json")
    ).validate(report)

    assert entrypoint(["result", str(project.root)]) == 3
    human = capsys.readouterr().out
    assert "Design requirements:\n" in human
    assert "[FAIL] requirement.pressure-budget:" in human
    assert "target <= 0.0 Pa" in human


def test_project_exports_verified_agentcae_scalar_sample(tmp_path, capsys):
    project = projects.init_project(tmp_path / "pipe")
    completed = project.run(parameters={"mean_velocity": 0.03})

    sample = project.scientific_sample(
        inputs=("diameter", "mean_velocity"),
        outputs=("flow.pressure_drop", "flow.mass_flow_rate"),
    )

    jsonschema.Draft202012Validator(
        contracts.load("scientific-sample.schema.json")
    ).validate(sample)
    assert sample["case_id"] == f"agentcfd-{completed.run_id}"
    assert sample["inputs"] == {"diameter": 0.05, "mean_velocity": 0.03}
    assert set(sample["outputs"]) == {
        "flow.mass_flow_rate",
        "flow.pressure_drop",
    }
    sample_export = sample["provenance"]["sample_export"]
    assert sample_export["project_verified"] is True
    assert sample_export["run_id"] == completed.run_id
    assert sample_export["source_result_sha256"] == hashlib.sha256(
        completed.result_path.read_bytes()
    ).hexdigest()
    assert [record["name"] for record in sample_export["input_schema"]] == [
        "diameter",
        "mean_velocity",
    ]
    assert sample_export["input_schema"][0]["metadata"]["unit"] == "m"
    assert set(sample["artifacts"]) == {"result", "summary"}

    target = tmp_path / "datasets" / "pipe-001.json"
    assert (
        entrypoint(
            [
                "export",
                "sample",
                str(project.root),
                str(target),
                "--input",
                "length",
                "--output-quantity",
                "flow.pressure_drop",
                "--case-id",
                "pipe-001",
                "--json",
            ]
        )
        == 0
    )
    cli_sample = json.loads(capsys.readouterr().out)
    assert cli_sample["case_id"] == "pipe-001"
    assert cli_sample["inputs"] == {"length": 10.0}
    assert json.loads(target.read_text(encoding="utf-8")) == cli_sample

    with pytest.raises(ProjectError, match="Unknown or non-numeric sample inputs"):
        project.scientific_sample(
            inputs=("missing",), outputs=("flow.pressure_drop",)
        )


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
    checkpoint = project.run_root / "evidence" / "restart.zip"
    checkpoint.parent.mkdir()
    with zipfile.ZipFile(checkpoint, "w") as bundle:
        bundle.writestr(
            "restart.json",
            json.dumps(
                {
                    "schema": "agentcfd.openfoam-restart/0.1",
                    "retained_times": [0.5],
                    "in_run_publication_count": 1,
                    "first_in_run_publication_time": 0.5,
                    "latest_in_run_publication_time": 0.5,
                    "latest_time": 0.5,
                    "atomic_publication": True,
                    "bounded_memory_streaming": True,
                }
            ),
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
    assert progress["checkpoint"] == {
        "status": "available",
        "path": str(checkpoint),
        "retained_times": [0.5],
        "in_run_publication_count": 1,
        "first_in_run_publication_time": 0.5,
        "latest_in_run_publication_time": 0.5,
        "latest_time": 0.5,
        "unit": "s",
        "size_bytes": checkpoint.stat().st_size,
        "atomic_publication": True,
        "bounded_memory_streaming": True,
    }
    assert progress["observation_cost"]["field_payloads_opened"] == 0
    assert progress["observation_cost"]["monitor_bytes_read"] > 0
    assert progress["observation_cost"]["checkpoint_metadata_bytes_read"] > 0
    assert progress["estimated_remaining"]["minimum_seconds"] >= 0


def test_project_status_reports_bounded_field_export_progress(tmp_path):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    project.run_root.mkdir()
    marker = project.run_root / "run.json"
    record = {
        "schema": "agentcfd.project-run/0.1",
        "run_id": "live-field-export",
        "mode": "replace",
        "directory": str(project.run_root),
        "status": "exporting",
        "phase": "portable-fields",
        "pid": os.getpid(),
        "started_at": "2026-09-06T00:00:00+00:00",
        "completed_at": None,
        "field_export": {
            "schema": "agentcfd.field-export-progress/0.1",
            "phase": "writing",
            "completed_frames": 0,
            "total_frames": 10,
            "fraction": 0.0,
            "batch_index": 1,
            "batch_count": 3,
            "current_batch_frames": 4,
            "maximum_batch_frames": 4,
            "updated_at": "2026-09-06T00:00:03+00:00",
        },
    }
    marker.write_text(json.dumps(record))

    report = project.status()

    jsonschema.Draft202012Validator(
        contracts.load("project-status.schema.json")
    ).validate(report)
    assert report["state"] == "running"
    assert report["progress"]["current_command"] == "portable-field-export"
    assert report["progress"]["field_export"] == record["field_export"]
    assert report["progress"]["estimated_remaining"] is None
    assert report["progress"]["observation_cost"]["field_payloads_opened"] == 0
    assert _watch_summary(report).startswith(
        "RUNNING | portable-field-export | frames 0/10 0.0% | batch 1/3"
    )

    record["field_export"]["fraction"] = 0.9
    marker.write_text(json.dumps(record))
    assert project.status()["progress"]["field_export"] is None


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
    assert Path(snapshots[1]["postprocess"]["result"]).parts[-2:] == (
        "output",
        "result.json",
    )


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
