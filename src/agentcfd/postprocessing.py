"""Reproducible post-processing recipes over portable AgentCFD fields."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from .outputs import ContourView, SliceView, StreamlineView, ViewRecipe


def _python_value(value: object) -> str:
    """Serialize strings/numbers/lists as a safe Python literal subset."""

    return repr(value)


def _field_index(manifest: Mapping[str, object]) -> dict[str, dict[str, object]]:
    records = manifest.get("fields")
    if not isinstance(records, list):
        raise ValueError("Field-bundle manifest has no field records.")
    result = {}
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("name"), str):
            name = str(record["name"])
            if name not in result or record.get("association") == "point":
                result[name] = record
    return result


def _color_command(record: Mapping[str, object]) -> str:
    association = "POINTS" if record.get("association") == "point" else "CELLS"
    name = str(record["export_name"])
    components = record.get("components")
    selection: tuple[str, ...] = (
        (association, name, "Magnitude")
        if isinstance(components, list) and len(components) > 1
        else (association, name)
    )
    return f"ColorBy(display, {_python_value(selection)})"


def _recipe_script(recipe: ViewRecipe, record: Mapping[str, object]) -> str:
    association = "POINTS" if record.get("association") == "point" else "CELLS"
    field_name = str(record["export_name"])
    common = [
        '"""Generated from AgentCFD output intent; rerun the project to regenerate."""',
        "from pathlib import Path",
        "from paraview.simple import *",
        "",
        "recipe_dir = Path(__file__).resolve().parent",
        'fields_path = recipe_dir.parent / "fields" / "fields.xdmf"',
        f"source = XDMFReader(registrationName={recipe.name!r}, FileNames=[str(fields_path)])",
        "source.UpdatePipeline()",
        "animation = GetAnimationScene()",
        "animation.UpdateAnimationUsingDataTimeSteps()",
        'view = GetActiveViewOrCreate("RenderView")',
    ]
    if isinstance(recipe, SliceView):
        filter_lines = [
            f"filtered = Slice(registrationName={recipe.name!r}, Input=source)",
            'filtered.SliceType = "Plane"',
            f"filtered.SliceType.Origin = {_python_value(list(recipe.origin))}",
            f"filtered.SliceType.Normal = {_python_value(list(recipe.normal))}",
        ]
    elif isinstance(recipe, ContourView):
        filter_lines = [
            f"filtered = Contour(registrationName={recipe.name!r}, Input=source)",
            f"filtered.ContourBy = {_python_value([association, field_name])}",
            f"filtered.Isosurfaces = {_python_value(list(recipe.values))}",
        ]
    elif isinstance(recipe, StreamlineView):
        directions = {"forward": "FORWARD", "backward": "BACKWARD", "both": "BOTH"}
        filter_lines = [
            f"filtered = StreamTracer(registrationName={recipe.name!r}, Input=source, SeedType='Line')",
            f"filtered.Vectors = {_python_value([association, field_name])}",
            f"filtered.SeedType.Point1 = {_python_value(list(recipe.seed_start))}",
            f"filtered.SeedType.Point2 = {_python_value(list(recipe.seed_end))}",
            f"filtered.SeedType.Resolution = {recipe.seeds - 1}",
            f"filtered.IntegrationDirection = {directions[recipe.direction]!r}",
        ]
    else:  # pragma: no cover - closed public union and OutputRequest validation
        raise TypeError(f"Unsupported post-processing recipe {type(recipe).__name__}.")
    camera = recipe.camera
    camera_lines = (
        ["view.ResetCamera()"]
        if camera is None
        else [
            f"view.CameraPosition = {_python_value(list(camera.position))}",
            f"view.CameraFocalPoint = {_python_value(list(camera.focal_point))}",
            f"view.CameraViewUp = {_python_value(list(camera.view_up))}",
            *(
                [
                    "view.CameraParallelProjection = 1",
                    f"view.CameraParallelScale = {camera.parallel_scale!r}",
                ]
                if camera.parallel_scale is not None
                else []
            ),
        ]
    )
    export = recipe.export
    export_lines = []
    if export is not None:
        export_lines.append(f"view.ViewSize = {_python_value(list(export.size))}")
        if export.screenshot:
            export_lines.append(
                f"SaveScreenshot(str(recipe_dir / {recipe.name + '.png'!r}), view, "
                f"ImageResolution={_python_value(list(export.size))}, "
                f"TransparentBackground={int(export.transparent_background)})"
            )
        if export.animation is not None:
            animation_name = (
                f"{recipe.name}.%04d.png"
                if export.animation == "png-sequence"
                else f"{recipe.name}.mp4"
            )
            export_lines.append(
                f"SaveAnimation(str(recipe_dir / {animation_name!r}), view, "
                f"ImageResolution={_python_value(list(export.size))}, "
                f"FrameRate={export.frame_rate})"
            )
    finish = [
        "display = Show(filtered, view)",
        _color_command(record),
        "display.SetScalarBarVisibility(view, True)",
        "Hide(source, view)",
        *camera_lines,
        "Render()",
        *export_lines,
        f"SaveState(str(recipe_dir / {recipe.name + '.pvsm'!r}))",
        "",
    ]
    return "\n".join([*common, *filter_lines, *finish])


def publish_paraview_recipes(
    run_directory: str | Path,
    recipes: Sequence[ViewRecipe],
    field_manifest: Mapping[str, object],
) -> tuple[Path, tuple[Path, ...]]:
    """Write tiny editable scripts; never duplicate the XDMF/HDF5 field payload."""

    root = Path(run_directory)
    directory = root / "postprocess"
    fields = _field_index(field_manifest)
    resolved: list[tuple[ViewRecipe, dict[str, object]]] = []
    for recipe in recipes:
        try:
            field_record = fields[recipe.field]
        except KeyError as error:
            raise ValueError(
                f"Post-processing recipe {recipe.name!r} field {recipe.field!r} "
                "is absent from the portable bundle."
            ) from error
        if field_record.get("association") != "point":
            raise ValueError(
                f"Post-processing recipe {recipe.name!r} requires a visualization "
                "point field. Use portable_profile='visualization' or 'both'."
            )
        components = field_record.get("components")
        component_count = len(components) if isinstance(components, list) else 0
        if isinstance(recipe, ContourView) and component_count > 1:
            raise ValueError(
                f"Contour recipe {recipe.name!r} requires a scalar field; "
                f"{recipe.field!r} is vector-valued."
            )
        if isinstance(recipe, StreamlineView) and component_count <= 1:
            raise ValueError(
                f"Streamline recipe {recipe.name!r} requires a vector field; "
                f"{recipe.field!r} is scalar-valued."
            )
        resolved.append((recipe, field_record))

    directory.mkdir(parents=True, exist_ok=True)
    records = []
    scripts = []
    for recipe, field_record in resolved:
        script = directory / f"{recipe.name}.py"
        script.write_text(_recipe_script(recipe, field_record), encoding="utf-8")
        scripts.append(script)
        render_outputs = [f"{recipe.name}.pvsm"]
        if recipe.export is not None:
            if recipe.export.screenshot:
                render_outputs.append(f"{recipe.name}.png")
            if recipe.export.animation == "png-sequence":
                render_outputs.append(f"{recipe.name}.%04d.png")
            elif recipe.export.animation == "mp4":
                render_outputs.append(f"{recipe.name}.mp4")
        records.append(
            {
                **recipe.to_dict(),
                "export_field": field_record["export_name"],
                "association": field_record["association"],
                "script": script.name,
                "state_after_launch": f"{recipe.name}.pvsm",
                "render_outputs_after_launch": render_outputs,
                "shares_field_payload": "../fields/fields.xdmf",
            }
        )
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "agentcfd.postprocess-recipes/0.1",
                "source": "../fields/fields.xdmf",
                "payload_copies": 0,
                "recipes": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path, tuple(scripts)


def read_recipe_manifest(run_directory: str | Path) -> dict[str, object] | None:
    """Read the small recipe index without loading any field arrays."""

    path = Path(run_directory) / "postprocess" / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["publish_paraview_recipes", "read_recipe_manifest"]
