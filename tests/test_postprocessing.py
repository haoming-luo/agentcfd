import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from agentcfd import contracts, outputs, postprocessing, projects
from agentcfd.cli import entrypoint


def _field_manifest():
    return {
        "fields": [
            {
                "name": "fluid.velocity",
                "export_name": "fluid.velocity.point",
                "association": "point",
                "components": ["x", "y", "z"],
            },
            {
                "name": "fluid.pressure",
                "export_name": "fluid.pressure.point",
                "association": "point",
                "components": [],
            },
            {
                "name": "fluid.vorticity",
                "export_name": "fluid.vorticity.point",
                "association": "point",
                "components": ["x", "y", "z"],
            },
        ]
    }


def _recipes():
    return (
        outputs.slice_view(
            "midplane-vorticity",
            field="fluid.vorticity",
            origin=(0.6, 0.1, 0.05),
            normal=(0.0, 0.0, 2.0),
            component="normal",
            camera=outputs.camera(
                position=(0.6, 0.1, 2.0),
                focal_point=(0.6, 0.1, 0.05),
                parallel_scale=0.6,
            ),
            export=outputs.render(
                size=(960, 540),
                animation="png-sequence",
                frame_rate=20,
            ),
        ),
        outputs.contour_view(
            "pressure-levels",
            field="fluid.pressure",
            values=(100.0, 500.0),
        ),
        outputs.streamline_view(
            "wake-streamlines",
            seed_start=(0.02, 0.01, 0.05),
            seed_end=(0.02, 0.19, 0.05),
            seeds=30,
        ),
        outputs.line_profile(
            "centerline-pressure",
            field="fluid.pressure",
            start=(0.05, 0.1, 0.05),
            end=(1.15, 0.1, 0.05),
            samples=121,
        ),
    )


def _layouts():
    return (
        outputs.render_layout(
            "wake-overview",
            views=("midplane-vorticity", "wake-streamlines"),
            columns=2,
            export=outputs.render(size=(1280, 720)),
        ),
    )


def test_view_recipes_are_typed_normalized_and_part_of_analysis_identity(tmp_path):
    request = outputs.animation(every=0.1, views=_recipes(), layouts=_layouts())
    record = request.to_dict()

    assert record["views"][0]["normal"] == [0.0, 0.0, 1.0]
    assert record["views"][0]["component"] == "normal"
    assert record["views"][0]["camera"]["parallel_scale"] == 0.6
    assert record["views"][0]["export"]["size"] == [960, 540]
    assert record["views"][1]["values"] == [100.0, 500.0]
    assert record["views"][2]["seeds"] == 30
    assert record["views"][3]["samples"] == 121
    assert record["views"][3]["component"] is None
    assert record["views"][3]["camera"] is None
    assert record["layouts"][0]["views"] == [
        "midplane-vorticity",
        "wake-streamlines",
    ]
    assert record["layouts"][0]["rows"] == 1
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    analysis = project.load_step().to_dict()
    analysis["output"] = record
    jsonschema.Draft202012Validator(
        contracts.load("analysis-request.schema.json")
    ).validate(analysis)
    with pytest.raises(ValueError, match="require their fields"):
        outputs.OutputRequest(
            fields=("fluid.pressure",),
            histories=(),
            views=(_recipes()[0],),
        )
    with pytest.raises(ValueError, match="references unknown views"):
        outputs.animation(
            every=0.1,
            views=_recipes(),
            layouts=(
                outputs.render_layout(
                    "bad-layout", views=("midplane-vorticity", "missing")
                ),
            ),
        )
    with pytest.raises(ValueError, match="line profiles remain compact"):
        outputs.animation(
            every=0.1,
            views=_recipes(),
            layouts=(
                outputs.render_layout(
                    "bad-layout",
                    views=("midplane-vorticity", "centerline-pressure"),
                ),
            ),
        )


def test_paraview_recipes_share_one_portable_payload_and_validate(tmp_path):
    run = tmp_path / "output"
    fields = run / "fields"
    fields.mkdir(parents=True)
    (fields / "fields.xdmf").write_text("<Xdmf/>")

    manifest_path, scripts = postprocessing.publish_paraview_recipes(
        run, _recipes(), _field_manifest(), _layouts()
    )
    manifest = json.loads(manifest_path.read_text())

    jsonschema.Draft202012Validator(
        contracts.load("postprocess-recipes.schema.json")
    ).validate(manifest)
    assert manifest["payload_copies"] == 0
    assert len(scripts) == 5
    for script in scripts:
        compile(script.read_text(), str(script), "exec")
        assert "fields.xdmf" in script.read_text()
        assert str(tmp_path) not in script.read_text()
    assert "Slice(" in scripts[0].read_text()
    assert "PythonCalculator(" in scripts[0].read_text()
    assert "inputs[0].PointData" in scripts[0].read_text()
    assert "CameraParallelProjection = 1" in scripts[0].read_text()
    assert "SaveScreenshot" in scripts[0].read_text()
    assert "SaveAnimation" in scripts[0].read_text()
    assert manifest["recipes"][0]["render_outputs_after_launch"] == [
        "midplane-vorticity.pvsm",
        "midplane-vorticity.png",
        "midplane-vorticity.%04d.png",
    ]
    assert "Contour(" in scripts[1].read_text()
    assert "StreamTracer(" in scripts[2].read_text()
    assert "PlotOverLine(" in scripts[3].read_text()
    assert "ChooseArraysToWrite=1" in scripts[3].read_text()
    assert "distance_m" in scripts[3].read_text()
    assert "animation.GoToLast()" in scripts[3].read_text()
    assert manifest["recipes"][3]["data_outputs_after_launch"] == [
        "centerline-pressure.csv"
    ]
    layout = manifest["layouts"][0]
    assert layout["views"] == ["midplane-vorticity", "wake-streamlines"]
    assert layout["columns"] == 2
    assert layout["rows"] == 1
    assert layout["render_outputs_after_launch"] == [
        "wake-overview.pvsm",
        "wake-overview.png",
    ]
    layout_script = scripts[4].read_text()
    assert "CreateLayout(" in layout_script
    assert "layout.AssignView" in layout_script
    assert "SplitViewHorizontal" in layout_script
    assert layout_script.count("XDMFReader(") == 1
    assert "SaveScreenshot" in layout_script


def test_view_presentation_rejects_ambiguous_or_empty_render_intent():
    with pytest.raises(ValueError, match="position and focal point"):
        outputs.camera(position=(1.0, 1.0, 1.0), focal_point=(1.0, 1.0, 1.0))
    with pytest.raises(ValueError, match="screenshot or animation"):
        outputs.render(screenshot=False)
    with pytest.raises(ValueError, match="animation must"):
        outputs.render(animation="gif")


def test_missing_portable_recipe_field_fails_before_paraview(tmp_path):
    with pytest.raises(ValueError, match="absent from the portable bundle"):
        postprocessing.publish_paraview_recipes(
            tmp_path,
            (
                outputs.slice_view(
                    "temperature",
                    field="fluid.temperature",
                    origin=(0.0, 0.0, 0.0),
                    normal=(1.0, 0.0, 0.0),
                ),
            ),
            _field_manifest(),
        )


def test_recipe_field_shape_and_visualization_association_fail_early(tmp_path):
    with pytest.raises(ValueError, match="requires a scalar field"):
        postprocessing.publish_paraview_recipes(
            tmp_path / "vector-contour",
            (
                outputs.contour_view(
                    "bad-contour", field="fluid.velocity", values=(0.5,)
                ),
            ),
            _field_manifest(),
        )
    vector_manifest, vector_scripts = postprocessing.publish_paraview_recipes(
        tmp_path / "vector-magnitude-profile",
        (
            outputs.line_profile(
                "velocity-magnitude",
                field="fluid.velocity",
                start=(0.0, 0.0, 0.0),
                end=(1.0, 0.0, 0.0),
                component="magnitude",
            ),
        ),
        _field_manifest(),
    )
    vector_record = json.loads(vector_manifest.read_text())["recipes"][0]
    assert vector_record["component"] == "magnitude"
    assert "math.sqrt" in vector_scripts[0].read_text()
    with pytest.raises(ValueError, match="cannot select a component"):
        postprocessing.publish_paraview_recipes(
            tmp_path / "scalar-component",
            (
                outputs.line_profile(
                    "pressure-x",
                    field="fluid.pressure",
                    start=(0.0, 0.0, 0.0),
                    end=(1.0, 0.0, 0.0),
                    component="x",
                ),
            ),
            _field_manifest(),
        )
    assert not (tmp_path / "vector-contour" / "postprocess").exists()
    with pytest.raises(ValueError, match="requires a vector field"):
        postprocessing.publish_paraview_recipes(
            tmp_path / "scalar-streamlines",
            (
                outputs.streamline_view(
                    "bad-streamlines",
                    field="fluid.pressure",
                    seed_start=(0.0, 0.0, 0.0),
                    seed_end=(0.0, 1.0, 0.0),
                ),
            ),
            _field_manifest(),
        )
    with pytest.raises(ValueError, match="requires component"):
        postprocessing.publish_paraview_recipes(
            tmp_path / "vector-line-profile",
            (
                outputs.line_profile(
                    "bad-profile",
                    field="fluid.velocity",
                    start=(0.0, 0.0, 0.0),
                    end=(1.0, 0.0, 0.0),
                ),
            ),
            _field_manifest(),
        )
    native_only = _field_manifest()
    native_only["fields"][0]["association"] = "cell"
    with pytest.raises(ValueError, match="visualization point field"):
        postprocessing.publish_paraview_recipes(
            tmp_path / "native",
            (_recipes()[2],),
            native_only,
        )
    _manifest, tangential_scripts = postprocessing.publish_paraview_recipes(
        tmp_path / "tangential-slice",
        (
            outputs.slice_view(
                "tangential-velocity",
                field="fluid.velocity",
                origin=(0.5, 0.1, 0.05),
                normal=(1.0, 0.0, 0.0),
                component="tangential",
            ),
        ),
        _field_manifest(),
    )
    assert "sqrt(abs(mag(" in tangential_scripts[0].read_text()
    with pytest.raises(ValueError, match="from scalar field"):
        postprocessing.publish_paraview_recipes(
            tmp_path / "scalar-slice-component",
            (
                outputs.slice_view(
                    "bad-pressure-component",
                    field="fluid.pressure",
                    origin=(0.5, 0.1, 0.05),
                    normal=(1.0, 0.0, 0.0),
                    component="normal",
                ),
            ),
            _field_manifest(),
        )


def test_view_cli_selects_and_launches_named_recipe(tmp_path, monkeypatch, capsys):
    project = projects.init_project(
        tmp_path / "wake", template="baffle-channel", provider="openfoam"
    )
    plan = project.plan()
    project.run_root.mkdir()
    fields = project.run_root / "fields"
    fields.mkdir()
    (fields / "fields.xdmf").write_text("<Xdmf/>")
    (fields / "manifest.json").write_text(
        json.dumps(
            {
                **_field_manifest(),
                "axis": {
                    "name": "time",
                    "unit": "s",
                    "physical_time": True,
                    "values": [0.1],
                },
                "output_selection": {"profile": "visualization"},
                "storage": {"actual_portable_bytes": 7},
            }
        )
    )
    postprocessing.publish_paraview_recipes(
        project.run_root,
        project.load_step().output.views,
        _field_manifest(),
        project.load_step().output.layouts,
    )
    (project.run_root / "result.json").write_text("{}")
    (project.run_root / "run.json").write_text(
        json.dumps(
            {
                "schema": "agentcfd.project-run/0.1",
                "run_id": "recipe-run",
                "mode": "replace",
                "directory": str(project.run_root),
                "status": "completed",
                "accepted": True,
                "analysis_sha256": plan["model"]["analysis_sha256"],
                "execution_sha256": project._execution_fingerprint(
                    plan["model"]["analysis_sha256"], provider="openfoam"
                ),
                "completed_at": "2026-09-06T00:00:00+00:00",
            }
        )
    )
    launched = []
    monkeypatch.setattr("agentcfd.cli._paraview_executable", lambda: "/paraview")
    monkeypatch.setattr(
        "agentcfd.cli.subprocess.Popen", lambda argv: launched.append(argv)
    )

    assert (
        entrypoint(
            [
                "view",
                str(project.root),
                "--recipe",
                "wake-streamlines",
                "--launch",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-view.schema.json")
    ).validate(report)
    assert report["kind"] == "paraview-script"
    assert report["summary"]["field"] == "fluid.velocity"
    assert report["batch_completed"] is False
    assert report["produced"] == []
    assert launched == [["/paraview", "--script", report["target"]]]
    assert Path(report["target"]).name == "wake-streamlines.py"

    def run_batch(command, **_kwargs):
        recipe_directory = Path(command[1]).parent
        stem = Path(command[1]).stem
        if stem == "centerline-pressure":
            (recipe_directory / "centerline-pressure.csv").write_text(
                "pressure,distance\n"
            )
            (recipe_directory / "centerline-pressure.pvsm").write_text("state")
        else:
            (recipe_directory / "wake-overview.png").write_text("image")
            (recipe_directory / "wake-overview.pvsm").write_text("state")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agentcfd.cli._paraview_batch_executable", lambda: "/pvbatch")
    monkeypatch.setattr("agentcfd.cli.subprocess.run", run_batch)
    assert (
        entrypoint(
            [
                "view",
                str(project.root),
                "--recipe",
                "centerline-pressure",
                "--batch",
                "--json",
            ]
        )
        == 0
    )
    batch = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-view.schema.json")
    ).validate(batch)
    assert batch["launched"] is False
    assert batch["batch_completed"] is True
    assert batch["viewer"] == "/pvbatch"
    assert [Path(path).name for path in batch["produced"]] == [
        "centerline-pressure.pvsm",
        "centerline-pressure.csv",
    ]

    assert (
        entrypoint(
            [
                "view",
                str(project.root),
                "--layout",
                "wake-overview",
                "--batch",
                "--json",
            ]
        )
        == 0
    )
    overview = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(
        contracts.load("project-view.schema.json")
    ).validate(overview)
    assert overview["summary"]["type"] == "render-layout"
    assert [Path(path).name for path in overview["produced"]] == [
        "wake-overview.pvsm",
        "wake-overview.png",
    ]
