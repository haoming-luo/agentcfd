"""Read-only, bounded-memory geometry preflight for imported CFD surfaces."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import struct
import tempfile
import unicodedata
from array import array
from pathlib import Path
from typing import Iterable, Iterator, Mapping


_Triangle = tuple[tuple[float, float, float], ...]
_TaggedTriangle = tuple[str | None, _Triangle]


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
_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class GeometryInspectionError(ValueError):
    """Raised when an imported geometry cannot be inspected safely."""


def _foam_region_base(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", ascii_name)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = "region_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    if normalized[0].isdigit():
        normalized = "region_" + normalized
    return normalized


def plan_region_normalization(path: str | Path) -> dict[str, object]:
    """Plan deterministic OpenFOAM-safe STL/OBJ region names without writing."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.lower()
    if suffix == ".stl":
        binary, _ = _binary_stl(source)
        if binary:
            raise GeometryInspectionError(
                "Binary STL does not carry stable named regions; export ASCII STL or OBJ."
            )
        encoding = "ascii"
        regions = _ascii_stl_regions(source)
        format_name = "stl"
    elif suffix == ".obj":
        encoding = "text"
        _, regions, _ = _obj_metadata(source)
        format_name = "obj"
    else:
        raise GeometryInspectionError(
            "Region normalization supports ASCII STL and OBJ only."
        )
    if not regions:
        raise GeometryInspectionError(
            "Geometry has no named regions to normalize; export named surfaces first."
        )
    bases = {name: _foam_region_base(name) for name in regions}
    base_counts = {
        base: sum(candidate == base for candidate in bases.values())
        for base in set(bases.values())
    }
    normalized = {
        name: (
            base
            if base_counts[base] == 1
            else base + "_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        )
        for name, base in bases.items()
    }
    records = [
        {
            "source": name,
            "normalized": normalized[name],
            "changed": name != normalized[name],
            "collision_resolved": base_counts[bases[name]] > 1,
        }
        for name in sorted(regions)
    ]
    return {
        "schema": "agentcfd.geometry-region-normalization/0.1",
        "source": {
            "path": str(source),
            "sha256": _sha256(source),
            "bytes": source.stat().st_size,
            "format": format_name,
            "encoding": encoding,
        },
        "region_count": len(records),
        "changed_count": sum(record["changed"] is True for record in records),
        "regions": records,
        "output": None,
        "source_modified": False,
        "geometry_coordinates_modified": False,
        "next_action": {
            "kind": "write-normalized-copy",
            "reason": "Review the exact region mapping, then choose a new output path.",
        },
    }


def normalize_geometry_regions(
    path: str | Path,
    destination: str | Path,
) -> tuple[Path, dict[str, object]]:
    """Write an atomic geometry copy with only named-region tokens changed."""

    report = plan_region_normalization(path)
    source = Path(str(report["source"]["path"]))
    target = Path(destination).expanduser().resolve()
    if target == source:
        raise GeometryInspectionError("Normalized geometry must use a new output path.")
    if target.exists():
        raise FileExistsError(f"Normalized geometry output already exists: {target}")
    if target.suffix.lower() != source.suffix.lower():
        raise GeometryInspectionError("Normalized geometry must preserve its file suffix.")
    mapping = {
        str(record["source"]): str(record["normalized"])
        for record in report["regions"]
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent
    )
    try:
        with source.open("r", encoding="utf-8", newline="") as input_stream, open(
            descriptor, "w", encoding="utf-8", newline="", closefd=True
        ) as output_stream:
            for line in input_stream:
                if source.suffix.lower() == ".stl":
                    match = re.match(
                        r"^(\s*)(solid|endsolid)(?:\s+(.*?))?(\r?\n)?$",
                        line,
                        flags=re.IGNORECASE,
                    )
                else:
                    match = re.match(r"^(\s*)([og])(?:\s+(.*?))?(\r?\n)?$", line)
                if match and match.group(3) in mapping:
                    newline = match.group(4) or ""
                    line = f"{match.group(1)}{match.group(2)} {mapping[match.group(3)]}{newline}"
                output_stream.write(line)
        temporary = Path(temporary_name)
        if source.suffix.lower() == ".stl":
            observed = _ascii_stl_regions(temporary)
        else:
            _, observed, _ = _obj_metadata(temporary)
        if set(observed) != set(mapping.values()):
            raise GeometryInspectionError(
                "Normalized geometry region verification disagrees with the plan."
            )
        temporary.replace(target)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    result = dict(report)
    result["output"] = {
        "path": str(target),
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
        "region_names": list(observed),
    }
    result["next_action"] = {
        "kind": "geometry-check",
        "reason": "Inspect units, topology, roles, and inlet direction on the normalized copy.",
    }
    return target, result


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


def accept_name_role_suggestions(
    inspection: Mapping[str, object],
) -> dict[str, str]:
    """Return an explicitly accepted, complete name-based role map.

    The caller supplies the confirmation gesture. This helper never invents a
    fallback role: every discovered region must have exactly one name match.
    """

    if inspection.get("schema") != "agentcfd.geometry-inspection/0.1":
        raise GeometryInspectionError(
            "Name-role acceptance requires an AgentCFD geometry inspection."
        )
    surface = inspection.get("surface")
    boundary_roles = inspection.get("boundary_roles")
    if not isinstance(surface, Mapping) or not isinstance(boundary_roles, Mapping):
        raise GeometryInspectionError(
            "Geometry inspection lacks surface or boundary-role records."
        )
    region_names = surface.get("region_names")
    suggestions = boundary_roles.get("suggestions")
    if not isinstance(region_names, list) or not isinstance(suggestions, Mapping):
        raise GeometryInspectionError(
            "Geometry inspection lacks stable region names or role suggestions."
        )
    accepted: dict[str, str] = {}
    unresolved: list[str] = []
    for raw_name in region_names:
        if not isinstance(raw_name, str):
            raise GeometryInspectionError(
                "Geometry inspection contains a non-string region name."
            )
        suggestion = suggestions.get(raw_name)
        role = suggestion.get("role") if isinstance(suggestion, Mapping) else None
        confidence = (
            suggestion.get("confidence")
            if isinstance(suggestion, Mapping)
            else None
        )
        if (
            not isinstance(role, str)
            or role not in _BOUNDARY_ROLES
            or confidence != "name-match"
        ):
            unresolved.append(raw_name)
            continue
        accepted[raw_name] = str(role)
    if unresolved or not accepted:
        detail = ", ".join(unresolved) if unresolved else "no named regions"
        raise GeometryInspectionError(
            "Cannot accept name-based roles because these regions are ambiguous: "
            + detail
            + ". Provide an explicit versioned role map instead."
        )
    return accepted


def assess_inlet_velocity_direction(
    inspection: Mapping[str, object],
    velocity_m_s: tuple[float, float, float],
    *,
    minimum_alignment: float = 1.0e-8,
) -> dict[str, object]:
    """Assess whether a Cartesian velocity points into confirmed inlet regions.

    Outward direction is derived only for a watertight, consistently oriented
    surface. Curved or otherwise ambiguous inlet normals remain indeterminate
    rather than being guessed.
    """

    if inspection.get("schema") != "agentcfd.geometry-inspection/0.1":
        raise GeometryInspectionError(
            "Inlet direction assessment requires an AgentCFD geometry inspection."
        )
    if (
        isinstance(minimum_alignment, bool)
        or not isinstance(minimum_alignment, (int, float))
        or not math.isfinite(minimum_alignment)
        or not 0.0 <= minimum_alignment < 1.0
    ):
        raise GeometryInspectionError(
            "Minimum inlet alignment must be finite and in the interval [0, 1)."
        )
    try:
        velocity = tuple(float(value) for value in velocity_m_s)
    except (TypeError, ValueError) as error:
        raise GeometryInspectionError(
            "Inlet velocity direction requires three finite Cartesian components."
        ) from error
    if len(velocity) != 3 or any(not math.isfinite(value) for value in velocity):
        raise GeometryInspectionError(
            "Inlet velocity direction requires three finite Cartesian components."
        )
    speed = math.sqrt(sum(value * value for value in velocity))
    if speed <= 0.0:
        raise GeometryInspectionError("Inlet velocity direction cannot be zero.")

    surface = inspection.get("surface")
    role_record = inspection.get("boundary_roles")
    reason: str | None = None
    if not isinstance(surface, Mapping) or not isinstance(role_record, Mapping):
        reason = "Inspection lacks surface or confirmed boundary-role evidence."
    elif surface.get("watertight") is not True:
        reason = "Outward direction is indeterminate because the surface is not watertight."
    elif surface.get("orientation_conflict_count") != 0:
        reason = "Outward direction is indeterminate because face orientations conflict."
    signed_volume = surface.get("signed_volume_native") if reason is None else None
    if reason is None and (
        not isinstance(signed_volume, (int, float))
        or isinstance(signed_volume, bool)
        or not math.isfinite(float(signed_volume))
        or abs(float(signed_volume)) <= 1.0e-30
    ):
        reason = "Outward direction is indeterminate because signed volume is zero."
    confirmed = role_record.get("confirmed") if isinstance(role_record, Mapping) else None
    if reason is None and not isinstance(confirmed, Mapping):
        reason = "Inlet direction requires explicitly confirmed boundary roles."
    inlet_names = (
        sorted(name for name, role in confirmed.items() if role == "inlet")
        if isinstance(confirmed, Mapping)
        else []
    )
    if reason is None and not inlet_names:
        reason = "Inlet direction requires at least one confirmed inlet region."
    metrics = surface.get("region_metrics") if isinstance(surface, Mapping) else None
    if reason is None and not isinstance(metrics, Mapping):
        reason = "Inspection lacks named-region area and normal metrics."

    regions: list[dict[str, object]] = []
    orientation_sign = 1.0 if isinstance(signed_volume, (int, float)) and signed_volume > 0 else -1.0
    if reason is None:
        assert isinstance(metrics, Mapping)
        for name in inlet_names:
            metric = metrics.get(name)
            normal = metric.get("mean_unit_normal") if isinstance(metric, Mapping) else None
            coherence = metric.get("normal_coherence") if isinstance(metric, Mapping) else None
            if (
                not isinstance(normal, list)
                or len(normal) != 3
                or not isinstance(coherence, (int, float))
                or isinstance(coherence, bool)
                or float(coherence) < 0.95
            ):
                reason = (
                    f"Inlet region {name!r} has no reliable single normal direction."
                )
                regions.clear()
                break
            outward = [orientation_sign * float(value) for value in normal]
            inward_speed = -sum(
                velocity[index] * outward[index] for index in range(3)
            )
            alignment = inward_speed / speed
            regions.append(
                {
                    "name": name,
                    "outward_unit_normal": outward,
                    "inward_normal_velocity_m_s": inward_speed,
                    "alignment_cosine": alignment,
                    "passed": alignment > float(minimum_alignment),
                }
            )

    status = (
        "indeterminate"
        if reason is not None
        else "passed"
        if all(bool(region["passed"]) for region in regions)
        else "failed"
    )
    if status == "passed":
        reason = "Velocity has a positive inward-normal component on every inlet."
    elif status == "failed":
        failed = ", ".join(
            str(region["name"]) for region in regions if not region["passed"]
        )
        reason = (
            "Velocity does not point into the confirmed inlet region(s): "
            + failed
            + ". Reverse or correct the Cartesian inlet vector."
        )
    assert reason is not None
    return {
        "schema": "agentcfd.inlet-direction-assessment/0.1",
        "status": status,
        "velocity_m_s": list(velocity),
        "speed_m_s": speed,
        "minimum_alignment": float(minimum_alignment),
        "orientation_basis": (
            "signed-enclosed-volume" if status != "indeterminate" else None
        ),
        "regions": regions,
        "reason": reason,
    }


def validate_inlet_velocity_direction(
    inspection: Mapping[str, object],
    velocity_m_s: tuple[float, float, float],
) -> dict[str, object]:
    """Reject a reliably reversed velocity and return auditable direction evidence."""

    assessment = assess_inlet_velocity_direction(inspection, velocity_m_s)
    if assessment["status"] == "failed":
        raise GeometryInspectionError(str(assessment["reason"]))
    return assessment


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
) -> Iterator[_TaggedTriangle]:
    with path.open("rb") as stream:
        stream.seek(84)
        for _ in range(count):
            record = stream.read(50)
            if len(record) != 50:
                raise GeometryInspectionError("Binary STL ended before its declared face count.")
            values = struct.unpack("<12fH", record)
            yield (
                None,
                (
                    (float(values[3]), float(values[4]), float(values[5])),
                    (float(values[6]), float(values[7]), float(values[8])),
                    (float(values[9]), float(values[10]), float(values[11])),
                ),
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
) -> Iterator[_TaggedTriangle]:
    vertices: list[tuple[float, float, float]] = []
    region: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for line_number, raw in enumerate(stream, start=1):
                stripped = raw.strip()
                lower = stripped.lower()
                if lower.startswith("solid"):
                    selected = stripped[5:].strip()
                    region = selected or None
                elif lower.startswith("endsolid"):
                    region = None
                elif lower.startswith("vertex"):
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
                        yield region, tuple(vertices)
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
) -> Iterator[_TaggedTriangle]:
    region: str | None = None
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, raw in enumerate(stream, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if parts[0] in {"o", "g", "usemtl"} and len(parts) > 1:
                region = " ".join(parts[1:])
            elif parts[0] == "f":
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
                        region,
                        (
                            vertices[indices[0]],
                            vertices[indices[offset]],
                            vertices[indices[offset + 1]],
                        ),
                    )


def _surface_metrics(
    triangles: Iterable[_TaggedTriangle],
    *,
    topology_triangle_limit: int,
    merge_tolerance: float,
) -> dict[str, object]:
    lower = [math.inf, math.inf, math.inf]
    upper = [-math.inf, -math.inf, -math.inf]
    unique_vertices: set[tuple[float | int, float | int, float | int]] = set()
    edge_counts: dict[tuple[tuple[object, ...], tuple[object, ...]], list[int]] = {}
    component_parents = array("q")
    component_ranks = bytearray()
    component_regions: list[str | None] = []
    component_areas = array("d")
    component_signed_volumes = array("d")

    def component_root(index: int) -> int:
        while component_parents[index] != index:
            component_parents[index] = component_parents[component_parents[index]]
            index = component_parents[index]
        return index

    def connect_components(left: int, right: int) -> None:
        left_root = component_root(left)
        right_root = component_root(right)
        if left_root == right_root:
            return
        if component_ranks[left_root] < component_ranks[right_root]:
            left_root, right_root = right_root, left_root
        component_parents[right_root] = left_root
        if component_ranks[left_root] == component_ranks[right_root]:
            component_ranks[left_root] += 1
    triangle_count = 0
    degenerate_count = 0
    signed_volume = 0.0
    topology_complete = True
    region_accumulators: dict[str, dict[str, object]] = {}
    for region, triangle in triangles:
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
        cross_magnitude = math.sqrt(sum(value * value for value in cross))
        area = 0.5 * cross_magnitude
        if cross_magnitude <= 1.0e-30:
            degenerate_count += 1
        if region is not None:
            accumulator = region_accumulators.setdefault(
                region,
                {
                    "triangle_count": 0,
                    "area_native": 0.0,
                    "area_vector_native": [0.0, 0.0, 0.0],
                    "centroid_moment_native": [0.0, 0.0, 0.0],
                },
            )
            accumulator["triangle_count"] = int(accumulator["triangle_count"]) + 1
            accumulator["area_native"] = float(accumulator["area_native"]) + area
            area_vector = accumulator["area_vector_native"]
            centroid_moment = accumulator["centroid_moment_native"]
            assert isinstance(area_vector, list)
            assert isinstance(centroid_moment, list)
            for axis in range(3):
                area_vector[axis] += 0.5 * cross[axis]
                centroid_moment[axis] += (
                    area * sum(vertex[axis] for vertex in triangle) / 3.0
                )
        triangle_signed_volume = (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            + a[1] * (b[2] * c[0] - b[0] * c[2])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
        signed_volume += triangle_signed_volume
        if topology_complete and triangle_count <= topology_triangle_limit:
            triangle_index = triangle_count - 1
            component_parents.append(triangle_index)
            component_ranks.append(0)
            component_regions.append(region)
            component_areas.append(area)
            component_signed_volumes.append(triangle_signed_volume)
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
                counts = edge_counts.setdefault(canonical, [0, 0, triangle_index])
                connect_components(triangle_index, counts[2])
                counts[direction] += 1
        elif topology_complete:
            topology_complete = False
            unique_vertices.clear()
            edge_counts.clear()
            component_parents.clear()
            component_ranks.clear()
            component_regions.clear()
            component_areas.clear()
            component_signed_volumes.clear()
    if triangle_count == 0:
        bounds = None
        dimensions = None
    else:
        bounds = {"minimum": lower, "maximum": upper}
        dimensions = [upper[index] - lower[index] for index in range(3)]
    boundary_edges = None
    non_manifold_edges = None
    orientation_conflicts = None
    connected_component_count = None
    connected_components = None
    if topology_complete:
        boundary_edges = sum(
            counts[0] + counts[1] == 1 for counts in edge_counts.values()
        )
        non_manifold_edges = sum(
            counts[0] + counts[1] > 2 for counts in edge_counts.values()
        )
        orientation_conflicts = sum(
            counts[0] + counts[1] == 2
            and (counts[0] == 2 or counts[1] == 2)
            for counts in edge_counts.values()
        )
        groups: dict[int, dict[str, object]] = {}
        total_area = sum(component_areas)
        for index, region in enumerate(component_regions):
            area = component_areas[index]
            triangle_volume = component_signed_volumes[index]
            root = component_root(index)
            group = groups.setdefault(
                root,
                {
                    "first_triangle_index": index,
                    "triangle_count": 0,
                    "area_native": 0.0,
                    "signed_volume_native": 0.0,
                    "region_names": set(),
                },
            )
            group["triangle_count"] = int(group["triangle_count"]) + 1
            group["area_native"] = float(group["area_native"]) + area
            group["signed_volume_native"] = (
                float(group["signed_volume_native"]) + triangle_volume
            )
            if region is not None:
                region_names = group["region_names"]
                assert isinstance(region_names, set)
                region_names.add(region)
        ordered_groups = sorted(
            groups.values(),
            key=lambda group: (
                -int(group["triangle_count"]),
                -float(group["area_native"]),
                tuple(sorted(group["region_names"])),
                int(group["first_triangle_index"]),
            ),
        )
        connected_components = []
        for position, group in enumerate(ordered_groups, start=1):
            area = float(group["area_native"])
            connected_components.append(
                {
                    "id": f"component-{position:04d}",
                    "triangle_count": int(group["triangle_count"]),
                    "area_native": area,
                    "area_fraction": area / total_area if total_area > 0.0 else None,
                    "signed_volume_native": float(group["signed_volume_native"]),
                    "region_names": sorted(group["region_names"]),
                }
            )
        connected_component_count = len(connected_components)
    region_metrics: dict[str, dict[str, object]] = {}
    for name, accumulator in sorted(region_accumulators.items()):
        area = float(accumulator["area_native"])
        area_vector = accumulator["area_vector_native"]
        centroid_moment = accumulator.pop("centroid_moment_native")
        assert isinstance(area_vector, list)
        assert isinstance(centroid_moment, list)
        vector_magnitude = math.sqrt(sum(value * value for value in area_vector))
        region_metrics[name] = {
            **accumulator,
            "centroid_native": (
                [value / area for value in centroid_moment] if area > 0.0 else None
            ),
            "mean_unit_normal": (
                [value / vector_magnitude for value in area_vector]
                if vector_magnitude > 0.0
                else None
            ),
            "normal_coherence": (
                min(1.0, vector_magnitude / area) if area > 0.0 else None
            ),
        }
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
        "connected_component_count": connected_component_count,
        "connected_components": connected_components,
        "watertight": (
            boundary_edges == 0 and non_manifold_edges == 0
            if topology_complete and triangle_count > 0
            else None
        ),
        "signed_volume_native": signed_volume if triangle_count else None,
        "region_metrics": region_metrics,
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
            "connected_component_count": None,
            "connected_components": None,
            "watertight": None,
            "signed_volume_native": None,
            "region_metrics": {},
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
    if (
        isinstance(metrics["connected_component_count"], int)
        and metrics["connected_component_count"] > 1
    ):
        issue(
            "DISCONNECTED_SURFACE_COMPONENTS",
            "warning",
            f"Found {metrics['connected_component_count']} edge-connected surface components.",
            "Review component sizes and regions; remove accidental debris or explicitly confirm intentional internal shells and baffles.",
        )
    if format_name in {"stl", "obj"} and not regions:
        issue(
            "BOUNDARY_REGIONS_UNNAMED",
            "warning",
            "No stable surface region names were discovered.",
            "Export named inlet, outlet, wall, symmetry, and interface regions.",
        )
    unsafe_regions = sorted(name for name in regions if _FOAM_WORD.fullmatch(name) is None)
    if unsafe_regions:
        issue(
            "BOUNDARY_REGION_NAMES_UNSAFE",
            "error",
            "Surface region names are not safe OpenFOAM words: "
            + ", ".join(unsafe_regions)
            + ".",
            "Preview `agentcfd geometry-normalize SOURCE`, then write and inspect a new normalized copy.",
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
    region_metrics = metrics["region_metrics"]
    assert isinstance(region_metrics, dict)
    scaled_region_metrics: dict[str, dict[str, object]] = {}
    for name, raw_metric in region_metrics.items():
        assert isinstance(raw_metric, dict)
        area_native = raw_metric["area_native"]
        centroid_native = raw_metric["centroid_native"]
        area_vector_native = raw_metric["area_vector_native"]
        scaled_region_metrics[name] = {
            **raw_metric,
            "area_m2": (
                None if scale is None else float(area_native) * scale**2
            ),
            "centroid_m": (
                None
                if scale is None or centroid_native is None
                else [float(value) * scale for value in centroid_native]
            ),
            "area_vector_m2": (
                None
                if scale is None
                else [float(value) * scale**2 for value in area_vector_native]
            ),
        }
    connected_components = metrics["connected_components"]
    scaled_connected_components = None
    if isinstance(connected_components, list):
        scaled_connected_components = []
        for raw_component in connected_components:
            assert isinstance(raw_component, dict)
            scaled_connected_components.append(
                {
                    **raw_component,
                    "area_m2": (
                        None
                        if scale is None
                        else float(raw_component["area_native"]) * scale**2
                    ),
                    "signed_volume_m3": (
                        None
                        if scale is None
                        else float(raw_component["signed_volume_native"]) * scale**3
                    ),
                }
            )
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
        flow_regions_without_faces = sorted(
            name
            for name, role in confirmed_roles.items()
            if role in {"inlet", "outlet"}
            and (
                name not in scaled_region_metrics
                or float(scaled_region_metrics[name]["area_native"]) <= 0.0
            )
        )
        if flow_regions_without_faces:
            issue(
                "FLOW_BOUNDARY_REGION_EMPTY",
                "error",
                "Flow boundary regions contain no positive-area faces: "
                + ", ".join(flow_regions_without_faces)
                + ".",
                "Re-export inlet and outlet as named regions containing surface faces.",
            )
        nonplanar_flow_regions = sorted(
            name
            for name, role in confirmed_roles.items()
            if role in {"inlet", "outlet"}
            and name in scaled_region_metrics
            and isinstance(scaled_region_metrics[name]["normal_coherence"], float)
            and float(scaled_region_metrics[name]["normal_coherence"]) < 0.95
        )
        if nonplanar_flow_regions:
            issue(
                "FLOW_BOUNDARY_NORMALS_DIVERGE",
                "warning",
                "Flow boundary face normals are not nearly parallel: "
                + ", ".join(nonplanar_flow_regions)
                + ".",
                "Confirm that each inlet/outlet is an intentional curved opening or re-export a planar cap.",
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
            "FLOW_BOUNDARY_REGION_EMPTY",
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
            "region_metrics": scaled_region_metrics,
            "connected_components": scaled_connected_components,
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
            "agentcfd_imported_mesh_lowering_available": True,
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
                "kind": "model-setup",
                "message": (
                    "Create ImportedSurface model intent with an explicit interior point "
                    "and automatic mesh budget, then run agentcfd mesh --plan-only."
                ),
            }
        ),
        "observation_cost": {
            "source_bytes_hashed": selected.stat().st_size,
            "field_payloads_opened": 0,
            "external_processes_started": 0,
        },
    }


__all__ = [
    "GeometryInspectionError",
    "accept_name_role_suggestions",
    "assess_inlet_velocity_direction",
    "inspect_geometry",
    "normalize_geometry_regions",
    "plan_region_normalization",
    "validate_inlet_velocity_direction",
]
