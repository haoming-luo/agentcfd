"""Portable field bundles for visualization, coupling, and learned workflows.

XDMF/HDF5 is the durable mesh-and-field representation.  NPZ mirrors the same
arrays without Python pickles so NumPy, PyTorch, JAX, and dataset tooling can
consume a bundle without understanding an OpenFOAM case directory.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import AgentCFDError, ProviderUnavailableError
from .provenance import file_sha256


@dataclass(frozen=True, slots=True)
class FieldSemantic:
    canonical_name: str
    unit: str
    components: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class FieldBundle:
    directory: Path
    xdmf: Path
    hdf5: Path
    npz: Path
    manifest: Path
    frame_count: int
    times: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.field-bundle-export/0.1",
            "directory": str(self.directory),
            "xdmf": str(self.xdmf),
            "hdf5": str(self.hdf5),
            "npz": str(self.npz),
            "manifest": str(self.manifest),
            "frame_count": self.frame_count,
            "times": list(self.times),
        }


_OPENFOAM_FIELDS = {
    "U": FieldSemantic(
        "fluid.velocity",
        "m/s",
        ("x", "y", "z"),
        "Velocity vector.",
    ),
    "p": FieldSemantic(
        "fluid.kinematic_pressure",
        "m^2/s^2",
        description="OpenFOAM incompressible kinematic pressure.",
    ),
    "k": FieldSemantic(
        "turbulence.kinetic_energy",
        "m^2/s^2",
        description="Turbulent kinetic energy per unit mass.",
    ),
    "omega": FieldSemantic(
        "turbulence.specific_dissipation_rate",
        "1/s",
        description="Specific turbulent dissipation rate.",
    ),
    "epsilon": FieldSemantic(
        "turbulence.dissipation_rate",
        "m^2/s^3",
        description="Turbulent kinetic-energy dissipation rate.",
    ),
    "nut": FieldSemantic(
        "turbulence.kinematic_eddy_viscosity",
        "m^2/s",
        description="Kinematic eddy viscosity.",
    ),
    "T": FieldSemantic(
        "thermal.temperature",
        "K",
        description="Absolute temperature.",
    ),
    "vorticity": FieldSemantic(
        "fluid.vorticity",
        "1/s",
        ("x", "y", "z"),
        "Curl of the velocity field.",
    ),
    "Q": FieldSemantic(
        "fluid.q_criterion",
        "1/s^2",
        description="Second invariant used to identify rotation-dominated regions.",
    ),
}


def _io_modules():
    try:
        import h5py  # noqa: F401
        import meshio
        import numpy as np
    except ImportError as error:
        raise AgentCFDError(
            "Field export requires the optional I/O dependencies. "
            "Install them with `python -m pip install 'agentcfd[io]'`."
        ) from error
    return meshio, np


def io_available() -> bool:
    """Return whether the optional portable-field stack can be imported."""

    try:
        _io_modules()
    except AgentCFDError:
        return False
    return True


def _time_from_vtu(path: Path) -> float:
    series_path = path.parent.parent / "case.vtm.series"
    if series_path.is_file():
        try:
            series = json.loads(series_path.read_text(encoding="utf-8"))
            records = series["files"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Malformed OpenFOAM VTK series index: {series_path}.") from error
        expected_name = f"{path.parent.name}.vtm"
        matches = [record.get("time") for record in records if record.get("name") == expected_name]
        if len(matches) != 1:
            raise ValueError(
                f"OpenFOAM VTK series must map {expected_name!r} to exactly one time."
            )
        try:
            value = float(matches[0])
        except (TypeError, ValueError) as error:
            raise ValueError(f"OpenFOAM VTK time for {expected_name!r} is invalid.") from error
        if not math.isfinite(value):
            raise ValueError(f"OpenFOAM VTK time for {expected_name!r} must be finite.")
        return value
    name = path.parent.name
    if not name.startswith("case_"):
        raise ValueError(f"Cannot recover OpenFOAM time from {path}.")
    try:
        value = float(name[5:])
    except ValueError as error:
        raise ValueError(f"OpenFOAM VTK time directory is malformed: {name!r}.") from error
    if not math.isfinite(value):
        raise ValueError(f"OpenFOAM VTK time must be finite: {name!r}.")
    return value


def openfoam_vtu_series(case_directory: str | Path) -> tuple[Path, ...]:
    """Discover an ordered internal-field VTU series produced by foamToVTK."""

    root = Path(case_directory)
    records = [
        (_time_from_vtu(path), path)
        for path in (root / "VTK").glob("case_*/internal.vtu")
        if path.is_file()
    ]
    records.sort(key=lambda item: item[0])
    if not records:
        raise FileNotFoundError(
            f"No foamToVTK internal-field series found below {root / 'VTK'}."
        )
    times = [time for time, _ in records]
    if len(set(times)) != len(times):
        raise ValueError("OpenFOAM VTK series contains duplicate time values.")
    return tuple(path for _, path in records)


def convert_openfoam_fields(
    case_directory: str | Path,
    *,
    container_image: str | None = None,
    timeout_seconds: float = 3600.0,
) -> tuple[Path, ...]:
    """Run foamToVTK without a shell and return the resulting internal series."""

    root = Path(case_directory).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Field-export timeout must be a positive finite number.")
    if container_image:
        docker = shutil.which("docker")
        if docker is None:
            raise ProviderUnavailableError("Container field export requires docker on PATH.")
        cidfile = root / ".agentcfd-foamToVTK.cid"
        if cidfile.exists():
            raise AgentCFDError(
                f"Field export found a stale container identity file: {cidfile}"
            )
        argv = [
            docker,
            "run",
            "--rm",
            "--cidfile",
            str(cidfile),
            "-v",
            f"{root}:/case",
            "-w",
            "/case",
            str(container_image),
            "foamToVTK",
            "-case",
            "/case",
            "-no-boundary",
        ]
    else:
        docker = None
        cidfile = None
        converter = shutil.which("foamToVTK")
        if converter is None:
            raise ProviderUnavailableError(
                "Field export requires foamToVTK on PATH or --container-image."
            )
        argv = [converter, "-case", str(root), "-no-boundary"]
    log = root / "log.foamToVTK"
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or ""
        stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else error.stderr or ""
        stopped = ""
        if docker is not None and cidfile is not None and cidfile.is_file():
            container_id = cidfile.read_text(encoding="utf-8").strip()
            if container_id:
                stop = subprocess.run(
                    [docker, "rm", "--force", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                stopped = f"\nStopped timed-out container {container_id}: {stop.stdout}{stop.stderr}"
        log.write_text(stdout + stderr + stopped, encoding="utf-8")
        raise AgentCFDError(
            f"foamToVTK exceeded {timeout_seconds:g} seconds; see {log}."
        ) from error
    finally:
        if cidfile is not None and cidfile.is_file():
            cidfile.unlink()
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise AgentCFDError(
            f"foamToVTK failed with exit code {completed.returncode}; see {log}."
        )
    return openfoam_vtu_series(root)


def _semantic(source_name: str) -> FieldSemantic:
    return _OPENFOAM_FIELDS.get(
        source_name,
        FieldSemantic(
            f"provider.openfoam.{source_name}",
            "unknown",
            description="Provider-native field without a registered canonical mapping.",
        ),
    )


def _array_key(*parts: object) -> str:
    return "__".join(str(part).replace(".", "_") for part in parts)


def _canonical_data(
    mesh: Any,
    *,
    density: float | None,
    associations: tuple[str, ...],
    selected_fields: tuple[str, ...] | None,
):
    _, np = _io_modules()
    point_data: dict[str, Any] = {}
    cell_data: dict[str, list[Any]] = {}
    fields: list[dict[str, object]] = []
    for association, source in (("point", mesh.point_data), ("cell", mesh.cell_data)):
        if association not in associations:
            continue
        for source_name, raw_values in sorted(source.items()):
            semantic = _semantic(source_name)
            name = f"{semantic.canonical_name}.{association}"
            values = (
                np.asarray(raw_values)
                if association == "point"
                else [np.asarray(value) for value in raw_values]
            )
            base_selected = selected_fields is None or any(
                selector in {source_name, semantic.canonical_name, name}
                for selector in selected_fields
            )
            pressure_name = f"fluid.pressure.{association}"
            pressure_selected = selected_fields is None or any(
                selector in {"p", "fluid.pressure", pressure_name}
                for selector in selected_fields
            )
            if not base_selected and not (
                source_name == "p" and density is not None and pressure_selected
            ):
                continue
            if association == "point":
                if values.shape[0] != len(mesh.points):
                    raise ValueError(f"Point field {source_name!r} has inconsistent length.")
                if not np.all(np.isfinite(values)):
                    raise ValueError(f"Point field {source_name!r} contains non-finite values.")
                shape = list(values.shape[1:])
            else:
                if len(values) != len(mesh.cells) or any(
                    block.shape[0] != len(cells.data)
                    for block, cells in zip(values, mesh.cells)
                ):
                    raise ValueError(f"Cell field {source_name!r} has inconsistent blocks.")
                if any(not np.all(np.isfinite(block)) for block in values):
                    raise ValueError(f"Cell field {source_name!r} contains non-finite values.")
                shape = list(values[0].shape[1:]) if values else []
            if base_selected:
                if association == "point":
                    point_data[name] = values
                else:
                    cell_data[name] = values
                fields.append(
                    {
                        "name": semantic.canonical_name,
                        "export_name": name,
                        "source_name": source_name,
                        "association": association,
                        "unit": semantic.unit,
                        "components": list(semantic.components),
                        "shape": shape,
                        "description": semantic.description,
                        "processing": (
                            "OpenFOAM cell-to-point interpolation"
                            if association == "point"
                            else "OpenFOAM native cell field"
                        ),
                    }
                )
            if source_name == "p" and density is not None and pressure_selected:
                pressure_values = (
                    values * density
                    if association == "point"
                    else [value * density for value in values]
                )
                if association == "point":
                    point_data[pressure_name] = pressure_values
                else:
                    cell_data[pressure_name] = pressure_values
                fields.append(
                    {
                        "name": "fluid.pressure",
                        "export_name": pressure_name,
                        "source_name": "p",
                        "association": association,
                        "unit": "Pa",
                        "components": [],
                        "shape": shape,
                        "description": "Static gauge pressure derived from kinematic pressure.",
                        "processing": f"multiplied by constant density {density:.17g} kg/m^3",
                    }
                )
    return point_data, cell_data, fields


def _source_context(case_directory: Path) -> dict[str, object]:
    context: dict[str, object] = {
        "provider": "openfoam",
        "case_directory": str(case_directory.resolve()),
    }
    result_path = case_directory / "agentcfd-result.json"
    if not result_path.is_file():
        return context
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return context
    for key in ("accepted", "trust_level", "provider"):
        if key in result:
            context[key] = result[key]
    provenance = result.get("provenance")
    if isinstance(provenance, dict):
        for key in ("model_sha256", "case_sha256", "mesh_sha256"):
            if key in provenance:
                context[key] = provenance[key]
    return context


def _density_from_result(case_directory: Path) -> float | None:
    result_path = case_directory / "agentcfd-result.json"
    if not result_path.is_file():
        return None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        record = payload["scientific_inputs"]["record"]
        density = float(record["model"]["fluid"]["density"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return density if math.isfinite(density) and density > 0 else None


def _axis_from_result(case_directory: Path) -> dict[str, object]:
    result_path = case_directory / "agentcfd-result.json"
    if result_path.is_file():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            procedure = payload["scientific_inputs"]["record"]["procedure"]
            procedure_type = procedure["type"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            procedure_type = None
        if procedure_type == "steady":
            return {
                "name": "solver_iteration",
                "unit": "1",
                "physical_time": False,
                "description": "Steady-solver iteration; not physical transient time.",
            }
        if procedure_type == "transient":
            return {
                "name": "time",
                "unit": "s",
                "physical_time": True,
                "description": "Physical simulation time in SI seconds.",
            }
    return {
        "name": "provider_step",
        "unit": "provider-native",
        "physical_time": False,
        "description": (
            "Ordered provider output coordinate; no physical-time claim is available "
            "without a linked SimulationResult."
        ),
    }


def export_vtu_series(
    vtu_files: Iterable[str | Path],
    output_directory: str | Path,
    *,
    case_directory: str | Path | None = None,
    density: float | None = None,
    axis: Mapping[str, object] | None = None,
    source: Mapping[str, object] | None = None,
    profile: str = "both",
    fields: Iterable[str] | None = None,
) -> FieldBundle:
    """Write one canonical XDMF/HDF5/NPZ bundle from a fixed-mesh VTU series."""

    meshio, np = _io_modules()
    files = tuple(Path(path) for path in vtu_files)
    if not files:
        raise ValueError("At least one VTU frame is required.")
    times = tuple(_time_from_vtu(path) for path in files)
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("VTU frames must have strictly increasing time values.")
    if density is not None:
        density = float(density)
        if not math.isfinite(density) or density <= 0:
            raise ValueError("Density must be a positive finite number.")
    profile_associations = {
        "visualization": ("point",),
        "native": ("cell",),
        "both": ("point", "cell"),
    }
    try:
        associations = profile_associations[profile]
    except KeyError as error:
        raise ValueError(
            "Field-output profile must be 'visualization', 'native', or 'both'."
        ) from error
    selected_fields = None if fields is None else tuple(str(name).strip() for name in fields)
    if selected_fields is not None and (
        not selected_fields
        or any(not name for name in selected_fields)
        or len(set(selected_fields)) != len(selected_fields)
    ):
        raise ValueError("Selected fields must be a non-empty sequence of unique names.")
    source_root = Path(case_directory) if case_directory is not None else files[0].parents[2]
    axis_record = dict(axis or _axis_from_result(source_root))
    required_axis = {"name", "unit", "physical_time", "description"}
    if set(axis_record) != required_axis:
        raise ValueError("Field axis must define name, unit, physical_time, and description.")
    if not isinstance(axis_record["physical_time"], bool):
        raise ValueError("Field-axis physical_time must be a boolean.")

    target = Path(output_directory)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"Field-bundle directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    xdmf_path = target / "fields.xdmf"
    hdf5_path = target / "fields.h5"
    npz_path = target / "fields.npz"
    manifest_path = target / "manifest.json"

    first = meshio.read(files[0])
    points = np.asarray(first.points)
    if points.ndim != 2 or not 1 <= points.shape[1] <= 3 or points.shape[0] == 0:
        raise ValueError("Field bundle requires a non-empty 1-D, 2-D, or 3-D point geometry.")
    if not np.all(np.isfinite(points)):
        raise ValueError("Field-bundle point geometry contains non-finite values.")
    cells = first.cells
    if not cells:
        raise ValueError("Field bundle requires at least one cell block.")
    topology = [(block.type, np.asarray(block.data)) for block in cells]
    for cell_type, data in topology:
        if data.ndim != 2 or data.shape[0] == 0:
            raise ValueError(f"Cell block {cell_type!r} is empty or malformed.")
        if data.dtype.kind not in {"i", "u"}:
            raise ValueError(f"Cell block {cell_type!r} connectivity must be integral.")
        if int(data.min()) < 0 or int(data.max()) >= len(points):
            raise ValueError(f"Cell block {cell_type!r} references an invalid point index.")
    point_frames: dict[str, list[Any]] = {}
    cell_frames: dict[str, list[list[Any]]] = {}
    field_records: list[dict[str, object]] | None = None

    import h5py

    writer = meshio.xdmf.TimeSeriesWriter(xdmf_path)
    writer.h5_filename = str(hdf5_path)
    writer.h5_file = h5py.File(hdf5_path, "w")
    try:
        writer.write_points_cells(points, cells)
        for index, (time, path) in enumerate(zip(times, files)):
            mesh = first if index == 0 else meshio.read(path)
            if not np.array_equal(points, np.asarray(mesh.points)):
                raise ValueError("XDMF time series requires one unchanged point geometry.")
            current_topology = [(block.type, np.asarray(block.data)) for block in mesh.cells]
            if len(current_topology) != len(topology) or any(
                left_type != right_type or not np.array_equal(left, right)
                for (left_type, left), (right_type, right) in zip(topology, current_topology)
            ):
                raise ValueError("XDMF time series requires one unchanged cell topology.")
            point_data, cell_data, records = _canonical_data(
                mesh,
                density=density,
                associations=associations,
                selected_fields=selected_fields,
            )
            if field_records is None:
                if selected_fields is not None:
                    matched = {
                        selector
                        for selector in selected_fields
                        if any(
                            selector
                            in {
                                record["name"],
                                record["source_name"],
                                record["export_name"],
                            }
                            for record in records
                        )
                    }
                    missing = sorted(set(selected_fields) - matched)
                    if missing:
                        raise ValueError(
                            f"Requested portable fields are unavailable: {missing}."
                        )
                if not records:
                    raise ValueError("Field-output selection produced no portable fields.")
                field_records = records
            elif [record["export_name"] for record in records] != [
                record["export_name"] for record in field_records
            ]:
                raise ValueError("All frames must expose the same canonical fields.")
            writer.write_data(time, point_data=point_data, cell_data=cell_data)
            for name, values in point_data.items():
                point_frames.setdefault(name, []).append(np.asarray(values))
            for name, blocks in cell_data.items():
                cell_frames.setdefault(name, []).append([np.asarray(block) for block in blocks])
    except BaseException:
        writer.h5_file.close()
        raise
    else:
        writer.__exit__(None, None, None)

    with h5py.File(hdf5_path, "a") as h5:
        h5.attrs["agentcae_schema"] = "agentcae.field-bundle"
        h5.attrs["schema_version"] = "0.1.0"
        h5.attrs["axis_name"] = str(axis_record["name"])
        h5.attrs["axis_unit"] = str(axis_record["unit"])
        h5.attrs["axis_is_physical_time"] = bool(axis_record["physical_time"])
        h5.attrs["point_count"] = int(points.shape[0])
        h5.attrs["cell_block_count"] = len(topology)

    arrays: dict[str, Any] = {
        "axis": np.asarray(times, dtype=float),
        "points": points,
    }
    array_records: list[dict[str, object]] = [
        {"key": "axis", "role": "coordinate", "unit": str(axis_record["unit"])},
        {"key": "points", "role": "geometry", "unit": "m"},
    ]
    for index, (cell_type, data) in enumerate(topology):
        key = _array_key("cells", index, cell_type)
        arrays[key] = data
        array_records.append({"key": key, "role": "topology", "cell_type": cell_type})
    for name, frames in sorted(point_frames.items()):
        key = _array_key("point", name)
        arrays[key] = np.stack(frames)
        array_records.append({"key": key, "role": "field", "export_name": name})
    for name, frames in sorted(cell_frames.items()):
        for block_index in range(len(frames[0])):
            key = _array_key("cell", name, block_index)
            arrays[key] = np.stack([frame[block_index] for frame in frames])
            array_records.append(
                {
                    "key": key,
                    "role": "field",
                    "export_name": name,
                    "cell_block": block_index,
                }
            )

    metadata = {
        "schema": "agentcae.field-bundle",
        "schema_version": "0.1.0",
        "mesh": {
            "point_count": int(points.shape[0]),
            "geometric_dimension": int(points.shape[1]),
            "cell_blocks": [
                {"type": cell_type, "count": int(data.shape[0])}
                for cell_type, data in topology
            ],
            "fixed_across_frames": True,
        },
        "axis": {
            "name": str(axis_record["name"]),
            "values": list(times),
            "unit": str(axis_record["unit"]),
            "physical_time": axis_record["physical_time"],
            "description": str(axis_record["description"]),
        },
        "fields": field_records or [],
        "arrays": array_records,
        "source": {**_source_context(source_root), **dict(source or {})},
        "formats": {
            "xdmf": "fields.xdmf",
            "hdf5": "fields.h5",
            "npz": "fields.npz",
        },
        "output_selection": {
            "profile": profile,
            "associations": list(associations),
            "requested_fields": (
                list(dict.fromkeys(record["name"] for record in field_records or ()))
                if selected_fields is None
                else list(selected_fields)
            ),
        },
        "npz": {"allow_pickle": False, "metadata_key": "metadata_json"},
    }
    arrays["metadata_json"] = np.asarray(
        json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    )
    np.savez_compressed(npz_path, **arrays)

    metadata["artifacts"] = {
        path.name: {"sha256": file_sha256(path), "size_bytes": path.stat().st_size}
        for path in (xdmf_path, hdf5_path, npz_path)
    }
    manifest_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return FieldBundle(
        directory=target,
        xdmf=xdmf_path,
        hdf5=hdf5_path,
        npz=npz_path,
        manifest=manifest_path,
        frame_count=len(times),
        times=times,
    )


def export_openfoam_case(
    case_directory: str | Path,
    output_directory: str | Path,
    *,
    container_image: str | None = None,
    timeout_seconds: float = 3600.0,
    convert: bool = True,
    density: float | None = None,
    axis: Mapping[str, object] | None = None,
    source: Mapping[str, object] | None = None,
    profile: str = "both",
    fields: Iterable[str] | None = None,
) -> FieldBundle:
    """Export all OpenFOAM time directories to the standard field bundle."""

    case = Path(case_directory)
    files = (
        convert_openfoam_fields(
            case,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
        )
        if convert
        else openfoam_vtu_series(case)
    )
    selected_density = density if density is not None else _density_from_result(case)
    return export_vtu_series(
        files,
        output_directory,
        case_directory=case,
        density=selected_density,
        axis=axis,
        source=source,
        profile=profile,
        fields=fields,
    )


def export_agentfem_field_sample(
    bundle_directory: str | Path,
    output_path: str | Path,
    *,
    field: str,
    association: str = "point",
    frame: int = -1,
    cell_block: int = 0,
) -> Path:
    """Export one bundle field using AgentFEM's dependency-free NPZ contract.

    The result contains ``coordinates``, ``values``, ``encoding_json``, and
    ``metadata_json`` and can therefore be opened directly with
    ``agentfem.datasets.FEMFieldSample.read`` or ``numpy.load(...,
    allow_pickle=False)``. Cell fields use cell-centre coordinates.
    """

    _, np = _io_modules()
    root = Path(bundle_directory)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if association not in {"point", "cell"}:
        raise ValueError("Field-sample association must be 'point' or 'cell'.")
    matches = [
        record
        for record in manifest.get("fields", ())
        if record.get("name") == field and record.get("association") == association
    ]
    if len(matches) != 1:
        available = sorted(
            f"{record.get('name')}:{record.get('association')}"
            for record in manifest.get("fields", ())
        )
        raise KeyError(
            f"Field {field!r} with association {association!r} was not found; "
            f"available={available}."
        )
    record = matches[0]
    axis_values = tuple(float(value) for value in manifest["axis"]["values"])
    selected_frame = int(frame)
    if selected_frame < 0:
        selected_frame += len(axis_values)
    if not 0 <= selected_frame < len(axis_values):
        raise IndexError(
            f"Field-sample frame {frame} is outside a {len(axis_values)}-frame bundle."
        )

    npz_path = root / manifest["formats"]["npz"]
    with np.load(npz_path, allow_pickle=False) as arrays:
        points = np.asarray(arrays["points"], dtype=float)
        if association == "point":
            key = _array_key("point", record["export_name"])
            coordinates = points
        else:
            blocks = manifest["mesh"]["cell_blocks"]
            if not 0 <= int(cell_block) < len(blocks):
                raise IndexError(
                    f"Cell block {cell_block} is outside a {len(blocks)}-block mesh."
                )
            block = blocks[int(cell_block)]
            topology_key = _array_key("cells", int(cell_block), block["type"])
            connectivity = np.asarray(arrays[topology_key], dtype=int)
            coordinates = points[connectivity].mean(axis=1)
            key = _array_key("cell", record["export_name"], int(cell_block))
        try:
            values = np.asarray(arrays[key][selected_frame], dtype=float)
        except KeyError as error:
            raise ValueError(f"Field-bundle NPZ is missing declared array {key!r}.") from error

    if values.shape[0] != coordinates.shape[0]:
        raise ValueError("Field-sample values do not match their coordinate count.")
    encoding = {
        "name": record["name"],
        "role": "output",
        "unit": record["unit"],
        "representation": "mesh_points" if association == "point" else "cell_centres",
        "association": association,
        "components": record["components"],
        "mesh_policy": "mesh_bound_coordinates",
        "source_name": record["source_name"],
        "processing": record["processing"],
    }
    metadata = {
        "schema": "agentcae.field-sample",
        "schema_version": "0.1.0",
        "source": manifest["source"],
        "field_bundle": str(root.resolve()),
        "field_bundle_npz_sha256": manifest["artifacts"][npz_path.name]["sha256"],
        "frame_index": selected_frame,
        "coordinate_name": manifest["axis"]["name"],
        "coordinate_unit": manifest["axis"]["unit"],
        "coordinate_value": axis_values[selected_frame],
        "physical_time": manifest["axis"]["physical_time"],
        "cell_block": int(cell_block) if association == "cell" else None,
    }
    output = Path(output_path)
    if output.suffix.lower() != ".npz":
        output = output.with_suffix(".npz")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            np.savez_compressed(
                temporary,
                coordinates=coordinates,
                values=values,
                encoding_json=json.dumps(
                    encoding, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
                metadata_json=json.dumps(
                    metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
            )
        temporary_path.replace(output)
        if os.name != "nt":
            output.chmod(0o644)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return output


def verify_field_bundle(directory: str | Path) -> dict[str, object]:
    """Open every portable representation and verify its frame identity."""

    meshio, np = _io_modules()
    root = Path(directory)
    manifest_path = root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, record in payload["artifacts"].items():
        path = root / name
        if file_sha256(path) != record["sha256"]:
            raise ValueError(f"Field-bundle artifact no longer matches its hash: {name}")
    hdf5_path = root / payload["formats"]["hdf5"]
    import h5py

    with h5py.File(hdf5_path, "r") as h5:
        if h5.attrs.get("agentcae_schema") != "agentcae.field-bundle":
            raise ValueError("HDF5 field bundle is missing its AgentCAE schema identity.")
        if h5.attrs.get("schema_version") != payload.get("schema_version"):
            raise ValueError("HDF5 field-bundle version disagrees with the manifest.")
        if h5.attrs.get("axis_name") != payload["axis"]["name"]:
            raise ValueError("HDF5 field-bundle axis disagrees with the manifest.")
    with meshio.xdmf.TimeSeriesReader(root / payload["formats"]["xdmf"]) as reader:
        points, cells = reader.read_points_cells()
        xdmf_times = [reader.read_data(index)[0] for index in range(reader.num_steps)]
    try:
        with np.load(root / payload["formats"]["npz"], allow_pickle=False) as arrays:
            npz_times = arrays["axis"]
            npz_points = arrays["points"]
            embedded = json.loads(str(arrays["metadata_json"]))
    except (KeyError, json.JSONDecodeError) as error:
        raise ValueError("NPZ field bundle is missing its safe standard arrays or metadata.") from error
    expected = payload["axis"]["values"]
    if not np.allclose(xdmf_times, expected) or not np.allclose(npz_times, expected):
        raise ValueError("XDMF and NPZ time axes disagree with the manifest.")
    if np.asarray(points).shape != npz_points.shape:
        raise ValueError("XDMF and NPZ point geometry disagree.")
    if embedded.get("schema") != payload.get("schema"):
        raise ValueError("NPZ embedded metadata disagrees with the manifest.")
    return {
        "schema": "agentcfd.field-bundle-verification/0.1",
        "verified": True,
        "frame_count": len(expected),
        "point_count": int(npz_points.shape[0]),
        "cell_block_count": len(cells),
        "formats": sorted(payload["formats"]),
    }


__all__ = [
    "FieldBundle",
    "FieldSemantic",
    "convert_openfoam_fields",
    "export_agentfem_field_sample",
    "export_openfoam_case",
    "export_vtu_series",
    "io_available",
    "openfoam_vtu_series",
    "verify_field_bundle",
]
