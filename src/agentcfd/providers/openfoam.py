"""OpenFOAM case lowering and external-process execution.

AgentCFD does not import, link, vendor, or modify OpenFOAM.  This provider
creates ordinary OpenFOAM case files and, when explicitly asked to run, calls
an installation already present on the user's machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .. import boundaries, engineering
from .._version import __version__
from .._validation import integer_at_least, positive_float
from ..errors import CaseIntegrityError, ProviderUnavailableError, UnsupportedCaseError
from ..jsonio import strict_json_object
from ..results import (
    Artifact,
    Check,
    FieldRecord,
    History,
    Quantity,
    SimulationResult,
    read_result_record,
)
from .base import ProviderDescriptor


_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OUTPUT_FIELD_KEYS = {
    "fluid.velocity": "U",
    "fluid.pressure": "p",
    "turbulence.kinetic_energy": "k",
    "turbulence.specific_dissipation_rate": "omega",
    "turbulence.dissipation_rate": "epsilon",
    "turbulence.kinematic_eddy_viscosity": "nut",
}
_OUTPUT_HISTORY_KEYS = {
    "flow.mass_balance": "flow.relative_mass_imbalance",
    "flow.pressure_drop": "flow.pressure_drop",
    "wall.y_plus": "wall.y_plus.average",
}

_LAMINAR_CAPABILITY = "openfoam.steady-laminar-circular-pipe"
_TURBULENT_CAPABILITY = "openfoam.steady-rans-smooth-circular-pipe"
_PRECURSOR_PROVIDER = "openfoam-periodic-precursor"
_PRECURSOR_FIELDS = ("U", "p", "k", "omega", "nut")


def _is_turbulent_step(step) -> bool:
    return step.model.study.turbulence == "k-omega-sst"


def _case_capability(step) -> str:
    return _TURBULENT_CAPABILITY if _is_turbulent_step(step) else _LAMINAR_CAPABILITY


def _analysis_sha256(step) -> str:
    """Fingerprint every public analysis input represented by a prepared case."""

    payload = {
        "model": step.model.to_dict(),
        "procedure": step.procedure.to_dict(),
        "output_request": step.output.to_dict(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _precursor_mapping_contract(
    step,
    source_directory: Path,
    *,
    cross_section_cells: int,
    nominal_wall_cell_fraction: float | None,
) -> dict[str, Any]:
    """Verify an accepted precursor and return a path-independent map contract."""

    source = source_directory.resolve()
    result_path = source / "agentcfd-result.json"
    try:
        result = read_result_record(result_path)
    except (FileNotFoundError, ValueError) as error:
        raise CaseIntegrityError(
            "The precursor result failed AgentCFD evidence validation: " + str(error)
        ) from error
    if (
        result.get("provider") != _PRECURSOR_PROVIDER
        or result.get("status") != "completed"
        or result.get("converged") is not True
        or result.get("accepted") is not True
        or result.get("trust_level") not in {"verified", "validated"}
    ):
        raise CaseIntegrityError(
            "The precursor result must be accepted and have verified or validated trust."
        )
    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        raise CaseIntegrityError("The precursor result has no provenance record.")
    if provenance.get("model_sha256") != step.model.fingerprint():
        raise CaseIntegrityError("The precursor belongs to a different scientific model.")
    mesh_sha256 = provenance.get("mesh_sha256")
    if not isinstance(mesh_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", mesh_sha256) is None:
        raise CaseIntegrityError("The precursor has no valid mesh identity.")

    scientific = result.get("scientific_inputs")
    record = scientific.get("record") if isinstance(scientific, dict) else None
    precursor = record.get("precursor") if isinstance(record, dict) else None
    if not isinstance(precursor, dict):
        raise CaseIntegrityError("The precursor result has no declared numerical controls.")
    if precursor.get("cross_section_cells") != cross_section_cells:
        raise CaseIntegrityError(
            "The precursor and downstream pipe must use the same cross-section resolution."
        )
    if precursor.get("nominal_wall_cell_fraction") != nominal_wall_cell_fraction:
        raise CaseIntegrityError(
            "The precursor and downstream pipe must use the same near-wall grading."
        )
    if precursor.get("axial_cells") != 1 or precursor.get("periodic_end_planes") is not True:
        raise CaseIntegrityError("The source is not a one-layer periodic pipe precursor.")

    artifacts = result.get("artifact_records")
    if not isinstance(artifacts, dict):
        raise CaseIntegrityError("The precursor result has no content-addressed artifacts.")

    def verified_artifact(name: str) -> tuple[Path, str, str]:
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict):
            raise CaseIntegrityError(f"The precursor is missing its {name} artifact.")
        relative = artifact.get("path")
        digest = artifact.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise CaseIntegrityError(f"The precursor {name} artifact identity is malformed.")
        candidate = source / relative
        path = candidate.resolve()
        try:
            normalized = path.relative_to(source).as_posix()
        except ValueError as error:
            raise CaseIntegrityError(f"The precursor {name} artifact escapes its case.") from error
        if candidate.is_symlink() or not path.is_file() or _file_sha256(path) != digest:
            raise CaseIntegrityError(f"The precursor {name} artifact is missing or changed.")
        return path, normalized, digest

    case_manifest_path, _, case_manifest_digest = verified_artifact("case_manifest")
    mesh_manifest_path, _, mesh_manifest_digest = verified_artifact("mesh_manifest")
    try:
        source_case_manifest = strict_json_object(
            case_manifest_path.read_text(encoding="utf-8"),
            label="precursor case manifest",
        )
        source_mesh_manifest = strict_json_object(
            mesh_manifest_path.read_text(encoding="utf-8"),
            label="precursor mesh manifest",
        )
    except ValueError as error:
        raise CaseIntegrityError("A precursor case or mesh manifest is invalid.") from error
    if source_case_manifest.get("case_sha256") != provenance.get("case_sha256"):
        raise CaseIntegrityError("The precursor case manifest disagrees with result provenance.")
    if source_mesh_manifest.get("mesh_sha256") != mesh_sha256:
        raise CaseIntegrityError("The precursor mesh manifest disagrees with result provenance.")

    selected: dict[str, dict[str, object]] = {}
    field_time: str | None = None
    for field in _PRECURSOR_FIELDS:
        _, normalized, digest = verified_artifact(f"field_{field}")
        parts = PurePosixPath(normalized).parts
        if len(parts) != 2 or parts[1] != field or not _is_positive_time_name(parts[0]):
            raise CaseIntegrityError(f"The precursor {field} artifact is not a final-time field.")
        if field_time is None:
            field_time = parts[0]
        elif field_time != parts[0]:
            raise CaseIntegrityError("The precursor fields do not share one solution time.")
        selected[field] = {"path": normalized, "sha256": digest}

    case_sha256 = provenance.get("case_sha256")
    if not isinstance(case_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", case_sha256) is None:
        raise CaseIntegrityError("The precursor has no valid lowered-case identity.")
    runtime_version = provenance.get("provider_version")
    if not isinstance(runtime_version, str) or not runtime_version.strip():
        raise CaseIntegrityError("The precursor has no OpenFOAM runtime version.")
    quantities = result.get("quantities")
    pressure_gradient = (
        quantities.get("flow.pressure_gradient")
        if isinstance(quantities, dict)
        else None
    )
    pressure_gradient_value = (
        pressure_gradient.get("value") if isinstance(pressure_gradient, dict) else None
    )
    if (
        isinstance(pressure_gradient_value, bool)
        or not isinstance(pressure_gradient_value, (int, float))
        or not math.isfinite(pressure_gradient_value)
        or pressure_gradient_value <= 0.0
        or pressure_gradient.get("unit") != "Pa/m"
    ):
        raise CaseIntegrityError("The precursor has no valid physical pressure gradient.")
    return {
        "schema": "agentcfd.openfoam-precursor-map/0.1",
        "source_result_sha256": _file_sha256(result_path),
        "source_case_sha256": case_sha256,
        "source_case_manifest_sha256": case_manifest_digest,
        "source_mesh_sha256": mesh_sha256,
        "source_mesh_manifest_sha256": mesh_manifest_digest,
        "source_model_sha256": step.model.fingerprint(),
        "source_runtime_version": runtime_version,
        "source_pressure_gradient_pa_per_m": pressure_gradient_value,
        "source_time": field_time,
        "cross_section_cells": cross_section_cells,
        "method": "mapNearest",
        "mapped_fields": selected,
        "boundary_policy": {
            "U": "flowRateInletVelocity with extrapolateProfile",
            "p": "mapped internal gauge with target outlet fixedValue",
            "k": "zeroGradient inlet from mapped internal field",
            "omega": "zeroGradient inlet from mapped internal field",
            "nut": "calculated",
        },
    }


def _solver_converged(log: str) -> bool:
    """Return true only for an explicit OpenFOAM convergence statement."""

    return bool(re.search(r"(?mi)^.*solution converged in \d+ iterations\s*$", log))


def _solver_residual_evidence(
    log: str,
) -> tuple[dict[str, Quantity], dict[str, History]]:
    """Recover per-equation initial and final residual histories from a solver log."""

    current_iteration: float | None = None
    samples: dict[str, dict[float, tuple[float, float, int]]] = {}
    time_pattern = re.compile(r"^\s*Time\s*=\s*([0-9.eE+-]+)\s*$")
    solve_pattern = re.compile(
        r"Solving for\s+([^,]+),\s+Initial residual\s*=\s*([^,]+),\s+"
        r"Final residual\s*=\s*([^,]+),\s+No Iterations\s+(\d+)"
    )
    for line in log.splitlines():
        time_match = time_pattern.match(line)
        if time_match is not None:
            try:
                selected = float(time_match.group(1))
            except ValueError:
                current_iteration = None
            else:
                current_iteration = selected if math.isfinite(selected) else None
            continue
        solve_match = solve_pattern.search(line)
        if solve_match is None or current_iteration is None:
            continue
        field = solve_match.group(1).strip()
        if _FOAM_WORD.fullmatch(field) is None:
            continue
        try:
            initial = float(solve_match.group(2))
            final = float(solve_match.group(3))
            iterations = int(solve_match.group(4))
        except ValueError:
            continue
        if not all(math.isfinite(value) and value >= 0.0 for value in (initial, final)):
            continue
        samples.setdefault(field, {})[current_iteration] = (initial, final, iterations)

    quantities: dict[str, Quantity] = {}
    histories: dict[str, History] = {}
    for field, field_samples in sorted(samples.items()):
        ordered = sorted(field_samples.items())
        axis = tuple(item[0] for item in ordered)
        initial_values = tuple(item[1][0] for item in ordered)
        final_values = tuple(item[1][1] for item in ordered)
        iteration_values = tuple(float(item[1][2]) for item in ordered)
        for kind, values, unit, description in (
            ("initial_residual", initial_values, "1", "Initial linear-system residual."),
            ("final_residual", final_values, "1", "Final linear-system residual."),
            ("linear_iterations", iteration_values, "1", "Linear solver iterations."),
        ):
            name = f"solver.{kind}.{field}"
            histories[name] = History(
                axis,
                values,
                unit=unit,
                abscissa_name="iteration",
                abscissa_unit="1",
                description=description,
            )
            quantities[name] = Quantity(
                values[-1],
                unit,
                kind="diagnostic",
                description=f"Final recorded {description.lower()}",
            )
    return quantities, histories


def _outer_residual_check(
    quantities: dict[str, Quantity],
    *,
    tolerance: float,
    axial_velocity_component: str | None = None,
    additional_fields: tuple[str, ...] = (),
) -> Check:
    """Require relevant pressure/velocity outer residuals below the target.

    The bounded pipe provider knows that the physical velocity is aligned with
    ``z``.  OpenFOAM's normalized residuals for the analytically zero transverse
    components can become ill-conditioned as their solution norm approaches
    machine zero, so those components are retained as diagnostics but are not
    used as axial pipe convergence gates.
    """

    prefix = "solver.initial_residual."
    residuals = {
        name.removeprefix(prefix): quantity.value
        for name, quantity in quantities.items()
        if name.startswith(prefix)
    }
    has_pressure = "p" in residuals
    if axial_velocity_component is None:
        velocity_names = tuple(
            name for name in residuals if name == "U" or name.startswith("U")
        )
    else:
        velocity_names = tuple(
            name for name in (axial_velocity_component, "U") if name in residuals
        )
    transverse_final: dict[str, float] = {}
    if axial_velocity_component is not None:
        final_prefix = "solver.final_residual."
        transverse_final = {
            name.removeprefix(final_prefix): quantity.value
            for name, quantity in quantities.items()
            if name.startswith(final_prefix)
            and name.removeprefix(final_prefix).startswith("U")
            and name.removeprefix(final_prefix) not in {axial_velocity_component, "U"}
        }
    has_velocity = bool(velocity_names)
    present_additional = tuple(name for name in additional_fields if name in residuals)
    selected_names = (("p",) if has_pressure else ()) + velocity_names + present_additional
    selected = tuple(residuals[name] for name in selected_names) + tuple(
        transverse_final.values()
    )
    maximum = max(selected) if selected else None
    passed = bool(
        has_pressure
        and has_velocity
        and maximum is not None
        and maximum <= tolerance
    )
    missing = []
    if not has_pressure:
        missing.append("pressure")
    if not has_velocity:
        missing.append("velocity")
    missing.extend(name for name in additional_fields if name not in residuals)
    return Check(
        name="outer-residual-target",
        passed=passed,
        value=maximum,
        limit=tolerance,
        message=(
            "Relevant outer initial residuals and zero-target transverse final "
            "linear residuals satisfy the configured target."
            if passed
            else "Missing equations: " + ", ".join(missing)
            if missing
            else "A relevant outer or zero-target transverse residual exceeds the configured target."
        ),
        kind="verification",
        observable="solver.residual",
    )


def _read_scalar_series(case_directory: Path, function_name: str) -> dict[float, float]:
    """Read the scalar history written by an OpenFOAM field-value object."""

    root = case_directory / "postProcessing" / function_name
    samples: dict[float, float] = {}
    if not root.is_dir():
        return samples

    def restart_key(path: Path) -> tuple[float, str]:
        try:
            start_time = float(path.relative_to(root).parts[0])
        except (ValueError, IndexError):
            start_time = -math.inf
        return start_time, str(path)

    for path in sorted(root.rglob("*.dat"), key=restart_key):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            columns = stripped.replace("(", " ").replace(")", " ").split()
            if len(columns) < 2:
                continue
            try:
                time = float(columns[0])
                value = float(columns[1])
            except ValueError:
                continue
            if math.isfinite(time) and math.isfinite(value):
                samples[time] = value
    return samples


def _read_y_plus_series(
    case_directory: Path,
    function_name: str = "agentcfd_y_plus",
) -> dict[str, dict[float, float]]:
    """Read patch min/max/average columns written by OpenFOAM ``yPlus``."""

    root = case_directory / "postProcessing" / function_name
    series: dict[str, dict[float, float]] = {
        "minimum": {},
        "maximum": {},
        "average": {},
    }
    if not root.is_dir():
        return series
    for path in sorted(root.rglob("*.dat"), key=str):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            columns = stripped.replace("(", " ").replace(")", " ").split()
            if len(columns) < 4:
                continue
            try:
                selected_time = float(columns[0])
                minimum, maximum, average = (float(value) for value in columns[-3:])
            except ValueError:
                continue
            if all(
                math.isfinite(value) and value >= 0.0
                for value in (selected_time, minimum, maximum, average)
            ):
                series["minimum"][selected_time] = minimum
                series["maximum"][selected_time] = maximum
                series["average"][selected_time] = average
    return series


def _mesh_quality_quantities(log: str) -> dict[str, Quantity]:
    """Recover stable checkMesh observables without depending on line order."""

    patterns = {
        "mesh.cell_count": (r"(?m)^\s*cells:\s+(\d+)\s*$", "1"),
        "mesh.maximum_aspect_ratio": (r"Max aspect ratio =\s*([0-9.eE+-]+)", "1"),
        "mesh.maximum_non_orthogonality": (
            r"Mesh non-orthogonality Max:\s*([0-9.eE+-]+)",
            "deg",
        ),
        "mesh.average_non_orthogonality": (
            r"Mesh non-orthogonality Max:\s*[0-9.eE+-]+\s+average:\s*([0-9.eE+-]+)",
            "deg",
        ),
        "mesh.maximum_skewness": (r"Max skewness =\s*([0-9.eE+-]+)", "1"),
    }
    quantities: dict[str, Quantity] = {}
    for name, (pattern, unit) in patterns.items():
        match = re.search(pattern, log)
        if match is not None:
            quantities[name] = Quantity(float(match.group(1)), unit)
    return quantities


def _nominal_wall_fraction_from_block_mesh(content: str) -> float | None:
    """Recover new or legacy-uniform near-wall intent from a blockMeshDict."""

    fraction_match = re.search(
        r"(?m)^// agentcfdNominalWallCellFraction\s+(auto|[0-9.eE+-]+)\s*$",
        content,
    )
    if fraction_match is None:
        legacy_gradings = re.findall(
            r"simpleGrading\s+\(\s*([0-9.eE+-]+)\s+"
            r"([0-9.eE+-]+)\s+([0-9.eE+-]+)\s*\)",
            content,
        )
        if len(legacy_gradings) != 5 or any(
            not all(math.isclose(float(value), 1.0) for value in grading)
            for grading in legacy_gradings
        ):
            raise CaseIntegrityError(
                "Prepared pipe case does not declare its nominal wall-cell strategy."
            )
        fraction = None
    else:
        encoded_fraction = fraction_match.group(1)
        fraction = None if encoded_fraction == "auto" else float(encoded_fraction)
    return fraction


def _mesh_controls_from_case(case_directory: Path) -> OpenFOAMMeshControls:
    """Recover the generated five-block resolution without trusting CLI repetition."""

    path = case_directory / "system" / "blockMeshDict"
    content = path.read_text(encoding="utf-8")
    matches = re.findall(
        r"hex\s+\([^)]*\)\s+\((\d+)\s+(\d+)\s+(\d+)\)",
        content,
    )
    if len(matches) != 5:
        raise CaseIntegrityError("Prepared pipe case does not contain five recognized hex blocks.")
    counts = {(int(first), int(second), int(axial)) for first, second, axial in matches}
    if len(counts) != 1:
        raise CaseIntegrityError("Prepared pipe blocks do not share one mesh resolution.")
    first, second, axial = counts.pop()
    if first != second:
        raise CaseIntegrityError("Prepared pipe cross-section block counts are inconsistent.")
    fraction = _nominal_wall_fraction_from_block_mesh(content)
    return OpenFOAMMeshControls(
        cross_section_cells=first,
        axial_cells=axial,
        nominal_wall_cell_fraction=fraction,
    )


def _unexpected_case_entries(prepared: PreparedOpenFOAMCase) -> tuple[str, ...]:
    """Find unrecorded inputs and links that could change prepared-case semantics."""

    allowed_files = set(prepared.files) | {"agentcfd-case.json"}
    allowed_directories = {
        parent.as_posix()
        for relative in prepared.files
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    }
    unexpected: list[str] = []
    for path in prepared.directory.rglob("*"):
        relative = path.relative_to(prepared.directory).as_posix()
        if path.is_symlink():
            unexpected.append(relative)
        elif path.is_dir():
            if relative not in allowed_directories:
                unexpected.append(relative + "/")
        elif relative not in allowed_files:
            unexpected.append(relative)
    return tuple(sorted(unexpected))


def _container_image_identity(
    docker_command: str,
    image: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Inspect the local image used by Docker and return immutable identity data."""

    record: dict[str, Any] = {
        "requested_reference": image,
        "image_id": None,
        "repo_digests": [],
        "os": None,
        "architecture": None,
        "identity_verified": False,
    }
    try:
        completed = subprocess.run(
            [docker_command, "image", "inspect", image],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(timeout_seconds, 30.0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        record["inspection_error"] = str(error)
        return record
    if completed.returncode != 0:
        record["inspection_error"] = (completed.stderr or completed.stdout).strip()[-500:]
        return record
    try:
        payload = json.loads(completed.stdout)
        selected = payload[0]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
        record["inspection_error"] = f"invalid docker image inspect output: {error}"
        return record
    image_id = selected.get("Id")
    repo_digests = selected.get("RepoDigests") or []
    if isinstance(image_id, str):
        record["image_id"] = image_id
    if isinstance(repo_digests, list):
        record["repo_digests"] = sorted(
            item for item in repo_digests if isinstance(item, str)
        )
    for key, source in (("os", "Os"), ("architecture", "Architecture")):
        value = selected.get(source)
        if isinstance(value, str):
            record[key] = value
    record["identity_verified"] = bool(
        isinstance(image_id, str)
        and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", image_id)
    )
    return record


def _stop_timed_out_container(docker_command: str, cidfile: Path) -> str:
    """Stop exactly the container recorded for a timed-out provider command."""

    try:
        container_id = cidfile.read_text(encoding="utf-8").strip()
    except OSError as error:
        return f"Container cleanup unavailable: {error}"
    if re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id) is None:
        return "Container cleanup refused an invalid container identity."
    try:
        cleanup = subprocess.run(
            [docker_command, "rm", "--force", container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Container cleanup failed: {error}"
    if cleanup.returncode == 0:
        return f"Stopped timed-out container {container_id}."
    detail = (cleanup.stderr or cleanup.stdout).strip()[-500:]
    return f"Container cleanup failed for {container_id}: {detail}"


def _write_mesh_manifest(case_directory: Path) -> tuple[str | None, Path | None]:
    """Content-address the generated OpenFOAM mesh used by the solved fields."""

    root = case_directory / "constant" / "polyMesh"
    if not root.is_dir():
        return None, None
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    if not files:
        return None, None
    mesh_sha256 = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = case_directory / "agentcfd-mesh.json"
    payload = {
        "schema": "agentcfd.openfoam-mesh/0.1",
        "mesh_sha256": mesh_sha256,
        "root": "constant/polyMesh",
        "files": files,
    }
    manifest.write_bytes(
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return mesh_sha256, manifest


def _runtime_version(logs: dict[str, str], fallback: str) -> str:
    for log in logs.values():
        match = re.search(r"(?m)^\|.*Version:\s*([^ ]+)\s+", log)
        if match is not None:
            return match.group(1)
        match = re.search(r"\bversion=([^\s]+)", log)
        if match is not None:
            return match.group(1)
    return fallback


def _runtime_version_key(version: str) -> str:
    """Normalize the optional ``v`` prefix used in OpenCFD release labels."""

    selected = version.strip()
    if len(selected) > 1 and selected[0].lower() == "v" and selected[1:].isdigit():
        return selected[1:]
    return selected


def _latest_time_directory(case_directory: Path) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for path in case_directory.iterdir():
        if not path.is_dir():
            continue
        try:
            time = float(path.name)
        except ValueError:
            continue
        if time > 0.0:
            candidates.append((time, path))
    return max(candidates, default=(0.0, None), key=lambda item: item[0])[1]


def _is_positive_time_name(name: str) -> bool:
    try:
        return float(name) > 0.0
    except ValueError:
        return False


def _recover_patch_data(
    case_directory: Path,
    *,
    density: float,
    reference_pressure_drop: float,
    solver_tolerance: float,
    reference_pressure_drop_per_flow: float | None = None,
    requested_volume_flow: float | None = None,
    pressure_error_limit: float = 0.02,
    mass_balance_limit: float = 1.0e-6,
    inlet_flow_error_limit: float = 0.01,
    pressure_reference_applicable: bool = True,
    pressure_reference_kind: str = "validation",
    pressure_reference_requirement: str = "fully developed inlet boundary",
    validation_message: str = "Pressure drop is compared with Hagen--Poiseuille at recovered flow.",
) -> tuple[dict[str, Quantity], dict[str, History], tuple[Check, ...], str]:
    if not isinstance(pressure_reference_applicable, bool):
        raise ValueError("pressure_reference_applicable must be a boolean.")
    if pressure_reference_kind not in {"verification", "validation"}:
        raise ValueError("pressure_reference_kind must be verification or validation.")
    series = {
        name: _read_scalar_series(case_directory, name)
        for name in (
            "agentcfd_inlet_flow",
            "agentcfd_outlet_flow",
            "agentcfd_inlet_pressure",
            "agentcfd_outlet_pressure",
        )
    }
    quantities = {
        "reference.flow.pressure_drop": Quantity(reference_pressure_drop, "Pa"),
        "reference.flow.pressure_drop_requested": Quantity(reference_pressure_drop, "Pa"),
    }
    common_times = sorted(set.intersection(*(set(values) for values in series.values())))
    if not common_times:
        return (
            quantities,
            {},
            (
                Check(
                    name="field-output-recovery",
                    passed=False,
                    value="missing",
                    limit="all four patch histories are required",
                    message="OpenFOAM did not write a complete common patch-history sample.",
                    kind="verification",
                    observable="flow.mass_balance_and_pressure_drop",
                ),
            ),
            "OpenFOAM patch result recovery is incomplete.",
        )

    inlet_flow = tuple(-series["agentcfd_inlet_flow"][time] for time in common_times)
    outlet_flow = tuple(series["agentcfd_outlet_flow"][time] for time in common_times)
    pressure_drop = tuple(
        density
        * (
            series["agentcfd_inlet_pressure"][time]
            - series["agentcfd_outlet_pressure"][time]
        )
        for time in common_times
    )
    mass_imbalance = tuple(
        abs(inflow - outflow) / max(abs(inflow), abs(outflow), 1.0e-300)
        for inflow, outflow in zip(inlet_flow, outlet_flow)
    )
    final_inlet_flow = inlet_flow[-1]
    final_outlet_flow = outlet_flow[-1]
    final_pressure_drop = pressure_drop[-1]
    final_mass_imbalance = mass_imbalance[-1]
    inlet_flow_check: tuple[Check, ...] = ()
    if requested_volume_flow is not None:
        requested_volume_flow = positive_float(
            requested_volume_flow,
            name="requested_volume_flow",
        )
        inlet_flow_error_limit = positive_float(
            inlet_flow_error_limit,
            name="inlet_flow_error_limit",
        )
        inlet_flow_error = abs(final_inlet_flow - requested_volume_flow) / requested_volume_flow
        quantities["flow.requested_volume_flow_rate"] = Quantity(
            requested_volume_flow,
            "m^3/s",
        )
        quantities["flow.inlet_flow_relative_error"] = Quantity(
            inlet_flow_error,
            "1",
            kind="verification_metric",
            description="Recovered inlet flow error relative to the public boundary request.",
        )
        inlet_flow_check = (
            Check(
                name="inlet-flow-target",
                passed=inlet_flow_error <= inlet_flow_error_limit,
                value=inlet_flow_error,
                limit=inlet_flow_error_limit,
                kind="verification",
                observable="flow.inlet_volume_flow_rate",
            ),
        )
    reference_from_recovered_flow = reference_pressure_drop
    if reference_pressure_drop_per_flow is not None:
        reference_from_recovered_flow = reference_pressure_drop_per_flow * 0.5 * (
            final_inlet_flow + final_outlet_flow
        )
        quantities["reference.flow.pressure_drop"] = Quantity(
            reference_from_recovered_flow,
            "Pa",
            description="Hagen-Poiseuille pressure drop at the recovered volume flow.",
        )
    reference_is_positive = reference_from_recovered_flow > 0.0
    relative_pressure_error = (
        abs(final_pressure_drop - reference_from_recovered_flow)
        / abs(reference_from_recovered_flow)
        if reference_is_positive
        else 1.0
    )
    pressure_error_limit = positive_float(
        pressure_error_limit,
        name="pressure_error_limit",
    )
    mass_balance_limit = max(
        positive_float(mass_balance_limit, name="mass_balance_limit"),
        10.0 * solver_tolerance,
    )
    quantities.update(
        {
            "flow.inlet_volume_flow_rate": Quantity(final_inlet_flow, "m^3/s"),
            "flow.outlet_volume_flow_rate": Quantity(final_outlet_flow, "m^3/s"),
            "flow.mass_flow_rate": Quantity(
                0.5 * density * (final_inlet_flow + final_outlet_flow),
                "kg/s",
            ),
            "flow.pressure_drop": Quantity(final_pressure_drop, "Pa"),
            "flow.pressure_drop_relative_error": Quantity(
                relative_pressure_error,
                "1",
                kind=(
                    f"{pressure_reference_kind}_metric"
                    if pressure_reference_applicable else "diagnostic"
                ),
                description=(
                    f"Relative {pressure_reference_kind} error against an applicable "
                    "fully developed reference."
                    if pressure_reference_applicable
                    else "Diagnostic difference from a non-applicable fully developed reference."
                ),
            ),
        }
    )
    iteration = tuple(common_times)
    histories = {
        "flow.inlet_volume_flow_rate": History(
            iteration,
            inlet_flow,
            unit="m^3/s",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
        "flow.outlet_volume_flow_rate": History(
            iteration,
            outlet_flow,
            unit="m^3/s",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
        "flow.pressure_drop": History(
            iteration,
            pressure_drop,
            unit="Pa",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
        "flow.relative_mass_imbalance": History(
            iteration,
            mass_imbalance,
            unit="1",
            abscissa_name="iteration",
            abscissa_unit=None,
        ),
    }
    checks = (
        Check(
            name="field-output-recovery",
            passed=True,
            value=float(len(common_times)),
            limit="at least one common patch sample",
            kind="runtime",
            observable="flow.patch_histories",
        ),
        Check(
            name="mass-balance",
            passed=final_mass_imbalance <= mass_balance_limit,
            value=final_mass_imbalance,
            limit=mass_balance_limit,
            kind="verification",
            observable="flow.relative_mass_imbalance",
        ),
        Check(
            name="positive-through-flow",
            passed=(
                final_inlet_flow > 0.0
                and final_outlet_flow > 0.0
                and reference_is_positive
            ),
            value=min(final_inlet_flow, final_outlet_flow),
            limit="> 0 m^3/s at both patches",
            kind="verification",
            observable="flow.volume_flow_rate",
        ),
        Check(
            name="pressure-drop-reference",
            passed=(
                relative_pressure_error <= pressure_error_limit
                if pressure_reference_applicable
                else True
            ),
            value=relative_pressure_error,
            limit=pressure_error_limit,
            message=validation_message,
            kind=pressure_reference_kind,
            observable="flow.pressure_drop_relative_error",
        ),
        Check(
            name="pressure-reference-applicability",
            passed=pressure_reference_applicable,
            value=("applicable" if pressure_reference_applicable else "not applicable"),
            limit=pressure_reference_requirement,
            message=(
                "The selected pressure-loss reference applies only after its declared "
                "inlet and grid-evidence requirements are met."
            ),
            kind=pressure_reference_kind,
            observable="boundary.inlet_profile",
        ),
        *inlet_flow_check,
    )
    return (
        quantities,
        histories,
        checks,
        "OpenFOAM patch flow and pressure histories were recovered automatically.",
    )


def _recover_turbulence_data(
    case_directory: Path,
    *,
    density: float,
    dynamic_viscosity: float,
    mean_velocity: float,
    diameter: float,
    length: float,
    pressure_drop: Quantity | None,
    policy: OpenFOAMValidationPolicy,
) -> tuple[dict[str, Quantity], dict[str, History], tuple[Check, ...]]:
    """Recover wall resolution and bulk friction evidence for the RANS slice."""

    raw = _read_y_plus_series(case_directory)
    common_times = sorted(set.intersection(*(set(values) for values in raw.values())))
    quantities: dict[str, Quantity] = {}
    histories: dict[str, History] = {}
    if common_times:
        for statistic in ("minimum", "maximum", "average"):
            values = tuple(raw[statistic][selected] for selected in common_times)
            name = f"wall.y_plus.{statistic}"
            histories[name] = History(
                tuple(common_times),
                values,
                unit="1",
                abscissa_name="iteration",
                abscissa_unit="1",
                description=f"Wall y-plus {statistic} reported by OpenFOAM.",
            )
            quantities[name] = Quantity(
                values[-1],
                "1",
                kind="verification_metric",
            )
    mean_y_plus = quantities.get("wall.y_plus.average")
    minimum_y_plus = quantities.get("wall.y_plus.minimum")
    maximum_y_plus = quantities.get("wall.y_plus.maximum")
    y_plus_value = mean_y_plus.value if mean_y_plus is not None else None
    y_plus_ok = bool(
        minimum_y_plus is not None
        and maximum_y_plus is not None
        and minimum_y_plus.value >= policy.minimum_wall_y_plus
        and maximum_y_plus.value <= policy.maximum_wall_y_plus
    )

    reynolds = engineering.reynolds_number(
        density=density,
        mean_velocity=mean_velocity,
        hydraulic_diameter=diameter,
        dynamic_viscosity=dynamic_viscosity,
    )
    reference_friction = engineering.darcy_friction_factor(reynolds)
    quantities["reference.flow.darcy_friction_factor"] = Quantity(
        reference_friction,
        "1",
    )
    friction_value: float | None = None
    friction_error: float | None = None
    if pressure_drop is not None:
        friction_value = (
            2.0
            * pressure_drop.value
            * diameter
            / (density * length * mean_velocity**2)
        )
        friction_error = abs(friction_value - reference_friction) / reference_friction
        quantities["flow.darcy_friction_factor"] = Quantity(friction_value, "1")
        quantities["flow.darcy_friction_factor_relative_error"] = Quantity(
            friction_error,
            "1",
            kind="diagnostic",
            description=(
                "Difference from the smooth-pipe Colebrook relation; inlet development "
                "and RANS model-form effects are not separated in this first slice."
            ),
        )

    return (
        quantities,
        histories,
        (
            Check(
                name="wall-y-plus-recovery",
                passed=bool(common_times),
                value=(len(common_times) if common_times else 0),
                limit="at least one complete wall y-plus sample",
                kind="verification",
                observable="wall.y_plus",
            ),
            Check(
                name="wall-y-plus-range",
                passed=y_plus_ok,
                value=y_plus_value,
                limit=(
                    f"all wall y+ in [{policy.minimum_wall_y_plus:g}, "
                    f"{policy.maximum_wall_y_plus:g}]"
                ),
                message=(
                    "The full recovered wall range, not only its mean, must match "
                    "the declared wall-function strategy."
                ),
                kind="verification",
                observable="wall.y_plus.average",
            ),
            Check(
                name="turbulent-friction-diagnostic",
                passed=bool(
                    friction_error is not None
                    and friction_error <= policy.maximum_relative_turbulent_friction_error
                ),
                value=friction_error,
                limit=policy.maximum_relative_turbulent_friction_error,
                message=(
                    "This comparison is diagnostic until a developed inlet and grid "
                    "sensitivity establish reference applicability."
                ),
                kind="verification",
                observable="flow.darcy_friction_factor_relative_error",
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class OpenFOAMMeshControls:
    """Provider-specific controls for the circular-pipe O-grid.

    ``nominal_wall_cell_fraction`` is the wall-adjacent cell width divided by
    the nominal outer-block wall-normal edge length.  Leaving it unset gives
    uniform cells.  Holding it fixed while increasing ``cross_section_cells``
    isolates an interior-resolution study without silently changing the
    wall-function sampling distance.
    """

    cross_section_cells: int = 8
    axial_cells: int | None = None
    nominal_wall_cell_fraction: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cross_section_cells",
            integer_at_least(
                self.cross_section_cells,
                name="cross_section_cells",
                minimum=2,
            ),
        )
        if self.axial_cells is not None:
            object.__setattr__(
                self,
                "axial_cells",
                integer_at_least(
                    self.axial_cells,
                    name="axial_cells",
                    minimum=2,
                ),
            )
        if self.nominal_wall_cell_fraction is not None:
            fraction = positive_float(
                self.nominal_wall_cell_fraction,
                name="nominal_wall_cell_fraction",
            )
            if fraction >= 1.0:
                raise ValueError("nominal_wall_cell_fraction must be below one.")
            object.__setattr__(self, "nominal_wall_cell_fraction", fraction)


@dataclass(frozen=True, slots=True)
class OpenFOAMValidationPolicy:
    """Explicit scientific acceptance thresholds for the pipe provider."""

    maximum_relative_mass_imbalance: float = 1.0e-6
    maximum_relative_pressure_error: float = 0.02
    maximum_relative_inlet_flow_error: float = 0.01
    maximum_relative_pressure_drop_drift: float = 1.0e-4
    maximum_relative_turbulent_pressure_drop_drift: float = 5.0e-4
    minimum_steady_samples: int = 5
    minimum_precursor_steady_samples: int = 50
    maximum_mesh_non_orthogonality: float = 65.0
    maximum_mesh_skewness: float = 4.0
    maximum_mesh_aspect_ratio: float = 50.0
    minimum_wall_y_plus: float = 30.0
    maximum_wall_y_plus: float = 300.0
    maximum_relative_turbulent_friction_error: float = 0.15
    maximum_turbulent_outer_residual: float = 1.0e-3
    validated_runtime_versions: tuple[str, ...] = ("2606",)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_relative_mass_imbalance",
            positive_float(
                self.maximum_relative_mass_imbalance,
                name="maximum_relative_mass_imbalance",
            ),
        )
        object.__setattr__(
            self,
            "maximum_relative_pressure_error",
            positive_float(
                self.maximum_relative_pressure_error,
                name="maximum_relative_pressure_error",
            ),
        )
        object.__setattr__(
            self,
            "maximum_relative_inlet_flow_error",
            positive_float(
                self.maximum_relative_inlet_flow_error,
                name="maximum_relative_inlet_flow_error",
            ),
        )
        object.__setattr__(
            self,
            "maximum_relative_pressure_drop_drift",
            positive_float(
                self.maximum_relative_pressure_drop_drift,
                name="maximum_relative_pressure_drop_drift",
            ),
        )
        object.__setattr__(
            self,
            "maximum_relative_turbulent_pressure_drop_drift",
            positive_float(
                self.maximum_relative_turbulent_pressure_drop_drift,
                name="maximum_relative_turbulent_pressure_drop_drift",
            ),
        )
        object.__setattr__(
            self,
            "minimum_steady_samples",
            integer_at_least(
                self.minimum_steady_samples,
                name="minimum_steady_samples",
                minimum=2,
            ),
        )
        object.__setattr__(
            self,
            "minimum_precursor_steady_samples",
            integer_at_least(
                self.minimum_precursor_steady_samples,
                name="minimum_precursor_steady_samples",
                minimum=10,
            ),
        )
        for name in (
            "maximum_mesh_non_orthogonality",
            "maximum_mesh_skewness",
            "maximum_mesh_aspect_ratio",
            "minimum_wall_y_plus",
            "maximum_wall_y_plus",
            "maximum_relative_turbulent_friction_error",
            "maximum_turbulent_outer_residual",
        ):
            object.__setattr__(
                self,
                name,
                positive_float(getattr(self, name), name=name),
            )
        if self.minimum_wall_y_plus >= self.maximum_wall_y_plus:
            raise ValueError(
                "minimum_wall_y_plus must be below maximum_wall_y_plus."
            )
        try:
            versions = tuple(self.validated_runtime_versions)
        except TypeError as error:
            raise ValueError(
                "validated_runtime_versions must be a sequence of version strings."
            ) from error
        if (
            not versions
            or any(not isinstance(version, str) or not version.strip() for version in versions)
            or len({_runtime_version_key(version) for version in versions}) != len(versions)
        ):
            raise ValueError(
                "validated_runtime_versions must contain unique non-empty version strings."
            )
        versions = tuple(version.strip() for version in versions)
        object.__setattr__(self, "validated_runtime_versions", versions)


def _pressure_drop_stability_check(
    history: History | None,
    *,
    policy: OpenFOAMValidationPolicy,
    turbulent: bool = False,
) -> Check:
    """Assess engineering-observable stability over the requested tail window."""

    enough_samples = bool(
        history is not None and len(history.values) >= policy.minimum_steady_samples
    )
    relative_drift: float | None = None
    if enough_samples and history is not None:
        tail = history.values[-policy.minimum_steady_samples :]
        scale = max(abs(tail[-1]), 1.0e-300)
        relative_drift = (max(tail) - min(tail)) / scale
    limit = (
        policy.maximum_relative_turbulent_pressure_drop_drift
        if turbulent
        else policy.maximum_relative_pressure_drop_drift
    )
    passed = bool(relative_drift is not None and relative_drift <= limit)
    return Check(
        name="pressure-drop-tail-stability",
        passed=passed,
        value=relative_drift,
        limit=limit,
        message=(
            f"Requires {policy.minimum_steady_samples} final pressure-drop samples."
            if not enough_samples
            else (
                "Relative range over the final pressure-drop samples; the RANS "
                "limit is intentionally separate from the laminar validation limit."
                if turbulent
                else "Relative range over the final pressure-drop samples."
            )
        ),
        kind="verification",
        observable="flow.pressure_drop",
    )


def _bounded_pipe_convergence(
    *,
    process_ok: bool,
    reached_end: bool,
    residual_check: Check,
    pressure_stability_check: Check,
    recovery_checks: tuple[Check, ...],
    explicit_marker: bool,
) -> tuple[bool, str]:
    """Resolve convergence from independent evidence for the bounded pipe case."""

    mass_balance_passed = next(
        (check.passed for check in recovery_checks if check.name == "mass-balance"),
        False,
    )
    converged = bool(
        process_ok
        and reached_end
        and residual_check.passed
        and pressure_stability_check.passed
        and mass_balance_passed
    )
    route = (
        "explicit-marker"
        if converged and explicit_marker
        else "axial-residual-pressure-stability-and-conservation"
        if converged
        else "insufficient-evidence"
    )
    return converged, route


def _mesh_metric_checks(
    quantities: dict[str, Quantity],
    *,
    policy: OpenFOAMValidationPolicy,
) -> tuple[Check, ...]:
    """Apply explicit limits to recovered checkMesh observables."""

    specifications = (
        (
            "mesh.maximum_non_orthogonality",
            policy.maximum_mesh_non_orthogonality,
        ),
        ("mesh.maximum_skewness", policy.maximum_mesh_skewness),
        ("mesh.maximum_aspect_ratio", policy.maximum_mesh_aspect_ratio),
    )
    return tuple(
        Check(
            name=f"{observable.removeprefix('mesh.').replace('_', '-')}-limit",
            passed=bool(
                observable in quantities and quantities[observable].value <= limit
            ),
            value=(quantities[observable].value if observable in quantities else None),
            limit=limit,
            message="Explicit AgentCFD mesh-quality promotion limit.",
            kind="verification",
            observable=observable,
        )
        for observable, limit in specifications
    )


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMCase:
    """Content-addressed record of a generated OpenFOAM case."""

    directory: Path
    model_sha256: str
    analysis_sha256: str
    case_sha256: str
    files: dict[str, str]
    capability: str = _LAMINAR_CAPABILITY
    schema: str = "agentcfd.openfoam-case/0.3"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "capability": self.capability,
            "model_sha256": self.model_sha256,
            "analysis_sha256": self.analysis_sha256,
            "case_sha256": self.case_sha256,
            "files": dict(sorted(self.files.items())),
            "provider": {
                "name": "openfoam",
                "execution_boundary": "filesystem-and-subprocess",
                "license": "GPL-3.0-or-later (external program)",
            },
        }


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMGridStudy:
    """A geometrically similar three-case OpenFOAM refinement plan."""

    directory: Path
    model_sha256: str
    cases: tuple[dict[str, object], ...]
    refinement_ratio: float
    scientific_inputs: dict[str, object]
    schema: str = "agentcfd.openfoam-grid-study/0.1"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "model_sha256": self.model_sha256,
            "quantity": "flow.pressure_drop",
            "refinement_ratio": self.refinement_ratio,
            "scientific_inputs": self.scientific_inputs,
            "cases": list(self.cases),
        }

    def write(self) -> Path:
        target = self.directory / "agentcfd-grid-study.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


def prepare_pipe_grid_study(
    step,
    directory: str | Path,
    *,
    cross_section_cells: tuple[int, int, int] = (8, 16, 32),
    base_axial_cells: int = 40,
) -> PreparedOpenFOAMGridStudy:
    """Prepare three same-model, geometrically similar circular-pipe cases."""

    selected = tuple(
        integer_at_least(value, name="cross_section_cells", minimum=2)
        for value in cross_section_cells
    )
    if len(selected) != 3:
        raise ValueError("Exactly three cross-section cell counts are required.")
    if not (selected[0] < selected[1] < selected[2]):
        raise ValueError("Cross-section cell counts must be strictly increasing.")
    ratio_21 = selected[1] / selected[0]
    ratio_32 = selected[2] / selected[1]
    if not math.isclose(ratio_21, ratio_32, rel_tol=1.0e-12):
        raise ValueError("Cross-section cell counts must use one refinement ratio.")
    base_axial = integer_at_least(
        base_axial_cells,
        name="base_axial_cells",
        minimum=2,
    )
    axial_counts: list[int] = []
    for cross_cells in selected:
        scaled = base_axial * cross_cells / selected[0]
        if not scaled.is_integer():
            raise ValueError("Axial cell counts must scale integrally with cross-section cells.")
        axial_counts.append(int(scaled))

    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"OpenFOAM grid-study directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    model_sha256 = step.model.fingerprint()
    cases: list[dict[str, object]] = []
    for index, (cross_cells, axial_cells) in enumerate(
        zip(selected, axial_counts),
        start=1,
    ):
        relative = Path(f"grid-{index}-c{cross_cells}-a{axial_cells}")
        prepared = OpenFOAMProvider(
            case_directory=root / relative,
            mesh=OpenFOAMMeshControls(
                cross_section_cells=cross_cells,
                axial_cells=axial_cells,
            ),
        ).prepare(step)
        if prepared.model_sha256 != model_sha256:
            raise RuntimeError("Prepared grid cases do not share one model identity.")
        cases.append(
            {
                "index": index,
                "label": ("coarse", "medium", "fine")[index - 1],
                "directory": str(relative),
                "cross_section_cells": cross_cells,
                "axial_cells": axial_cells,
                "expected_cell_count": 5 * cross_cells**2 * axial_cells,
                "case_sha256": prepared.case_sha256,
                "result": str(relative / "agentcfd-result.json"),
            }
        )
    study = PreparedOpenFOAMGridStudy(
        directory=root,
        model_sha256=model_sha256,
        cases=tuple(cases),
        refinement_ratio=ratio_21,
        scientific_inputs={
            "model": step.model.to_dict(),
            "procedure": step.procedure.to_dict(),
            "output_request": step.output.to_dict(),
        },
    )
    study.write()
    return study


class OpenFOAMProvider:
    """Lower a bounded model to an external ``simpleFoam`` workflow.

    The supported cases are intentionally narrow: steady, incompressible,
    isothermal, Newtonian flow through a smooth circular pipe, either laminar
    or the explicitly declared k-omega SST RANS slice. Case generation is
    deterministic and testable without an OpenFOAM installation. Execution
    additionally requires ``blockMesh``, ``checkMesh``, and ``simpleFoam``.
    """

    def __init__(
        self,
        *,
        case_directory: str | Path | None = None,
        precursor_case: str | Path | None = None,
        mesh: OpenFOAMMeshControls | None = None,
        validation: OpenFOAMValidationPolicy | None = None,
        timeout_seconds: float = 3600.0,
        container_image: str | None = None,
    ) -> None:
        self.case_directory = Path(case_directory) if case_directory is not None else None
        self.precursor_case = Path(precursor_case) if precursor_case is not None else None
        self.mesh = mesh or OpenFOAMMeshControls()
        self.validation = validation or OpenFOAMValidationPolicy()
        self.timeout_seconds = positive_float(timeout_seconds, name="timeout_seconds")
        self.container_image = str(container_image).strip() if container_image else None

    def _commands(self) -> dict[str, str | None]:
        names = ["blockMesh", "checkMesh", "simpleFoam"]
        if self.precursor_case is not None:
            names.insert(2, "mapFields")
        if self.container_image is not None:
            docker = shutil.which("docker")
            return {name: docker for name in names}
        return {name: shutil.which(name) for name in names}

    def _execution_argv(
        self,
        name: str,
        command: str,
        case_directory: Path,
        *,
        cidfile: Path | None = None,
    ) -> list[str]:
        if self.container_image is None:
            if name == "mapFields":
                if self.precursor_case is None:
                    raise RuntimeError("mapFields requires a precursor case.")
                return [
                    command,
                    str(self.precursor_case.resolve()),
                    "-case",
                    str(case_directory),
                    "-sourceTime",
                    "latestTime",
                    "-mapMethod",
                    "mapNearest",
                ]
            return [command, "-case", str(case_directory)]
        argv = [
            command,
            "run",
            "--rm",
        ]
        if cidfile is not None:
            argv.extend(("--cidfile", str(cidfile)))
        if name == "mapFields":
            if self.precursor_case is None:
                raise RuntimeError("mapFields requires a precursor case.")
            argv.extend(("-v", f"{self.precursor_case.resolve()}:/precursor:ro"))
        argv.extend(
            [
                "-v",
                f"{case_directory.resolve()}:/case",
                "-w",
                "/case",
                self.container_image,
                name,
            ]
        )
        if name == "mapFields":
            argv.extend(
                (
                    "/precursor",
                    "-case",
                    "/case",
                    "-sourceTime",
                    "latestTime",
                    "-mapMethod",
                    "mapNearest",
                )
            )
        else:
            argv.extend(("-case", "/case"))
        return argv

    def descriptor(self) -> ProviderDescriptor:
        commands = self._commands()
        available = all(commands.values())
        version = self.container_image or os.environ.get(
            "WM_PROJECT_VERSION", "externally-managed"
        )
        return ProviderDescriptor(
            name="openfoam",
            version=version,
            license="GPL-3.0-or-later (external program)",
            available=available,
            execution_boundary=(
                "filesystem-and-container-subprocess"
                if self.container_image
                else "filesystem-and-subprocess"
            ),
            capabilities=(_LAMINAR_CAPABILITY, _TURBULENT_CAPABILITY),
        )

    def prepare(self, step, directory: str | Path | None = None) -> PreparedOpenFOAMCase:
        """Validate and write a deterministic OpenFOAM case.

        The destination must not already contain files.  AgentCFD never deletes
        or silently overwrites an existing CFD case.
        """

        step.model.validate()
        self._validate_supported(step)
        mapping_contract = None
        if self.precursor_case is not None:
            mapping_contract = _precursor_mapping_contract(
                step,
                self.precursor_case,
                cross_section_cells=self.mesh.cross_section_cells,
                nominal_wall_cell_fraction=self.mesh.nominal_wall_cell_fraction,
            )
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM case_directory is required for prepare or run.")
        if target.exists() and any(target.iterdir()):
            raise FileExistsError(f"OpenFOAM case directory is not empty: {target}")
        rendered = self._render_files(step)
        if mapping_contract is not None:
            rendered["system/mapFieldsDict"] = _map_fields_dict()
            rendered["agentcfd-precursor-map.json"] = (
                json.dumps(mapping_contract, indent=2, sort_keys=True) + "\n"
            )
        target.mkdir(parents=True, exist_ok=True)

        hashes: dict[str, str] = {}
        for relative, content in sorted(rendered.items()):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            path.write_bytes(data)
            hashes[relative] = hashlib.sha256(data).hexdigest()

        case_identity = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prepared = PreparedOpenFOAMCase(
            directory=target,
            model_sha256=step.model.fingerprint(),
            analysis_sha256=_analysis_sha256(step),
            case_sha256=case_identity,
            files=hashes,
            capability=_case_capability(step),
        )
        manifest = target / "agentcfd-case.json"
        manifest.write_bytes(
            (json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
        return prepared

    def run(self, step) -> SimulationResult:
        """Prepare and execute the case through external OpenFOAM commands.

        A successful process is reported as completed execution evidence.
        Numerical convergence additionally requires OpenFOAM's explicit
        convergence marker. Patch flow and pressure histories are recovered
        independently so scientific checks never rely on log wording alone.
        """

        prepared = self.prepare(step)
        mesh_controls = _mesh_controls_from_case(prepared.directory)
        return self._execute_prepared(step, prepared, mesh_controls=mesh_controls)

    def run_prepared(
        self,
        step,
        directory: str | Path | None = None,
    ) -> SimulationResult:
        """Verify and execute an existing content-addressed case."""

        prepared = self._load_prepared_case(step, directory)
        execution_outputs = [
            path
            for path in prepared.directory.iterdir()
            if path.name.startswith("log.")
            or path.name == "postProcessing"
            or (path.is_dir() and _is_positive_time_name(path.name))
        ]
        if (prepared.directory / "constant" / "polyMesh").exists():
            execution_outputs.append(prepared.directory / "constant" / "polyMesh")
        if execution_outputs:
            names = ", ".join(sorted(path.name for path in execution_outputs))
            raise CaseIntegrityError(
                "Prepared OpenFOAM case already contains execution output; "
                f"use a fresh case to avoid mixed evidence: {names}"
            )
        unexpected = _unexpected_case_entries(prepared)
        if unexpected:
            raise CaseIntegrityError(
                "Prepared OpenFOAM case contains unrecorded entries that could alter "
                f"execution: {', '.join(unexpected)}"
            )
        mesh_controls = _mesh_controls_from_case(prepared.directory)
        self._verify_prepared_mapping(
            step,
            prepared,
            cross_section_cells=mesh_controls.cross_section_cells,
            nominal_wall_cell_fraction=mesh_controls.nominal_wall_cell_fraction,
        )
        return self._execute_prepared(step, prepared, mesh_controls=mesh_controls)

    def _verify_prepared_mapping(
        self,
        step,
        prepared: PreparedOpenFOAMCase,
        *,
        cross_section_cells: int | None = None,
        nominal_wall_cell_fraction: float | None = None,
    ) -> None:
        mapping_path = prepared.directory / "agentcfd-precursor-map.json"
        if self.precursor_case is None:
            if mapping_path.exists():
                raise CaseIntegrityError(
                    "The prepared case requires a precursor, but none was supplied."
                )
            return
        if not mapping_path.is_file():
            raise CaseIntegrityError(
                "The selected precursor was not recorded in the prepared target case."
            )
        expected = strict_json_object(
            mapping_path.read_text(encoding="utf-8"),
            label=f"OpenFOAM precursor mapping contract {mapping_path}",
        )
        actual = _precursor_mapping_contract(
            step,
            self.precursor_case,
            cross_section_cells=(
                self.mesh.cross_section_cells
                if cross_section_cells is None
                else cross_section_cells
            ),
            nominal_wall_cell_fraction=(
                self.mesh.nominal_wall_cell_fraction
                if cross_section_cells is None
                else nominal_wall_cell_fraction
            ),
        )
        if expected != actual:
            raise CaseIntegrityError(
                "The precursor identity or mapping contract changed after target preparation."
            )

    def _load_prepared_case(
        self,
        step,
        directory: str | Path | None = None,
    ) -> PreparedOpenFOAMCase:
        step.model.validate()
        self._validate_supported(step)
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM case_directory is required for prepared execution.")
        manifest_path = target / "agentcfd-case.json"
        try:
            manifest = strict_json_object(
                manifest_path.read_text(encoding="utf-8"),
                label=f"OpenFOAM case manifest {manifest_path}",
            )
        except (FileNotFoundError, ValueError) as error:
            raise CaseIntegrityError(
                f"Prepared OpenFOAM case manifest is missing or invalid: {manifest_path}"
            ) from error
        if manifest.get("schema") != "agentcfd.openfoam-case/0.3":
            raise CaseIntegrityError("Prepared OpenFOAM case schema is unsupported.")
        capability = _case_capability(step)
        if manifest.get("capability") != capability:
            raise CaseIntegrityError(
                "Prepared OpenFOAM case belongs to a different provider capability."
            )
        model_sha256 = step.model.fingerprint()
        if manifest.get("model_sha256") != model_sha256:
            raise CaseIntegrityError(
                "Prepared OpenFOAM case belongs to a different scientific model."
            )
        analysis_sha256 = _analysis_sha256(step)
        if manifest.get("analysis_sha256") != analysis_sha256:
            raise CaseIntegrityError(
                "Prepared OpenFOAM case belongs to a different analysis procedure or output request."
            )
        recorded_files = manifest.get("files")
        if not isinstance(recorded_files, dict) or not recorded_files:
            raise CaseIntegrityError("Prepared OpenFOAM case has no recorded files.")

        verified_files: dict[str, str] = {}
        root = target.resolve()
        for relative, expected in recorded_files.items():
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise CaseIntegrityError("Prepared case file identities are malformed.")
            path = (target / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise CaseIntegrityError(
                    f"Prepared case file escapes the case directory: {relative!r}"
                ) from error
            if not path.is_file():
                raise CaseIntegrityError(f"Prepared case file is missing: {relative}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise CaseIntegrityError(f"Prepared case file has changed: {relative}")
            verified_files[relative] = actual

        case_sha256 = hashlib.sha256(
            json.dumps(
                verified_files,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if manifest.get("case_sha256") != case_sha256:
            raise CaseIntegrityError("Prepared OpenFOAM combined case identity has changed.")
        return PreparedOpenFOAMCase(
            directory=target,
            model_sha256=model_sha256,
            analysis_sha256=analysis_sha256,
            case_sha256=case_sha256,
            files=verified_files,
            capability=capability,
        )

    def _execute_prepared(
        self,
        step,
        prepared: PreparedOpenFOAMCase,
        *,
        mesh_controls: OpenFOAMMeshControls,
    ) -> SimulationResult:
        self._verify_prepared_mapping(
            step,
            prepared,
            cross_section_cells=mesh_controls.cross_section_cells,
            nominal_wall_cell_fraction=mesh_controls.nominal_wall_cell_fraction,
        )
        mapping_contract: dict[str, Any] | None = None
        if self.precursor_case is not None:
            mapping_contract = strict_json_object(
                (prepared.directory / "agentcfd-precursor-map.json").read_text(
                    encoding="utf-8"
                ),
                label="prepared OpenFOAM precursor mapping contract",
            )
        commands = self._commands()
        missing = [name for name, path in commands.items() if path is None]
        if missing:
            if self.container_image is not None:
                raise ProviderUnavailableError(
                    "OpenFOAM container execution requires docker on PATH."
                )
            raise ProviderUnavailableError(
                "OpenFOAM execution requires commands on PATH: " + ", ".join(missing)
            )

        logs: dict[str, str] = {}
        return_codes: dict[str, int] = {}
        command_wall_seconds: dict[str, float] = {}
        for name in commands:
            cidfile = (
                prepared.directory / f".agentcfd-{name}.cid"
                if self.container_image is not None
                else None
            )
            started_at = time.monotonic()
            try:
                completed = subprocess.run(
                    self._execution_argv(
                        name,
                        str(commands[name]),
                        prepared.directory,
                        cidfile=cidfile,
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                combined = completed.stdout + completed.stderr
                return_codes[name] = completed.returncode
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
                combined = stdout + stderr + f"\nAgentCFD timeout after {self.timeout_seconds:g} seconds.\n"
                if cidfile is not None:
                    combined += _stop_timed_out_container(
                        str(commands[name]),
                        cidfile,
                    ) + "\n"
                return_codes[name] = -124
            except KeyboardInterrupt:
                if cidfile is not None:
                    _stop_timed_out_container(str(commands[name]), cidfile)
                raise
            finally:
                command_wall_seconds[name] = time.monotonic() - started_at
                if cidfile is not None:
                    cidfile.unlink(missing_ok=True)
            logs[name] = combined
            (prepared.directory / f"log.{name}").write_text(combined, encoding="utf-8")
            if return_codes[name] != 0:
                break

        if return_codes.get("mapFields") == 0:
            self._verify_prepared_mapping(
                step,
                prepared,
                cross_section_cells=mesh_controls.cross_section_cells,
                nominal_wall_cell_fraction=mesh_controls.nominal_wall_cell_fraction,
            )

        process_ok = all(return_codes.get(name) == 0 for name in commands)
        solver_log = logs.get("simpleFoam", "")
        reached_end = process_ok and bool(re.search(r"(?m)^End\s*$", solver_log))
        explicit_solver_converged = process_ok and _solver_converged(solver_log)
        container_identity: dict[str, Any] | None = None
        container_checks: tuple[Check, ...] = ()
        if self.container_image is not None:
            docker_command = next(
                (str(command) for command in commands.values() if command is not None),
                "docker",
            )
            container_identity = _container_image_identity(
                docker_command,
                self.container_image,
                timeout_seconds=self.timeout_seconds,
            )
            container_checks = (
                Check(
                    name="container-image-identity",
                    passed=bool(container_identity["identity_verified"]),
                    value=(
                        container_identity.get("image_id")
                        or container_identity.get("inspection_error", "missing")
                    ),
                    limit="immutable local image SHA-256 is recorded",
                    kind="runtime",
                    observable="provider.container_identity",
                ),
            )
        turbulent = _is_turbulent_step(step)
        colebrook_reference_drop = self._reference_pressure_drop(step)
        reference_drop = (
            float(mapping_contract["source_pressure_gradient_pa_per_m"])
            * step.model.domain.length
            if mapping_contract is not None
            else colebrook_reference_drop
        )
        mean_velocity = self._mean_velocity(step.model)
        requested_volume_flow = (
            math.pi * step.model.domain.diameter**2 / 4.0 * mean_velocity
        )
        fully_developed_inlet = any(
            isinstance(value, boundaries.FullyDevelopedVelocityInlet)
            for value in step.model.boundary_conditions.values()
        ) or mapping_contract is not None
        quantities, histories, recovery_checks, recovery_message = _recover_patch_data(
            prepared.directory,
            density=step.model.fluid.density,
            reference_pressure_drop=reference_drop,
            solver_tolerance=step.procedure.relative_tolerance,
            reference_pressure_drop_per_flow=(
                None
                if turbulent
                else 128.0
                * step.model.fluid.dynamic_viscosity
                * step.model.domain.length
                / (math.pi * step.model.domain.diameter**4)
            ),
            requested_volume_flow=requested_volume_flow,
            pressure_error_limit=self.validation.maximum_relative_pressure_error,
            mass_balance_limit=self.validation.maximum_relative_mass_imbalance,
            inlet_flow_error_limit=self.validation.maximum_relative_inlet_flow_error,
            pressure_reference_applicable=fully_developed_inlet,
            pressure_reference_kind=(
                "verification" if mapping_contract is not None else "validation"
            ),
            pressure_reference_requirement=(
                "accepted matching-resolution periodic precursor"
                if mapping_contract is not None
                else "developed turbulent inlet and grid evidence"
                if turbulent
                else "fully developed laminar inlet boundary"
            ),
            validation_message=(
                "A content-addressed periodic precursor supplies the developed internal field."
                if mapping_contract is not None
                else
                "The fully developed profile isolates spatial and outlet-boundary error."
                if fully_developed_inlet
                else "Turbulent friction is diagnostic until developed-inlet and grid evidence pass."
                if turbulent
                else "The uniform inlet includes developing-flow effects in the total pressure drop."
            ),
        )
        if mapping_contract is not None:
            quantities["reference.flow.precursor_pressure_drop"] = Quantity(
                reference_drop,
                "Pa",
                description="Periodic-precursor pressure gradient integrated over target length.",
            )
            quantities["reference.flow.colebrook_pressure_drop"] = Quantity(
                colebrook_reference_drop,
                "Pa",
                description="Smooth-pipe Colebrook comparison retained as a model-form diagnostic.",
            )
        turbulence_checks: tuple[Check, ...] = ()
        if turbulent:
            turbulence_quantities, turbulence_histories, turbulence_checks = (
                _recover_turbulence_data(
                    prepared.directory,
                    density=step.model.fluid.density,
                    dynamic_viscosity=step.model.fluid.dynamic_viscosity,
                    mean_velocity=mean_velocity,
                    diameter=step.model.domain.diameter,
                    length=step.model.domain.length,
                    pressure_drop=quantities.get("flow.pressure_drop"),
                    policy=self.validation,
                )
            )
            quantities.update(turbulence_quantities)
            histories.update(turbulence_histories)
            inlet = next(
                value
                for value in step.model.boundary_conditions.values()
                if isinstance(value, boundaries.TurbulentMeanVelocityInlet)
            )
            inlet_estimate = engineering.turbulence_inlet_from_intensity(
                mean_velocity=mean_velocity,
                intensity=inlet.turbulence_intensity,
                length_scale=inlet.turbulence_length_scale,
            )
            quantities["turbulence.inlet.kinetic_energy"] = Quantity(
                inlet_estimate.turbulent_kinetic_energy,
                "m^2/s^2",
                kind="scientific_input",
            )
            quantities["turbulence.inlet.specific_dissipation_rate"] = Quantity(
                inlet_estimate.specific_dissipation_rate,
                "1/s",
                kind="scientific_input",
            )
        for name, duration in command_wall_seconds.items():
            quantities[f"runtime.{name}.wall_seconds"] = Quantity(
                duration,
                "s",
                kind="runtime_metric",
            )
        quantities["runtime.total_wall_seconds"] = Quantity(
            sum(command_wall_seconds.values()),
            "s",
            kind="runtime_metric",
        )
        mesh_quality_quantities = _mesh_quality_quantities(logs.get("checkMesh", ""))
        quantities.update(mesh_quality_quantities)
        quantities["mesh.nominal_wall_cell_height"] = Quantity(
            _nominal_wall_cell_height(
                radius=step.model.domain.diameter / 2.0,
                cross_cells=mesh_controls.cross_section_cells,
                nominal_wall_cell_fraction=mesh_controls.nominal_wall_cell_fraction,
            ),
            "m",
            kind="scientific_input",
            description=(
                "Design height on the O-grid corner-to-core radial edge; curved-face "
                "cell-centre wall distance is recovered separately through y+."
            ),
        )
        quantities["mesh.wall_to_core_expansion_ratio"] = Quantity(
            _wall_normal_expansion_ratio(
                mesh_controls.cross_section_cells,
                mesh_controls.nominal_wall_cell_fraction,
            ),
            "1",
            kind="scientific_input",
        )
        mesh_metric_checks = _mesh_metric_checks(
            mesh_quality_quantities,
            policy=self.validation,
        )
        expected_cell_count = (
            5 * mesh_controls.cross_section_cells**2 * int(mesh_controls.axial_cells or 0)
        )
        actual_cell_count = quantities.get("mesh.cell_count")
        quantities["mesh.expected_cell_count"] = Quantity(
            float(expected_cell_count),
            "1",
            kind="verification_metric",
        )
        mesh_count_matches = bool(
            expected_cell_count > 0
            and actual_cell_count is not None
            and actual_cell_count.value == expected_cell_count
        )
        residual_quantities, residual_histories = _solver_residual_evidence(solver_log)
        quantities.update(residual_quantities)
        histories.update(residual_histories)
        residual_check = _outer_residual_check(
            residual_quantities,
            tolerance=(
                self.validation.maximum_turbulent_outer_residual
                if turbulent
                else step.procedure.relative_tolerance
            ),
            axial_velocity_component=(None if explicit_solver_converged else "Uz"),
            additional_fields=(("k", "omega") if turbulent else ()),
        )
        pressure_stability_check = _pressure_drop_stability_check(
            histories.get("flow.pressure_drop"),
            policy=self.validation,
            turbulent=turbulent,
        )
        solver_converged, convergence_route = _bounded_pipe_convergence(
            process_ok=process_ok,
            reached_end=reached_end,
            residual_check=residual_check,
            pressure_stability_check=pressure_stability_check,
            recovery_checks=recovery_checks,
            explicit_marker=explicit_solver_converged,
        )
        reynolds = engineering.reynolds_number(
            density=step.model.fluid.density,
            mean_velocity=mean_velocity,
            hydraulic_diameter=step.model.domain.diameter,
            dynamic_viscosity=step.model.fluid.dynamic_viscosity,
        )
        quantities["flow.reynolds_number"] = Quantity(reynolds, "1")
        if any(
            isinstance(value, (boundaries.MassFlowInlet, boundaries.MeanVelocityInlet))
            for value in step.model.boundary_conditions.values()
        ):
            entrance_length = engineering.laminar_hydrodynamic_entrance_length(
                reynolds=reynolds,
                hydraulic_diameter=step.model.domain.diameter,
            )
            quantities["flow.laminar_entrance_length_estimate"] = Quantity(
                entrance_length,
                "m",
                kind="diagnostic",
                description="Screening estimate 0.05 Re D for a uniform laminar inlet.",
            )
            quantities["flow.pipe_to_entrance_length_ratio"] = Quantity(
                step.model.domain.length / entrance_length,
                "1",
                kind="diagnostic",
            )
        mesh_sha256, mesh_manifest = _write_mesh_manifest(prepared.directory)
        artifact_paths = {
            "case_manifest": prepared.directory / "agentcfd-case.json",
            **{
                f"log_{name}": prepared.directory / f"log.{name}"
                for name in logs
            },
        }
        if mapping_contract is not None:
            artifact_paths["precursor_mapping_contract"] = (
                prepared.directory / "agentcfd-precursor-map.json"
            )
        if mesh_manifest is not None:
            artifact_paths["mesh_manifest"] = mesh_manifest
        fields: dict[str, FieldRecord] = {}
        latest_time = _latest_time_directory(prepared.directory)
        if latest_time is not None:
            field_units = (
                ("U", "m/s"),
                ("p", "m^2/s^2"),
                *((
                    ("k", "m^2/s^2"),
                    ("omega", "1/s"),
                    ("nut", "m^2/s"),
                ) if turbulent else ()),
            )
            for name, unit in field_units:
                path = latest_time / name
                if path.is_file():
                    fields[name] = FieldRecord(
                        location="cell",
                        unit=unit,
                        artifact=str(path),
                        components=("x", "y", "z") if name == "U" else (),
                        mesh_sha256=mesh_sha256,
                    )
                    artifact_paths[f"field_{name}"] = path
        missing_outputs = [
            name
            for name in step.output.fields
            if _OUTPUT_FIELD_KEYS[name] not in fields
        ] + [
            name
            for name in step.output.histories
            if _OUTPUT_HISTORY_KEYS[name] not in histories
        ]
        descriptor = self.descriptor()
        runtime_version = _runtime_version(logs, descriptor.version)
        runtime_version_validated = _runtime_version_key(runtime_version) in {
            _runtime_version_key(version)
            for version in self.validation.validated_runtime_versions
        }
        return SimulationResult(
            status="completed" if process_ok else "failed",
            converged=solver_converged,
            provider="openfoam",
            quantities=quantities,
            histories=histories,
            fields=fields,
            checks=(
                Check(
                    name="openfoam-process",
                    passed=process_ok,
                    value=json.dumps(return_codes, sort_keys=True),
                    limit="all return codes equal zero",
                    kind="runtime",
                    observable="provider.process",
                ),
                Check(
                    name="solver-completion-marker",
                    passed=reached_end,
                    value="found" if reached_end else "missing",
                    limit="OpenFOAM log ends with End",
                    kind="runtime",
                    observable="provider.convergence_marker",
                ),
                Check(
                    name="solver-convergence-marker",
                    passed=solver_converged,
                    value=convergence_route,
                    limit=(
                        "explicit marker or bounded-pipe axial residual, pressure stability, "
                        "and conservation evidence"
                    ),
                    message=(
                        "Zero-target transverse components gate on final linear residuals "
                        "instead of ill-conditioned normalized outer initial residuals."
                        if solver_converged and not explicit_solver_converged
                        else "A normal End marker alone does not prove numerical convergence."
                    ),
                    kind="verification",
                    observable="provider.numerical_convergence",
                ),
                residual_check,
                pressure_stability_check,
                Check(
                    name="mesh-quality",
                    passed=(
                        return_codes.get("checkMesh") == 0
                        and "Mesh OK" in logs.get("checkMesh", "")
                    ),
                    value=(
                        "Mesh OK"
                        if "Mesh OK" in logs.get("checkMesh", "")
                        else "not confirmed"
                    ),
                    limit="checkMesh succeeds and reports Mesh OK",
                    kind="verification",
                    observable="mesh.quality",
                ),
                *mesh_metric_checks,
                Check(
                    name="mesh-cell-count",
                    passed=mesh_count_matches,
                    value=(actual_cell_count.value if actual_cell_count is not None else None),
                    limit=float(expected_cell_count),
                    kind="verification",
                    observable="mesh.cell_count",
                ),
                Check(
                    name="mesh-identity",
                    passed=mesh_sha256 is not None,
                    value=mesh_sha256 or "missing",
                    limit="content-addressed polyMesh manifest exists",
                    kind="verification",
                    observable="mesh.identity",
                ),
                Check(
                    name="openfoam-runtime-version",
                    passed=runtime_version_validated,
                    value=runtime_version,
                    limit=(
                        "validated versions: "
                        + ", ".join(self.validation.validated_runtime_versions)
                    ),
                    message=(
                        "Case generation is validated against the OpenCFD v2606 dialect; "
                        "other distributions or versions require an explicit validation run."
                    ),
                    kind="runtime",
                    observable="provider.version",
                ),
                Check(
                    name="requested-output-completeness",
                    passed=not missing_outputs,
                    value=(
                        "complete"
                        if not missing_outputs
                        else ", ".join(missing_outputs)
                    ),
                    limit="all requested fields and histories are recovered",
                    kind="runtime",
                    observable="result.outputs",
                ),
                *(
                    (
                        Check(
                            name="precursor-field-mapping",
                            passed=(
                                return_codes.get("mapFields") == 0
                                and bool(re.search(r"(?m)^End\s*$", logs.get("mapFields", "")))
                            ),
                            value=(
                                "U,p,k,omega,nut mapped"
                                if return_codes.get("mapFields") == 0
                                else return_codes.get("mapFields")
                            ),
                            limit="mapFields succeeds and reports End",
                            kind="verification",
                            observable="boundary.precursor_mapping",
                        ),
                        Check(
                            name="precursor-runtime-compatibility",
                            passed=(
                                _runtime_version_key(
                                    str(mapping_contract["source_runtime_version"])
                                )
                                == _runtime_version_key(runtime_version)
                            ),
                            value=(
                                f"source={mapping_contract['source_runtime_version']}, "
                                f"target={runtime_version}"
                            ),
                            limit="source and target OpenFOAM runtime versions match",
                            kind="runtime",
                            observable="provider.version",
                        ),
                    )
                    if mapping_contract is not None
                    else ()
                ),
                *container_checks,
                *recovery_checks,
                *turbulence_checks,
            ),
            artifacts={
                name: Artifact.from_path(
                    path,
                    role="execution-evidence",
                    media_type=("application/json" if path.suffix == ".json" else "text/plain"),
                )
                for name, path in artifact_paths.items()
            },
            scientific_inputs={
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
                "mesh_controls": asdict(mesh_controls),
                "validation_policy": asdict(self.validation),
                "lowered_case_sha256": prepared.case_sha256,
                "analysis_sha256": prepared.analysis_sha256,
                **(
                    {"precursor_mapping": mapping_contract}
                    if mapping_contract is not None
                    else {}
                ),
            },
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": step.model.fingerprint(),
                "analysis_sha256": prepared.analysis_sha256,
                "case_sha256": prepared.case_sha256,
                "mesh_sha256": mesh_sha256,
                "provider": "openfoam",
                "provider_version": runtime_version,
                "execution_boundary": descriptor.execution_boundary,
                "container_image": self.container_image,
                "container_identity": container_identity,
                "command_return_codes": return_codes,
                "command_wall_seconds": command_wall_seconds,
                "explicit_solver_convergence_marker": explicit_solver_converged,
                "convergence_route": convergence_route,
                "case_manifest": str(prepared.directory / "agentcfd-case.json"),
                **(
                    {
                        "precursor_case": str(self.precursor_case.resolve()),
                        "precursor_result_sha256": mapping_contract[
                            "source_result_sha256"
                        ],
                        "precursor_mesh_sha256": mapping_contract[
                            "source_mesh_sha256"
                        ],
                    }
                    if mapping_contract is not None and self.precursor_case is not None
                    else {}
                ),
            },
            messages=(recovery_message,),
        )

    def _validate_supported(self, step) -> None:
        model = step.model
        study = model.study
        unsupported_fields = sorted(set(step.output.fields) - _OUTPUT_FIELD_KEYS.keys())
        unsupported_histories = sorted(
            set(step.output.histories) - _OUTPUT_HISTORY_KEYS.keys()
        )
        if unsupported_fields or unsupported_histories:
            unsupported = ", ".join((*unsupported_fields, *unsupported_histories))
            raise UnsupportedCaseError(
                f"The OpenFOAM pipe provider cannot recover requested outputs: {unsupported}."
            )
        if not study.steady or study.compressible or study.energy or study.reacting:
            raise UnsupportedCaseError(
                "The OpenFOAM pipe provider supports steady, incompressible, "
                "isothermal, Newtonian internal flow only."
            )
        if study.turbulence not in (None, "k-omega-sst"):
            raise UnsupportedCaseError(
                "The OpenFOAM pipe provider supports laminar or k-omega-sst flow only."
            )
        if not study.laminar and study.wall_treatment != "blended-wall-functions":
            raise UnsupportedCaseError(
                "The k-omega-sst pipe slice currently supports only the explicitly "
                "declared blended-wall-functions treatment."
            )
        if model.domain.roughness != 0.0:
            raise UnsupportedCaseError("The initial OpenFOAM provider requires a smooth pipe.")
        wall_conditions = [
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.NoSlipWall)
        ]
        if any(condition.roughness not in (None, 0.0) for condition in wall_conditions):
            raise UnsupportedCaseError("Rough-wall lowering is not implemented.")
        if len(wall_conditions) != 1:
            raise UnsupportedCaseError("The initial OpenFOAM pipe mesh requires exactly one wall boundary.")
        reynolds = (
            model.fluid.density
            * self._mean_velocity(model)
            * model.domain.diameter
            / model.fluid.dynamic_viscosity
        )
        turbulent_inlets = [
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.TurbulentMeanVelocityInlet)
        ]
        if study.laminar and turbulent_inlets:
            raise UnsupportedCaseError(
                "A turbulent inlet requires a turbulent Study."
            )
        if not study.laminar and len(turbulent_inlets) != 1:
            raise UnsupportedCaseError(
                "The k-omega-sst pipe slice requires exactly one turbulent mean-velocity inlet."
            )
        if not study.laminar and any(
            isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                ),
            )
            for value in model.boundary_conditions.values()
        ):
            raise UnsupportedCaseError(
                "The k-omega-sst pipe slice requires the explicit turbulent inlet boundary."
            )
        if study.laminar and reynolds >= 2300.0:
            raise UnsupportedCaseError(
                f"Re={reynolds:.6g} is outside the declared laminar provider range Re < 2300."
            )
        if not study.laminar and reynolds < 4000.0:
            raise UnsupportedCaseError(
                f"Re={reynolds:.6g} is outside the declared turbulent provider range Re >= 4000."
            )
        if self.precursor_case is not None and study.laminar:
            raise UnsupportedCaseError(
                "Periodic precursor mapping is available only for k-omega-sst turbulent flow."
            )
        turbulence_fields = {
            "turbulence.kinetic_energy",
            "turbulence.specific_dissipation_rate",
            "turbulence.kinematic_eddy_viscosity",
        }
        turbulence_histories = {"wall.y_plus"}
        if study.laminar and (
            set(step.output.fields) & turbulence_fields
            or set(step.output.histories) & turbulence_histories
        ):
            raise UnsupportedCaseError(
                "Laminar flow cannot produce turbulence fields or wall y-plus."
            )
        for name in model.boundary_conditions:
            if not _FOAM_WORD.fullmatch(name):
                raise UnsupportedCaseError(
                    f"Boundary name {name!r} is not a valid OpenFOAM word; use letters, digits, and underscores."
                )

    def _render_files(self, step) -> dict[str, str]:
        model = step.model
        inlet_name, inlet = next(
            (name, value)
            for name, value in model.boundary_conditions.items()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                    boundaries.TurbulentMeanVelocityInlet,
                ),
            )
        )
        outlet_name, outlet = next(
            (name, value)
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.PressureOutlet)
        )
        wall_names = tuple(
            name
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.NoSlipWall)
        )
        rho = model.fluid.density
        mean_velocity = self._mean_velocity(model)
        outlet_kinematic_pressure = outlet.gauge_pressure / rho

        axial_cells = self.mesh.axial_cells
        if axial_cells is None:
            axial_cells = max(20, min(800, math.ceil(2.0 * model.domain.length / model.domain.diameter)))

        turbulent = _is_turbulent_step(step)
        files = {
            "0/U": _velocity_field(
                inlet_name,
                outlet_name,
                wall_names,
                mean_velocity,
                radius=model.domain.diameter / 2.0,
                fully_developed=isinstance(inlet, boundaries.FullyDevelopedVelocityInlet),
                flow_rate_constrained=isinstance(
                    inlet,
                    boundaries.TurbulentMeanVelocityInlet,
                ),
            ),
            "0/p": _pressure_field(inlet_name, outlet_name, wall_names, outlet_kinematic_pressure),
            "constant/transportProperties": _transport_properties(model.fluid.kinematic_viscosity),
            "constant/turbulenceProperties": _turbulence_properties(turbulent=turbulent),
            "system/blockMeshDict": _block_mesh_dict(
                length=model.domain.length,
                radius=model.domain.diameter / 2.0,
                cross_cells=self.mesh.cross_section_cells,
                axial_cells=axial_cells,
                inlet=inlet_name,
                outlet=outlet_name,
                walls=wall_names,
                nominal_wall_cell_fraction=self.mesh.nominal_wall_cell_fraction,
            ),
            "system/controlDict": _control_dict(
                step.procedure.maximum_iterations,
                inlet=inlet_name,
                outlet=outlet_name,
                turbulent=turbulent,
            ),
            "system/fvSchemes": _fv_schemes(turbulent=turbulent),
            "system/fvSolution": _fv_solution(
                step.procedure.relative_tolerance,
                turbulent=turbulent,
                strict_velocity_solve=self.precursor_case is not None,
            ),
        }
        if turbulent:
            estimate = engineering.turbulence_inlet_from_intensity(
                mean_velocity=mean_velocity,
                intensity=inlet.turbulence_intensity,
                length_scale=inlet.turbulence_length_scale,
            )
            files.update(
                _turbulence_fields(
                    inlet_name,
                    outlet_name,
                    wall_names,
                    kinetic_energy=estimate.turbulent_kinetic_energy,
                    specific_dissipation_rate=estimate.specific_dissipation_rate,
                    mapped_inlet=self.precursor_case is not None,
                )
            )
        return files

    @staticmethod
    def _reference_pressure_drop(step) -> float:
        model = step.model
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                    boundaries.TurbulentMeanVelocityInlet,
                ),
            )
        )
        if isinstance(inlet, boundaries.MassFlowInlet):
            velocity = inlet.mass_flow_rate / (model.fluid.density * model.domain.area)
        else:
            velocity = inlet.velocity
        if model.study.laminar:
            return (
                32.0
                * model.fluid.dynamic_viscosity
                * model.domain.length
                * velocity
                / model.domain.diameter**2
            )
        reynolds = engineering.reynolds_number(
            density=model.fluid.density,
            mean_velocity=velocity,
            hydraulic_diameter=model.domain.diameter,
            dynamic_viscosity=model.fluid.dynamic_viscosity,
        )
        friction = engineering.darcy_friction_factor(reynolds)
        return engineering.darcy_weisbach_pressure_loss(
            friction_factor=friction,
            length=model.domain.length,
            hydraulic_diameter=model.domain.diameter,
            density=model.fluid.density,
            mean_velocity=velocity,
        )

    @staticmethod
    def _mean_velocity(model) -> float:
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(
                value,
                (
                    boundaries.MassFlowInlet,
                    boundaries.MeanVelocityInlet,
                    boundaries.FullyDevelopedVelocityInlet,
                    boundaries.TurbulentMeanVelocityInlet,
                ),
            )
        )
        if isinstance(inlet, boundaries.MassFlowInlet):
            return inlet.mass_flow_rate / (model.fluid.density * model.domain.area)
        return inlet.velocity


def _header(*, object_name: str, class_name: str, location: str | None = None) -> str:
    location_line = f'    location    "{location}";\n' if location is not None else ""
    return (
        "/* Generated by AgentCFD. OpenFOAM is an external GPL program. */\n"
        "FoamFile\n{\n"
        "    version     2.0;\n"
        "    format      ascii;\n"
        f"    class       {class_name};\n"
        f"{location_line}"
        f"    object      {object_name};\n"
        "}\n\n"
    )


def _velocity_field(
    inlet: str,
    outlet: str,
    walls: tuple[str, ...],
    velocity: float,
    *,
    radius: float,
    fully_developed: bool,
    flow_rate_constrained: bool = False,
) -> str:
    wall_blocks = "\n".join(
        f"    {name}\n    {{\n        type noSlip;\n    }}" for name in walls
    )
    if fully_developed and flow_rate_constrained:
        raise ValueError("An inlet cannot use both analytic and flow-rate-constrained profiles.")
    if fully_developed:
        target_volume_flow = math.pi * radius**2 * velocity
        radial_profile = (
            f"(1 - (sqr(pos().x()) + sqr(pos().y()))/{radius**2:.17g})"
        )
        inlet_block = f"""        type uniformFixedValue;
        value uniform (0 0 {velocity:.17g});
        uniformValue
        {{
            type expression;
            expression "vector(0, 0, ({target_volume_flow:.17g})*{radial_profile}/weightSum({radial_profile}))";
        }}"""
    elif flow_rate_constrained:
        target_volume_flow = math.pi * radius**2 * velocity
        inlet_block = f"""        type flowRateInletVelocity;
        volumetricFlowRate constant {target_volume_flow:.17g};
        extrapolateProfile yes;
        value uniform (0 0 {velocity:.17g});"""
    else:
        inlet_block = f"""        type fixedValue;
        value uniform (0 0 {velocity:.17g});"""
    return _header(object_name="U", class_name="volVectorField", location="0") + f"""dimensions      [0 1 -1 0 0 0 0];
internalField   uniform (0 0 {velocity:.17g});
boundaryField
{{
    {inlet}
    {{
{inlet_block}
    }}
    {outlet}
    {{
        type zeroGradient;
    }}
{wall_blocks}
}}
"""


def _pressure_field(inlet: str, outlet: str, walls: tuple[str, ...], pressure: float) -> str:
    wall_blocks = "\n".join(
        f"    {name}\n    {{\n        type zeroGradient;\n    }}" for name in walls
    )
    return _header(object_name="p", class_name="volScalarField", location="0") + f"""dimensions      [0 2 -2 0 0 0 0];
internalField   uniform {pressure:.17g};
boundaryField
{{
    {inlet}
    {{
        type zeroGradient;
    }}
    {outlet}
    {{
        type fixedValue;
        value uniform {pressure:.17g};
    }}
{wall_blocks}
}}
"""


def _transport_properties(nu: float) -> str:
    return _header(object_name="transportProperties", class_name="dictionary", location="constant") + f"""transportModel Newtonian;
nu              [0 2 -1 0 0 0 0] {nu:.17g};
"""


def _turbulence_properties(
    *,
    turbulent: bool = False,
    turbulence_model: str = "k-omega-sst",
) -> str:
    model_names = {
        "k-omega-sst": "kOmegaSST",
        "k-epsilon": "kEpsilon",
    }
    if turbulence_model not in model_names:
        raise ValueError("Unsupported OpenFOAM turbulence model.")
    model_name = model_names[turbulence_model]
    body = """simulationType RAS;

RAS
{
    RASModel        MODEL_NAME;
    turbulence      on;
    printCoeffs     on;
}
""".replace("MODEL_NAME", model_name) if turbulent else """simulationType laminar;
"""
    return _header(
        object_name="turbulenceProperties",
        class_name="dictionary",
        location="constant",
    ) + body


def _turbulence_fields(
    inlet: str,
    outlet: str,
    walls: tuple[str, ...],
    *,
    kinetic_energy: float,
    specific_dissipation_rate: float,
    mapped_inlet: bool = False,
) -> dict[str, str]:
    wall_k = "\n".join(
        f"""    {name}
    {{
        type kqRWallFunction;
        value uniform {kinetic_energy:.17g};
    }}"""
        for name in walls
    )
    wall_omega = "\n".join(
        f"""    {name}
    {{
        type omegaWallFunction;
        blending binomial;
        value uniform {specific_dissipation_rate:.17g};
    }}"""
        for name in walls
    )
    wall_nut = "\n".join(
        f"""    {name}
    {{
        type nutUBlendedWallFunction;
        value uniform 0;
    }}"""
        for name in walls
    )
    k_inlet = (
        "        type zeroGradient;"
        if mapped_inlet
        else f"        type fixedValue;\n        value uniform {kinetic_energy:.17g};"
    )
    omega_inlet = (
        "        type zeroGradient;"
        if mapped_inlet
        else (
            "        type fixedValue;\n"
            f"        value uniform {specific_dissipation_rate:.17g};"
        )
    )
    k = _header(object_name="k", class_name="volScalarField", location="0") + f"""dimensions      [0 2 -2 0 0 0 0];
internalField   uniform {kinetic_energy:.17g};
boundaryField
{{
    {inlet}
    {{
{k_inlet}
    }}
    {outlet}
    {{
        type zeroGradient;
    }}
{wall_k}
}}
"""
    omega = _header(object_name="omega", class_name="volScalarField", location="0") + f"""dimensions      [0 0 -1 0 0 0 0];
internalField   uniform {specific_dissipation_rate:.17g};
boundaryField
{{
    {inlet}
    {{
{omega_inlet}
    }}
    {outlet}
    {{
        type zeroGradient;
    }}
{wall_omega}
}}
"""
    nut = _header(object_name="nut", class_name="volScalarField", location="0") + f"""dimensions      [0 2 -1 0 0 0 0];
internalField   uniform 0;
boundaryField
{{
    {inlet}
    {{
        type calculated;
        value uniform 0;
    }}
    {outlet}
    {{
        type calculated;
        value uniform 0;
    }}
{wall_nut}
}}
"""
    return {"0/k": k, "0/omega": omega, "0/nut": nut}


def _map_fields_dict() -> str:
    """Map internal precursor fields while preserving target patch semantics."""

    return _header(
        object_name="mapFieldsDict",
        class_name="dictionary",
        location="system",
    ) + """patchMap       ();
cuttingPatches ();
"""


def _wall_normal_expansion_ratio(
    cross_cells: int,
    nominal_wall_cell_fraction: float | None,
) -> float:
    """Return blockMesh end/start grading from a wall-adjacent cell fraction.

    The ratio is oriented from wall to core.  OpenFOAM defines simple grading
    as end-cell width divided by start-cell width.  We solve the geometric
    series instead of treating the block ratio as a per-cell growth factor.
    """

    cells = integer_at_least(cross_cells, name="cross_cells", minimum=2)
    if nominal_wall_cell_fraction is None:
        return 1.0
    fraction = positive_float(
        nominal_wall_cell_fraction,
        name="nominal_wall_cell_fraction",
    )
    if fraction >= 1.0:
        raise ValueError("nominal_wall_cell_fraction must be below one.")
    uniform_fraction = 1.0 / cells
    if math.isclose(fraction, uniform_fraction, rel_tol=1.0e-14, abs_tol=0.0):
        return 1.0

    target_sum = 1.0 / fraction

    def geometric_sum(ratio: float) -> float:
        total = 1.0
        term = 1.0
        for _ in range(1, cells):
            term *= ratio
            total += term
            if not math.isfinite(total):
                return math.inf
        return total

    lower = 0.0
    upper = 1.0
    if target_sum > cells:
        while geometric_sum(upper) < target_sum:
            upper *= 2.0
    for _ in range(160):
        midpoint = (lower + upper) / 2.0
        if geometric_sum(midpoint) < target_sum:
            lower = midpoint
        else:
            upper = midpoint
    per_cell_ratio = (lower + upper) / 2.0
    return per_cell_ratio ** (cells - 1)


def _nominal_wall_cell_height(
    *,
    radius: float,
    cross_cells: int,
    nominal_wall_cell_fraction: float | None,
) -> float:
    """Return the design height on an O-grid corner-to-core radial edge."""

    fraction = (
        1.0 / cross_cells
        if nominal_wall_cell_fraction is None
        else nominal_wall_cell_fraction
    )
    outer_edge_length = radius * (1.0 - math.sqrt(2.0) / 3.0)
    return outer_edge_length * fraction


def _block_mesh_dict(
    *,
    length: float,
    radius: float,
    cross_cells: int,
    axial_cells: int,
    inlet: str,
    outlet: str,
    walls: tuple[str, ...],
    cyclic_end_planes: bool = False,
    nominal_wall_cell_fraction: float | None = None,
) -> str:
    inner = radius / 3.0
    wall_to_core = _wall_normal_expansion_ratio(
        cross_cells,
        nominal_wall_cell_fraction,
    )
    core_to_wall = 1.0 / wall_to_core
    encoded_fraction = (
        "auto"
        if nominal_wall_cell_fraction is None
        else f"{nominal_wall_cell_fraction:.17g}"
    )
    wall_name = walls[0]
    if len(walls) != 1:
        raise UnsupportedCaseError("The initial OpenFOAM pipe mesh requires exactly one wall boundary.")
    inlet_type = "cyclic" if cyclic_end_planes else "patch"
    outlet_type = "cyclic" if cyclic_end_planes else "patch"
    inlet_neighbour = f"\n        neighbourPatch {outlet};" if cyclic_end_planes else ""
    outlet_neighbour = f"\n        neighbourPatch {inlet};" if cyclic_end_planes else ""
    return _header(object_name="blockMeshDict", class_name="dictionary", location="system") + f"""// agentcfdNominalWallCellFraction {encoded_fraction}
// agentcfdWallToCoreExpansionRatio {wall_to_core:.17g}
convertToMeters 1;

geometry
{{
    pipeCylinder
    {{
        type cylinder;
        point1 (0 0 0);
        point2 (0 0 {length:.17g});
        radius {radius:.17g};
    }}
}}

vertices
(
    ({-inner:.17g} {-inner:.17g} 0)
    ({inner:.17g} {-inner:.17g} 0)
    ({-inner:.17g} {inner:.17g} 0)
    ({inner:.17g} {inner:.17g} 0)
    project ({-radius:.17g} {-radius:.17g} 0) (pipeCylinder)
    project ({radius:.17g} {-radius:.17g} 0) (pipeCylinder)
    project ({-radius:.17g} {radius:.17g} 0) (pipeCylinder)
    project ({radius:.17g} {radius:.17g} 0) (pipeCylinder)
    ({-inner:.17g} {-inner:.17g} {length:.17g})
    ({inner:.17g} {-inner:.17g} {length:.17g})
    ({-inner:.17g} {inner:.17g} {length:.17g})
    ({inner:.17g} {inner:.17g} {length:.17g})
    project ({-radius:.17g} {-radius:.17g} {length:.17g}) (pipeCylinder)
    project ({radius:.17g} {-radius:.17g} {length:.17g}) (pipeCylinder)
    project ({-radius:.17g} {radius:.17g} {length:.17g}) (pipeCylinder)
    project ({radius:.17g} {radius:.17g} {length:.17g}) (pipeCylinder)
);

blocks
(
    hex (4 5 1 0 12 13 9 8) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 {wall_to_core:.17g} 1)
    hex (4 0 2 6 12 8 10 14) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading ({wall_to_core:.17g} 1 1)
    hex (1 5 7 3 9 13 15 11) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading ({core_to_wall:.17g} 1 1)
    hex (2 3 7 6 10 11 15 14) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 {core_to_wall:.17g} 1)
    hex (0 1 3 2 8 9 11 10) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
);

edges
(
    arc 4 5 (0 {-radius:.17g} 0)
    arc 7 5 ({radius:.17g} 0 0)
    arc 6 7 (0 {radius:.17g} 0)
    arc 4 6 ({-radius:.17g} 0 0)
    arc 12 13 (0 {-radius:.17g} {length:.17g})
    arc 13 15 ({radius:.17g} 0 {length:.17g})
    arc 12 14 ({-radius:.17g} 0 {length:.17g})
    arc 14 15 (0 {radius:.17g} {length:.17g})
);

boundary
(
    {inlet}
    {{
        type {inlet_type};{inlet_neighbour}
        faces
        (
            (4 0 1 5)
            (4 6 2 0)
            (1 3 7 5)
            (2 6 7 3)
            (0 2 3 1)
        );
    }}
    {outlet}
    {{
        type {outlet_type};{outlet_neighbour}
        faces
        (
            (12 13 9 8)
            (12 8 10 14)
            (9 13 15 11)
            (10 11 15 14)
            (8 9 11 10)
        );
    }}
    {wall_name}
    {{
        type wall;
        faces
        (
            (4 5 13 12)
            (4 12 14 6)
            (5 7 15 13)
            (6 14 15 7)
        );
    }}
);
"""


def _control_dict(
    maximum_iterations: int,
    *,
    inlet: str,
    outlet: str,
    turbulent: bool = False,
) -> str:
    y_plus = """
    agentcfd_y_plus
    {
        type yPlus;
        libs (fieldFunctionObjects);
        executeControl timeStep;
        executeInterval 1;
        writeControl timeStep;
        writeInterval 1;
        writeToFile true;
        writeFields false;
        log true;
    }
""" if turbulent else ""
    return _header(object_name="controlDict", class_name="dictionary", location="system") + f"""application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {maximum_iterations};
deltaT          1;
writeControl    timeStep;
writeInterval   {maximum_iterations};
purgeWrite      1;
writeFormat     ascii;
writePrecision  10;
runTimeModifiable true;

functions
{{
    agentcfd_inlet_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {inlet};
        operation sum;
        fields (phi);
    }}
    agentcfd_outlet_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {outlet};
        operation sum;
        fields (phi);
    }}
    agentcfd_inlet_pressure
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {inlet};
        operation areaAverage;
        fields (p);
    }}
    agentcfd_outlet_pressure
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        writeControl timeStep;
        writeInterval 1;
        writeFields false;
        regionType patch;
        name {outlet};
        operation areaAverage;
        fields (p);
    }}
{y_plus}
}}
"""


def _fv_schemes(*, turbulent: bool = False) -> str:
    turbulence_divergence = """
    div(phi,k) bounded Gauss upwind;
    div(phi,omega) bounded Gauss upwind;
""" if turbulent else ""
    return _header(object_name="fvSchemes", class_name="dictionary", location="system") + f"""ddtSchemes
{{
    default steadyState;
}}
gradSchemes
{{
    default Gauss linear;
}}
divSchemes
{{
    default none;
    div(phi,U) bounded Gauss linearUpwind grad(U);
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
{turbulence_divergence}
}}
laplacianSchemes
{{
    default Gauss linear corrected;
}}
interpolationSchemes
{{
    default linear;
}}
snGradSchemes
{{
    default corrected;
}}
wallDist
{{
    method meshWave;
}}
"""


def _fv_solution(
    relative_tolerance: float,
    *,
    turbulent: bool = False,
    strict_velocity_solve: bool = False,
) -> str:
    pressure_relative_tolerance = 0.1 if turbulent else 0.01
    velocity_relative_tolerance = 0.0 if strict_velocity_solve else 0.1
    turbulence_solvers = f"""
    \"(k|omega)\"
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance {relative_tolerance:.17g};
        relTol 0.1;
    }}
""" if turbulent else ""
    turbulence_residuals = f"""
        k {relative_tolerance:.17g};
        omega {relative_tolerance:.17g};
""" if turbulent else ""
    turbulence_relaxation = """
        k 0.7;
        omega 0.7;
""" if turbulent else ""
    return _header(object_name="fvSolution", class_name="dictionary", location="system") + f"""solvers
{{
    p
    {{
        solver GAMG;
        tolerance {relative_tolerance:.17g};
        relTol {pressure_relative_tolerance:.17g};
        smoother GaussSeidel;
    }}
    U
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance {relative_tolerance:.17g};
        relTol {velocity_relative_tolerance:.17g};
    }}
{turbulence_solvers}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors 0;
    consistent yes;
    residualControl
    {{
        p {relative_tolerance:.17g};
        U {relative_tolerance:.17g};
{turbulence_residuals}
    }}
}}

relaxationFactors
{{
    fields
    {{
        p 0.3;
    }}
    equations
    {{
        U 0.7;
{turbulence_relaxation}
    }}
}}
"""
