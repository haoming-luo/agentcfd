"""Deterministic, dependency-free geometry generators for common CFD domains."""

from __future__ import annotations

import hashlib
import math
import os
import shlex
from pathlib import Path
from typing import Iterable

from ._validation import integer_at_least, positive_float


_Point = tuple[float, float, float]
_Triangle = tuple[_Point, _Point, _Point]
_MAX_TRIANGLES = 2_000_000


def _bounded_integer(
    value: object, *, name: str, minimum: int, maximum: int
) -> int:
    selected = integer_at_least(value, name=name, minimum=minimum)
    if selected > maximum:
        raise ValueError(f"{name} must not exceed {maximum}.")
    return selected


def _point_on_ring(
    center: _Point,
    tangent: _Point,
    radius: float,
    angle: float,
) -> _Point:
    # For this planar elbow, the in-plane normal is the tangent rotated +90°.
    normal = (-tangent[1], tangent[0], 0.0)
    radial = math.cos(angle)
    transverse = math.sin(angle)
    return (
        center[0] + radius * radial * normal[0],
        center[1] + radius * radial * normal[1],
        center[2] + radius * transverse,
    )


def _vector(a: _Point, b: _Point) -> _Point:
    return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


def _cross(a: _Point, b: _Point) -> _Point:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normal(triangle: _Triangle) -> _Point:
    raw = _cross(_vector(triangle[0], triangle[1]), _vector(triangle[0], triangle[2]))
    magnitude = math.sqrt(sum(value * value for value in raw))
    if magnitude <= 0.0:
        raise ValueError("Generated geometry contains a degenerate triangle.")
    return tuple(value / magnitude for value in raw)  # type: ignore[return-value]


def _number(value: float) -> str:
    if abs(value) < 5.0e-16:
        value = 0.0
    return format(value, ".17g")


def _stl_region(name: str, triangles: Iterable[_Triangle]) -> list[str]:
    lines = [f"solid {name}"]
    for triangle in triangles:
        normal = _normal(triangle)
        lines.append("  facet normal " + " ".join(_number(value) for value in normal))
        lines.append("    outer loop")
        for point in triangle:
            lines.append("      vertex " + " ".join(_number(value) for value in point))
        lines.extend(("    endloop", "  endfacet"))
    lines.append(f"endsolid {name}")
    return lines


def _elbow_geometry(
    *,
    diameter_m: object,
    bend_radius_m: object,
    inlet_length_m: object,
    outlet_length_m: object,
    cross_section_segments: object,
    bend_segments: object,
    inlet_segments: object,
    outlet_segments: object,
) -> tuple[dict[str, object], bytes]:
    diameter = positive_float(diameter_m, name="diameter_m")
    bend_radius = positive_float(bend_radius_m, name="bend_radius_m")
    inlet_length = positive_float(inlet_length_m, name="inlet_length_m")
    outlet_length = positive_float(outlet_length_m, name="outlet_length_m")
    cross_segments = _bounded_integer(
        cross_section_segments,
        name="cross_section_segments",
        minimum=8,
        maximum=512,
    )
    target_station_spacing = diameter / 4.0
    bend_count = (
        min(
            512,
            max(
                12,
                math.ceil(
                    0.5 * math.pi * bend_radius / target_station_spacing
                ),
            ),
        )
        if bend_segments is None
        else _bounded_integer(
            bend_segments, name="bend_segments", minimum=4, maximum=512
        )
    )
    inlet_count = (
        min(256, max(1, math.ceil(inlet_length / target_station_spacing)))
        if inlet_segments is None
        else _bounded_integer(
            inlet_segments, name="inlet_segments", minimum=1, maximum=256
        )
    )
    outlet_count = (
        min(256, max(1, math.ceil(outlet_length / target_station_spacing)))
        if outlet_segments is None
        else _bounded_integer(
            outlet_segments, name="outlet_segments", minimum=1, maximum=256
        )
    )
    tube_radius = 0.5 * diameter
    if bend_radius <= tube_radius:
        raise ValueError(
            "bend_radius_m must exceed half the diameter to avoid a self-intersecting elbow."
        )

    stations: list[tuple[_Point, _Point]] = []
    for index in range(inlet_count + 1):
        fraction = index / inlet_count
        stations.append(((-inlet_length * (1.0 - fraction), 0.0, 0.0), (1.0, 0.0, 0.0)))
    for index in range(1, bend_count + 1):
        theta = 0.5 * math.pi * index / bend_count
        stations.append(
            (
                (
                    bend_radius * math.sin(theta),
                    bend_radius * (1.0 - math.cos(theta)),
                    0.0,
                ),
                (math.cos(theta), math.sin(theta), 0.0),
            )
        )
    for index in range(1, outlet_count + 1):
        distance = outlet_length * index / outlet_count
        stations.append(((bend_radius, bend_radius + distance, 0.0), (0.0, 1.0, 0.0)))

    rings = [
        tuple(
            _point_on_ring(
                center,
                tangent,
                tube_radius,
                2.0 * math.pi * index / cross_segments,
            )
            for index in range(cross_segments)
        )
        for center, tangent in stations
    ]
    walls: list[_Triangle] = []
    for station_index in range(len(rings) - 1):
        first = rings[station_index]
        second = rings[station_index + 1]
        for angle_index in range(cross_segments):
            following = (angle_index + 1) % cross_segments
            # angle × station gives the outward orientation for this ring frame.
            walls.append(
                (first[angle_index], first[following], second[following])
            )
            walls.append(
                (first[angle_index], second[following], second[angle_index])
            )

    inlet_center = stations[0][0]
    outlet_center = stations[-1][0]
    inlet: list[_Triangle] = []
    outlet: list[_Triangle] = []
    for index in range(cross_segments):
        following = (index + 1) % cross_segments
        inlet.append((inlet_center, rings[0][following], rings[0][index]))
        outlet.append((outlet_center, rings[-1][index], rings[-1][following]))

    triangle_count = len(inlet) + len(walls) + len(outlet)
    if triangle_count > _MAX_TRIANGLES:
        raise ValueError(
            f"Requested tessellation has {triangle_count} triangles; maximum is {_MAX_TRIANGLES}."
        )
    lines: list[str] = []
    lines.extend(_stl_region("inlet", inlet))
    lines.extend(_stl_region("walls", walls))
    lines.extend(_stl_region("outlet", outlet))
    payload = ("\n".join(lines) + "\n").encode("utf-8")

    middle = math.pi / 4.0
    nominal_area = math.pi * tube_radius**2
    tessellated_area = (
        0.5
        * cross_segments
        * tube_radius**2
        * math.sin(2.0 * math.pi / cross_segments)
    )
    recommendations = {
        "interior_point_m": [
            bend_radius * math.sin(middle),
            bend_radius * (1.0 - math.cos(middle)),
            0.0,
        ],
        "boundary_roles": {"inlet": "inlet", "outlet": "outlet", "walls": "wall"},
        "inlet_velocity_direction": [1.0, 0.0, 0.0],
        "measurement_sections": [
            {
                "name": "upstream",
                "kind": "plane",
                "role": "observation",
                "origin_m": [-0.5 * inlet_length, 0.0, 0.0],
                "normal": [1.0, 0.0, 0.0],
            },
            {
                "name": "downstream",
                "kind": "plane",
                "role": "observation",
                "origin_m": [bend_radius, bend_radius + 0.5 * outlet_length, 0.0],
                "normal": [0.0, 1.0, 0.0],
            },
        ],
        "mesh_starting_point": {
            "base_size_m": diameter / 8.0,
            "maximum_cells": 2_000_000,
            "status": "starting-point-not-accuracy-guarantee",
        },
        "project_initialization": {
            "template": "imported-internal-flow",
            "provider": "openfoam",
            "geometry_unit": "m",
            "geometry_path": None,
            "required_user_decisions": [
                "project_directory",
                "inlet_velocity_mass_flow_or_total_pressure",
            ],
        },
    }
    geometry = {
        "type": "circular-elbow-90deg",
        "unit": "m",
        "parameters": {
            "diameter_m": diameter,
            "bend_radius_m": bend_radius,
            "inlet_length_m": inlet_length,
            "outlet_length_m": outlet_length,
            "cross_section_segments": cross_segments,
            "bend_segments": bend_count,
            "inlet_segments": inlet_count,
            "outlet_segments": outlet_count,
        },
        "bounds_m": {
            "minimum": [-inlet_length, -tube_radius, -tube_radius],
            "maximum": [bend_radius + tube_radius, bend_radius + outlet_length, tube_radius],
        },
        "triangle_count": triangle_count,
        "tessellation_accuracy": {
            "nominal_cross_section_area_m2": nominal_area,
            "tessellated_cross_section_area_m2": tessellated_area,
            "cross_section_area_error_percent": 100.0
            * (nominal_area - tessellated_area)
            / nominal_area,
            "maximum_radial_chord_error_m": tube_radius
            * (1.0 - math.cos(math.pi / cross_segments)),
        },
        "region_triangle_counts": {
            "inlet": len(inlet),
            "walls": len(walls),
            "outlet": len(outlet),
        },
        "watertight_by_construction": True,
        "outward_oriented_by_construction": True,
    }
    return {"geometry": geometry, "recommendations": recommendations}, payload


def plan_circular_elbow_stl(
    path: str | Path,
    *,
    diameter_m: object,
    bend_radius_m: object,
    inlet_length_m: object,
    outlet_length_m: object,
    cross_section_segments: object = 32,
    bend_segments: object | None = None,
    inlet_segments: object | None = None,
    outlet_segments: object | None = None,
) -> dict[str, object]:
    """Plan an exact 90° circular elbow STL without writing it."""

    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != ".stl":
        raise ValueError("Generated circular-elbow geometry must use an .stl path.")
    generated, payload = _elbow_geometry(
        diameter_m=diameter_m,
        bend_radius_m=bend_radius_m,
        inlet_length_m=inlet_length_m,
        outlet_length_m=outlet_length_m,
        cross_section_segments=cross_section_segments,
        bend_segments=bend_segments,
        inlet_segments=inlet_segments,
        outlet_segments=outlet_segments,
    )
    parameters = generated["geometry"]["parameters"]
    assert isinstance(parameters, dict)
    command = " ".join(
        (
            "agentcfd geometry-create elbow",
            shlex.quote(str(target)),
            "--diameter-m",
            _number(float(parameters["diameter_m"])),
            "--bend-radius-m",
            _number(float(parameters["bend_radius_m"])),
            "--inlet-length-m",
            _number(float(parameters["inlet_length_m"])),
            "--outlet-length-m",
            _number(float(parameters["outlet_length_m"])),
            "--cross-section-segments",
            str(parameters["cross_section_segments"]),
            "--bend-segments",
            str(parameters["bend_segments"]),
            "--inlet-segments",
            str(parameters["inlet_segments"]),
            "--outlet-segments",
            str(parameters["outlet_segments"]),
        )
    )
    return {
        "schema": "agentcfd.generated-geometry/0.1",
        **generated,
        "artifact": {
            "path": str(target),
            "size_bytes": len(payload),
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "written": False,
            "already_exists": target.exists(),
        },
        "next_action": {
            "command": command,
            "reason": "Write only after reviewing the exact parameters and output path.",
        },
    }


def write_circular_elbow_stl(
    path: str | Path,
    *,
    diameter_m: object,
    bend_radius_m: object,
    inlet_length_m: object,
    outlet_length_m: object,
    cross_section_segments: object = 32,
    bend_segments: object | None = None,
    inlet_segments: object | None = None,
    outlet_segments: object | None = None,
) -> tuple[Path, dict[str, object]]:
    """Atomically create a named-region circular-elbow fluid-volume STL."""

    target = Path(path).expanduser().resolve()
    report = plan_circular_elbow_stl(
        target,
        diameter_m=diameter_m,
        bend_radius_m=bend_radius_m,
        inlet_length_m=inlet_length_m,
        outlet_length_m=outlet_length_m,
        cross_section_segments=cross_section_segments,
        bend_segments=bend_segments,
        inlet_segments=inlet_segments,
        outlet_segments=outlet_segments,
    )
    if target.exists():
        raise FileExistsError(f"Generated geometry output already exists: {target}")
    generated, payload = _elbow_geometry(
        **report["geometry"]["parameters"]  # type: ignore[arg-type,index]
    )
    del generated
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"Temporary geometry output already exists: {temporary}")
    try:
        temporary.write_bytes(payload)
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise FileExistsError(
                f"Generated geometry output already exists: {target}"
            ) from error
    finally:
        if temporary.exists():
            temporary.unlink()
    artifact = dict(report["artifact"])  # type: ignore[arg-type]
    artifact["written"] = True
    artifact["already_exists"] = False
    report["artifact"] = artifact
    recommendations = dict(report["recommendations"])  # type: ignore[arg-type]
    project_initialization = dict(
        recommendations["project_initialization"]  # type: ignore[arg-type]
    )
    project_initialization["geometry_path"] = str(target)
    recommendations["project_initialization"] = project_initialization
    report["recommendations"] = recommendations
    report["next_action"] = {
        "command": (
            f"agentcfd geometry-check {shlex.quote(str(target))} --unit m "
            "--internal-flow --role inlet=inlet --role outlet=outlet --role walls=wall"
        ),
        "reason": "Independently verify topology, orientation, dimensions, and boundary roles.",
    }
    return target, report
