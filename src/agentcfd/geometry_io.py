"""Read-only, bounded-memory geometry preflight for imported CFD surfaces."""

from __future__ import annotations

import hashlib
import math
import shutil
import struct
import re
from pathlib import Path
from typing import Iterable, Iterator, Mapping


_UNIT_SCALE_TO_M = {
    "m": 1.0,
    "mm": 1.0e-3,
    "cm": 1.0e-2,
    "um": 1.0e-6,
    "in": 0.0254,
    "ft": 0.3048,
}
_TRIANGULATED_FORMATS = {".stl": "stl", ".obj": "obj"}
_CAD_FORMATS = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges"}
_BOUNDARY_ROLES = {
    "inlet",
    "outlet",
    "wall",
    "symmetry",
    "periodic",
    "interface",
    "farfield",
    "opening",
    "empty",
}
_ROLE_HINTS = {
    "inlet": {"inlet", "inflow", "upstream", "supply", "intake"},
    "outlet": {"outlet", "outflow", "downstream", "exhaust", "return"},
    "wall": {"wall", "walls", "housing", "pipe", "baffle", "solid"},
    "symmetry": {"symmetry", "sym", "mirror"},
    "periodic": {"periodic", "cyclic"},
    "interface": {"interface", "coupled"},
    "farfield": {"farfield", "freestream", "ambient"},
    "opening": {"opening", "vent"},
    "empty": {"empty", "frontback"},
}


class GeometryInspectionError(ValueError):
    """Raised when an imported geometry cannot be inspected safely."""


def _role_suggestions(regions: tuple[str, ...]) -> dict[str, dict[str, object]]:
    suggestions = {}
    for region in regions:
        tokens = {
            token for token in re.split(r"[^a-z0-9]+", region.lower()) if token
        }
        matches = [
            role for role, hints in _ROLE_HINTS.items() if tokens.intersection(hints)
        ]
        suggestions[region] = {
            "role": matches[0] if len(matches) == 1 else None,
            "confidence": "name-match" if len(matches) == 1 else "ambiguous",
            "basis": sorted(tokens),
            "requires_confirmation": True,
        }
    return suggestions


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _binary_stl(path: Path) -> tuple[bool, int | None]:
    size = path.stat().st_size
    if size < 84:
        return False, None
    with path.open("rb") as stream:
        stream.seek(80)
        count = struct.unpack("<I", stream.read(4))[0]
    return size == 84 + 50 * count, count


def _binary_stl_triangles(
    path: Path, count: int
) -> Iterator[tuple[tuple[float, float, float], ...]]:
    with path.open("rb") as stream:
        stream.seek(84)
        for _ in range(count):
            record = stream.read(50)
            if len(record) != 50:
                raise GeometryInspectionError("Binary STL ended before its declared face count.")
            values = struct.unpack("<12fH", record)
            yield (
                (float(values[3]), float(values[4]), float(values[5])),
                (float(values[6]), float(values[7]), float(values[8])),
                (float(values[9]), float(values[10]), float(values[11])),
            )


def _ascii_stl_regions(path: Path) -> tuple[str, ...]:
    regions: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for raw in stream:
                stripped = raw.strip()
                if stripped.lower().startswith("solid"):
                    name = stripped[5:].strip()
                    if name:
                        regions.add(name)
    except UnicodeDecodeError as error:
        raise GeometryInspectionError(
            "STL is neither a size-consistent binary file nor valid UTF-8 ASCII."
        ) from error
    return tuple(sorted(regions))


def _ascii_stl_triangles(
    path: Path,
) -> Iterator[tuple[tuple[float, float, float], ...]]:
    vertices: list[tuple[float, float, float]] = []
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for line_number, raw in enumerate(stream, start=1):
                stripped = raw.strip()
                lower = stripped.lower()
                if lower.startswith("vertex"):
                    parts = stripped.split()
                    if len(parts) != 4:
                        raise GeometryInspectionError(
                            f"Malformed STL vertex at line {line_number}."
                        )
                    try:
                        vertex = tuple(float(value) for value in parts[1:])
                    except ValueError as error:
                        raise GeometryInspectionError(
                            f"Non-numeric STL vertex at line {line_number}."
                        ) from error
                    if any(not math.isfinite(value) for value in vertex):
                        raise GeometryInspectionError(
                            f"Non-finite STL vertex at line {line_number}."
                        )
                    vertices.append(vertex)
                    if len(vertices) == 3:
                        yield tuple(vertices)
                        vertices.clear()
    except UnicodeDecodeError as error:
        raise GeometryInspectionError(
            "STL is neither a size-consistent binary file nor valid UTF-8 ASCII."
        ) from error
    if vertices:
        raise GeometryInspectionError("ASCII STL contains an incomplete triangle.")


def _obj_metadata(
    path: Path,
) -> tuple[list[tuple[float, float, float]], tuple[str, ...], int]:
    vertices: list[tuple[float, float, float]] = []
    regions: set[str] = set()
    polygon_faces = 0
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, raw in enumerate(stream, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if parts[0] == "v":
                if len(parts) < 4:
                    raise GeometryInspectionError(
                        f"Malformed OBJ vertex at line {line_number}."
                    )
                try:
                    vertex = tuple(float(value) for value in parts[1:4])
                except ValueError as error:
                    raise GeometryInspectionError(
                        f"Non-numeric OBJ vertex at line {line_number}."
                    ) from error
                if any(not math.isfinite(value) for value in vertex):
                    raise GeometryInspectionError(
                        f"Non-finite OBJ vertex at line {line_number}."
                    )
                vertices.append(vertex)
            elif parts[0] in {"o", "g", "usemtl"} and len(parts) > 1:
                regions.add(" ".join(parts[1:]))
            elif parts[0] == "f" and len(parts) > 4:
                polygon_faces += 1
    return vertices, tuple(sorted(regions)), polygon_faces


def _obj_triangles(
    path: Path,
    vertices: list[tuple[float, float, float]],
) -> Iterator[tuple[tuple[float, float, float], ...]]:
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, raw in enumerate(stream, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if parts[0] == "f":
                if len(parts) < 4:
                    raise GeometryInspectionError(
                        f"OBJ face at line {line_number} has fewer than three vertices."
                    )
                indices = []
                for token in parts[1:]:
                    value = token.split("/", 1)[0]
                    try:
                        index = int(value)
                    except ValueError as error:
                        raise GeometryInspectionError(
                            f"Malformed OBJ face index at line {line_number}."
                        ) from error
                    resolved = index - 1 if index > 0 else len(vertices) + index
                    if not 0 <= resolved < len(vertices):
                        raise GeometryInspectionError(
                            f"OBJ face index is out of range at line {line_number}."
                        )
                    indices.append(resolved)
                for offset in range(1, len(indices) - 1):
                    yield (
                        vertices[indices[0]],
                        vertices[indices[offset]],
                        vertices[indices[offset + 1]],
                    )


def _surface_metrics(
    triangles: Iterable[tuple[tuple[float, float, float], ...]],
    *,
    topology_triangle_limit: int,
    merge_tolerance: float,
) -> dict[str, object]:
    lower = [math.inf, math.inf, math.inf]
    upper = [-math.inf, -math.inf, -math.inf]
    unique_vertices: set[tuple[float | int, float | int, float | int]] = set()
    edge_counts: dict[tuple[tuple[object, ...], tuple[object, ...]], list[int]] = {}
    triangle_count = 0
    degenerate_count = 0
    signed_volume = 0.0
    topology_complete = True
    for triangle in triangles:
        triangle_count += 1
        for vertex in triangle:
            for axis, value in enumerate(vertex):
                lower[axis] = min(lower[axis], value)
                upper[axis] = max(upper[axis], value)
        a, b, c = triangle
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        if math.sqrt(sum(value * value for value in cross)) <= 1.0e-30:
            degenerate_count += 1
        signed_volume += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            + a[1] * (b[2] * c[0] - b[0] * c[2])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
        if topology_complete and triangle_count <= topology_triangle_limit:
            keys = tuple(
                vertex
                if merge_tolerance == 0.0
                else tuple(round(value / merge_tolerance) for value in vertex)
                for vertex in triangle
            )
            unique_vertices.update(keys)
            ka, kb, kc = keys
            for left, right in ((ka, kb), (kb, kc), (kc, ka)):
                canonical = (left, right) if left <= right else (right, left)
                direction = 0 if canonical == (left, right) else 1
                counts = edge_counts.setdefault(canonical, [0, 0])
                counts[direction] += 1
        elif topology_complete:
            topology_complete = False
            unique_vertices.clear()
            edge_counts.clear()
    if triangle_count == 0:
        bounds = None
        dimensions = None
    else:
        bounds = {"minimum": lower, "maximum": upper}
        dimensions = [upper[index] - lower[index] for index in range(3)]
    boundary_edges = None
    non_manifold_edges = None
    orientation_conflicts = None
    if topology_complete:
        boundary_edges = sum(sum(counts) == 1 for counts in edge_counts.values())
        non_manifold_edges = sum(sum(counts) > 2 for counts in edge_counts.values())
        orientation_conflicts = sum(
            sum(counts) == 2 and (counts[0] == 2 or counts[1] == 2)
            for counts in edge_counts.values()
        )
    return {
        "triangle_count": triangle_count,
        "unique_vertex_count": len(unique_vertices) if topology_complete else None,
        "degenerate_triangle_count": degenerate_count,
        "bounds": bounds,
        "dimensions": dimensions,
        "topology_complete": topology_complete,
        "boundary_edge_count": boundary_edges,
        "non_manifold_edge_count": non_manifold_edges,
        "orientation_conflict_count": orientation_conflicts,
        "watertight": (
            boundary_edges == 0 and non_manifold_edges == 0
            if topology_complete and triangle_count > 0
            else None
        ),
        "signed_volume_native": signed_volume if triangle_count else None,
    }


def inspect_geometry(
    path: str | Path,
    *,
    unit: str | None = None,
    require_watertight: bool = True,
    topology_triangle_limit: int = 1_000_000,
    merge_tolerance: float = 0.0,
    boundary_roles: Mapping[str, str] | None = None,
    internal_flow: bool = False,
) -> dict[str, object]:
    """Inspect STL/OBJ geometry without modifying or tessellating the source."""

    selected = Path(path).expanduser().resolve()
    if not selected.is_file():
        raise FileNotFoundError(selected)
    if (
        isinstance(topology_triangle_limit, bool)
        or not isinstance(topology_triangle_limit, int)
        or topology_triangle_limit < 1
    ):
        raise GeometryInspectionError("Topology triangle limit must be a positive integer.")
    if (
        isinstance(merge_tolerance, bool)
        or not isinstance(merge_tolerance, (int, float))
        or not math.isfinite(merge_tolerance)
        or merge_tolerance < 0.0
    ):
        raise GeometryInspectionError(
            "Vertex merge tolerance must be a finite non-negative source-unit value."
        )
    merge_tolerance = float(merge_tolerance)
    normalized_unit = None if unit is None else str(unit).strip().lower()
    if normalized_unit is not None and normalized_unit not in _UNIT_SCALE_TO_M:
        raise GeometryInspectionError(
            "Geometry unit must be one of: m, mm, cm, um, in, ft."
        )
    suffix = selected.suffix.lower()
    format_name = _TRIANGULATED_FORMATS.get(suffix) or _CAD_FORMATS.get(suffix)
    issues: list[dict[str, str]] = []

    def issue(code: str, severity: str, message: str, repair: str) -> None:
        issues.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "repair": repair,
            }
        )

    encoding = None
    regions: tuple[str, ...] = ()
    polygon_faces = 0
    if suffix == ".stl":
        binary, declared_count = _binary_stl(selected)
        if binary:
            encoding = "binary"
            assert declared_count is not None
            triangles: Iterable[tuple[tuple[float, float, float], ...]] = (
                _binary_stl_triangles(selected, declared_count)
            )
        else:
            encoding = "ascii"
            regions = _ascii_stl_regions(selected)
            triangles = _ascii_stl_triangles(selected)
        metrics = _surface_metrics(
            triangles,
            topology_triangle_limit=topology_triangle_limit,
            merge_tolerance=merge_tolerance,
        )
    elif suffix == ".obj":
        encoding = "text"
        vertices, regions, polygon_faces = _obj_metadata(selected)
        metrics = _surface_metrics(
            _obj_triangles(selected, vertices),
            topology_triangle_limit=topology_triangle_limit,
            merge_tolerance=merge_tolerance,
        )
    else:
        metrics = {
            "triangle_count": None,
            "unique_vertex_count": None,
            "degenerate_triangle_count": None,
            "bounds": None,
            "dimensions": None,
            "topology_complete": False,
            "boundary_edge_count": None,
            "non_manifold_edge_count": None,
            "orientation_conflict_count": None,
            "watertight": None,
            "signed_volume_native": None,
        }
        if suffix in _CAD_FORMATS:
            issue(
                "CAD_TESSELLATION_REQUIRED",
                "error",
                "STEP/IGES topology is recognized but is not silently tessellated.",
                "Tessellate with controlled tolerance and named faces to STL/OBJ, then rerun geometry-check.",
            )
        else:
            issue(
                "GEOMETRY_FORMAT_UNSUPPORTED",
                "error",
                f"Unsupported imported geometry suffix {suffix or '<none>'!r}.",
                "Provide STL or OBJ for the released read-only inspector.",
            )
    if normalized_unit is None:
        issue(
            "GEOMETRY_UNIT_UNDECLARED",
            "error",
            "STL/OBJ coordinates do not carry a dependable physical unit.",
            "Rerun with an explicit --unit m, mm, cm, um, in, or ft.",
        )
    if metrics["triangle_count"] == 0:
        issue(
            "GEOMETRY_EMPTY",
            "error",
            "No surface triangles were found.",
            "Export a non-empty triangulated surface.",
        )
    if metrics["topology_complete"] is False and metrics["triangle_count"] is not None:
        issue(
            "TOPOLOGY_SCAN_LIMIT_REACHED",
            "warning",
            "Bounds were scanned, but edge topology exceeded the configured memory guard.",
            "Raise --max-topology-triangles deliberately or run OpenFOAM surfaceCheck.",
        )
    if metrics["degenerate_triangle_count"]:
        issue(
            "DEGENERATE_TRIANGLES",
            "error",
            f"Found {metrics['degenerate_triangle_count']} zero-area triangles.",
            "Repair or re-tessellate degenerate faces before meshing.",
        )
    if metrics["non_manifold_edge_count"]:
        issue(
            "NON_MANIFOLD_EDGES",
            "error",
            f"Found {metrics['non_manifold_edge_count']} edges shared by more than two faces.",
            "Repair non-manifold intersections before volume meshing.",
        )
    if metrics["boundary_edge_count"] and require_watertight:
        issue(
            "SURFACE_NOT_WATERTIGHT",
            "error",
            f"Found {metrics['boundary_edge_count']} open boundary edges.",
            "Close the fluid volume or rerun with --allow-open for an intentional open surface.",
        )
    if metrics["orientation_conflict_count"]:
        issue(
            "INCONSISTENT_FACE_ORIENTATION",
            "warning",
            f"Found {metrics['orientation_conflict_count']} same-direction shared edges.",
            "Orient connected faces consistently before using inside/outside meshing controls.",
        )
    if format_name in {"stl", "obj"} and not regions:
        issue(
            "BOUNDARY_REGIONS_UNNAMED",
            "warning",
            "No stable surface region names were discovered.",
            "Export named inlet, outlet, wall, symmetry, and interface regions.",
        )
    if polygon_faces:
        issue(
            "OBJ_POLYGONS_TRIANGULATED",
            "info",
            f"Triangulated {polygon_faces} OBJ polygon faces by a deterministic fan.",
            "Confirm that source polygons are planar and non-self-intersecting.",
        )
    if merge_tolerance > 0.0:
        issue(
            "VERTEX_MERGE_TOLERANCE_APPLIED",
            "info",
            f"Topology vertices were quantized with tolerance {merge_tolerance:g} source units.",
            "Confirm the tolerance is smaller than every physical gap that must remain open.",
        )

    scale = None if normalized_unit is None else _UNIT_SCALE_TO_M[normalized_unit]
    bounds_m = None
    dimensions_m = None
    volume_m3 = None
    if scale is not None and metrics["bounds"] is not None:
        bounds = metrics["bounds"]
        assert isinstance(bounds, dict)
        bounds_m = {
            "minimum": [value * scale for value in bounds["minimum"]],
            "maximum": [value * scale for value in bounds["maximum"]],
        }
        dimensions = metrics["dimensions"]
        assert isinstance(dimensions, list)
        dimensions_m = [value * scale for value in dimensions]
        signed_volume = metrics["signed_volume_native"]
        if isinstance(signed_volume, float) and metrics["watertight"] is True:
            volume_m3 = abs(signed_volume) * scale**3
    geometry_error_count = sum(item["severity"] == "error" for item in issues)
    geometry_ready = geometry_error_count == 0
    suggestions = _role_suggestions(regions)
    confirmed_roles: dict[str, str] | None = None
    boundary_roles_ready = False
    if boundary_roles is None:
        issue(
            "BOUNDARY_ROLES_UNCONFIRMED",
            "warning",
            "Surface regions have no explicit CFD boundary-role map.",
            "Review suggestions and pass a versioned --roles JSON map before imported meshing.",
        )
    elif not isinstance(boundary_roles, Mapping):
        issue(
            "BOUNDARY_ROLE_MAP_INVALID",
            "error",
            "Boundary roles must be a mapping from exact region name to CFD role.",
            "Provide an object whose keys match discovered region_names exactly.",
        )
    else:
        confirmed_roles = {
            str(name): str(role).strip().lower()
            for name, role in boundary_roles.items()
        }
        missing = sorted(set(regions) - set(confirmed_roles))
        unknown = sorted(set(confirmed_roles) - set(regions))
        invalid = sorted(
            name
            for name, role in confirmed_roles.items()
            if role not in _BOUNDARY_ROLES
        )
        if not regions:
            issue(
                "BOUNDARY_REGIONS_UNAVAILABLE",
                "error",
                "The surface exposes no stable names to map to CFD boundary roles.",
                "Re-export named surface regions before defining a role map.",
            )
        if missing:
            issue(
                "BOUNDARY_REGIONS_UNMAPPED",
                "error",
                "Unmapped surface regions: " + ", ".join(missing) + ".",
                "Map every discovered region explicitly; do not rely on a default wall.",
            )
        if unknown:
            issue(
                "BOUNDARY_ROLE_REGIONS_UNKNOWN",
                "error",
                "Role map contains unknown regions: " + ", ".join(unknown) + ".",
                "Use exact names from surface.region_names and remove stale entries.",
            )
        if invalid:
            issue(
                "BOUNDARY_ROLES_INVALID",
                "error",
                "Unsupported roles for regions: " + ", ".join(invalid) + ".",
                "Use inlet, outlet, wall, symmetry, periodic, interface, farfield, opening, or empty.",
            )
        role_values = set(confirmed_roles.values())
        if internal_flow and "inlet" not in role_values:
            issue(
                "INTERNAL_FLOW_INLET_MISSING",
                "error",
                "Internal-flow mapping has no inlet region.",
                "Confirm at least one region with role inlet.",
            )
        if internal_flow and "outlet" not in role_values:
            issue(
                "INTERNAL_FLOW_OUTLET_MISSING",
                "error",
                "Internal-flow mapping has no outlet region.",
                "Confirm at least one region with role outlet.",
            )
        role_error_codes = {
            "BOUNDARY_REGIONS_UNMAPPED",
            "BOUNDARY_ROLE_REGIONS_UNKNOWN",
            "BOUNDARY_ROLES_INVALID",
            "INTERNAL_FLOW_INLET_MISSING",
            "INTERNAL_FLOW_OUTLET_MISSING",
            "BOUNDARY_REGIONS_UNAVAILABLE",
        }
        boundary_roles_ready = bool(regions) and not any(
            item["code"] in role_error_codes for item in issues
        )
    tools = {
        name: shutil.which(name)
        for name in ("surfaceCheck", "snappyHexMesh", "gmsh")
    }
    return {
        "schema": "agentcfd.geometry-inspection/0.1",
        "source": {
            "path": str(selected),
            "sha256": _sha256(selected),
            "bytes": selected.stat().st_size,
            "format": format_name or "unknown",
            "encoding": encoding,
            "unit": normalized_unit,
            "scale_to_m": scale,
        },
        "surface": {
            **metrics,
            "region_names": list(regions),
            "bounds_m": bounds_m,
            "dimensions_m": dimensions_m,
            "enclosed_volume_m3": volume_m3,
        },
        "policy": {
            "require_watertight": require_watertight,
            "topology_triangle_limit": topology_triangle_limit,
            "merge_tolerance_native": merge_tolerance,
            "internal_flow": internal_flow,
        },
        "boundary_roles": {
            "allowed_roles": sorted(_BOUNDARY_ROLES),
            "suggestions": suggestions,
            "confirmed": confirmed_roles,
        },
        "tools": tools,
        "readiness": {
            "geometry_ready": geometry_ready,
            "boundary_roles_ready": boundary_roles_ready,
            "ready_for_import_setup": geometry_ready and boundary_roles_ready,
            "agentcfd_imported_mesh_lowering_available": False,
            "ready_to_mesh": False,
        },
        "issues": issues,
        "next_action": (
            {
                "kind": "repair",
                "message": next(
                    item["repair"] for item in issues if item["severity"] == "error"
                ),
            }
            if not geometry_ready
            else {
                "kind": "boundary-confirmation",
                "message": next(
                    (
                        item["repair"]
                        for item in issues
                        if item["severity"] == "error"
                        and (
                            item["code"].startswith("BOUNDARY_")
                            or item["code"].startswith("INTERNAL_FLOW_")
                        )
                    ),
                    "Confirm every surface region with a versioned --roles JSON map.",
                ),
            }
            if not boundary_roles_ready
            else {
                "kind": "provider-roadmap",
                "message": (
                    "Geometry and roles pass released preflight; imported snappyHexMesh "
                    "lowering is not yet claimed."
                ),
            }
        ),
        "observation_cost": {
            "source_bytes_hashed": selected.stat().st_size,
            "field_payloads_opened": 0,
            "external_processes_started": 0,
        },
    }


__all__ = ["GeometryInspectionError", "inspect_geometry"]
