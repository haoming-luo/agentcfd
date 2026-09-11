"""Neutral exchange records for CFD, FEM, experiments, and learned models."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .jsonio import strict_json_object
from .provenance import file_sha256


_CANONICAL_FIELDS = {
    "fluid.velocity",
    "fluid.pressure",
    "fluid.density",
    "fluid.temperature",
    "fluid.wall_shear",
    "thermal.temperature",
    "thermal.heat_flux",
    "solid.displacement",
    "solid.velocity",
    "solid.traction",
}
_DIRECTIONS = {"cfd-to-fem", "fem-to-cfd", "bidirectional", "simulation-to-learning"}
_LOCATIONS = {"point", "cell", "facet", "global"}


@dataclass(frozen=True, slots=True)
class ExchangeField:
    name: str
    direction: str
    location: str
    unit: str
    conservative: bool = False

    def __post_init__(self) -> None:
        if self.name not in _CANONICAL_FIELDS:
            raise ValueError(f"Unknown canonical field {self.name!r}.")
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"Unknown exchange direction {self.direction!r}.")
        if self.location not in _LOCATIONS:
            raise ValueError(f"Unknown field location {self.location!r}.")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("Exchange fields require an explicit unit.")
        if not isinstance(self.conservative, bool):
            raise ValueError("Exchange field conservative must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CouplingManifest:
    interface: str
    source_model_sha256: str
    target: str
    coordinate_frame: str
    mesh_sha256: str
    fields: tuple[ExchangeField, ...]
    time_coordinate: str = "time"
    schema: str = "agentcae.coupling-manifest/0.1"

    def __post_init__(self) -> None:
        for name in ("interface", "target", "coordinate_frame", "time_coordinate"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "Interface, target, coordinate frame, and time coordinate are required."
                )
        if self.schema != "agentcae.coupling-manifest/0.1":
            raise ValueError("Unsupported coupling-manifest schema.")
        if not isinstance(self.fields, tuple) or any(
            not isinstance(field, ExchangeField) for field in self.fields
        ):
            raise ValueError("Coupling fields must be ExchangeField records.")
        if not self.fields:
            raise ValueError("At least one exchange field is required.")
        for digest in (self.source_model_sha256, self.mesh_sha256):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest.lower()
                )
            ):
                raise ValueError("Model and mesh identities must be SHA-256 hex digests.")
        names = tuple(field.name for field in self.fields)
        if len(set(names)) != len(names):
            raise ValueError("Coupling fields must not contain duplicate canonical names.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "interface": self.interface,
            "source_model_sha256": self.source_model_sha256,
            "target": self.target,
            "coordinate_frame": self.coordinate_frame,
            "mesh_sha256": self.mesh_sha256,
            "time_coordinate": self.time_coordinate,
            "fields": [field.to_dict() for field in self.fields],
        }


def fluid_loads_to_solid(
    *,
    interface: str,
    source_model_sha256: str,
    target: str,
    coordinate_frame: str,
    mesh_sha256: str,
    include_temperature: bool = True,
) -> CouplingManifest:
    fields = [
        ExchangeField("fluid.pressure", "cfd-to-fem", "facet", "Pa", conservative=True),
        ExchangeField("solid.traction", "cfd-to-fem", "facet", "Pa", conservative=True),
    ]
    if include_temperature:
        fields.append(ExchangeField("thermal.temperature", "cfd-to-fem", "facet", "K"))
    return CouplingManifest(
        interface=interface,
        source_model_sha256=source_model_sha256,
        target=target,
        coordinate_frame=coordinate_frame,
        mesh_sha256=mesh_sha256,
        fields=tuple(fields),
    )


def verify_scientific_dataset(directory: str | Path) -> dict[str, object]:
    """Verify a scalar campaign dataset without importing a solver or ML stack."""

    root = Path(directory)
    manifest_path = root / "manifest.json"
    samples_path = root / "samples.jsonl"
    checks: list[dict[str, object]] = []

    def add_check(code: str, passed: bool, message: str) -> None:
        checks.append({"code": code, "passed": passed, "message": message})

    manifest: dict[str, object] | None = None
    try:
        manifest = strict_json_object(
            manifest_path.read_text(encoding="utf-8"),
            label=f"AgentCFD scientific dataset {manifest_path}",
        )
        if manifest.get("schema") != "agentcfd.scientific-dataset/0.1":
            raise ValueError("Unsupported AgentCFD scientific-dataset schema.")
        if manifest.get("sample_schema") != "agentcae.scientific-sample/0.1.0":
            raise ValueError("Unsupported dataset sample schema.")
        if not isinstance(manifest.get("inputs"), list) or not manifest["inputs"]:
            raise ValueError("Dataset input schema is missing.")
        if not isinstance(manifest.get("outputs"), list) or not manifest["outputs"]:
            raise ValueError("Dataset output schema is missing.")
        if any(
            not isinstance(record, Mapping)
            or set(record) != {"name", "metadata"}
            or not isinstance(record.get("name"), str)
            or not record["name"]
            or (
                record.get("metadata") is not None
                and not isinstance(record.get("metadata"), Mapping)
            )
            for record in manifest["inputs"]
        ):
            raise ValueError("Dataset input schema is malformed.")
        if any(
            not isinstance(record, Mapping)
            or set(record) != {"name", "shape", "unit", "kind", "description"}
            or not isinstance(record.get("name"), str)
            or not record["name"]
            or record.get("shape") != []
            or (
                record.get("unit") is not None
                and not isinstance(record.get("unit"), str)
            )
            or not isinstance(record.get("kind"), str)
            or not isinstance(record.get("description"), str)
            for record in manifest["outputs"]
        ):
            raise ValueError("Dataset output schema is malformed.")
        input_names = [record["name"] for record in manifest["inputs"]]
        output_names = [record["name"] for record in manifest["outputs"]]
        if len(input_names) != len(set(input_names)) or len(output_names) != len(
            set(output_names)
        ):
            raise ValueError("Dataset input/output schemas contain duplicate names.")
        if not isinstance(manifest.get("runs"), list):
            raise ValueError("Dataset run index is malformed.")
        if manifest.get("sample_count") != len(manifest["runs"]):
            raise ValueError("Dataset sample count disagrees with its run index.")
        excluded = manifest.get("excluded")
        if not isinstance(excluded, list) or manifest.get("excluded_count") != len(
            excluded
        ):
            raise ValueError("Dataset exclusion count is inconsistent.")
    except (OSError, KeyError, TypeError, ValueError) as error:
        add_check("MANIFEST_INTEGRITY", False, str(error))
    else:
        add_check(
            "MANIFEST_INTEGRITY",
            True,
            "The dataset manifest, schemas, counts, and run index are coherent.",
        )

    samples_bytes = 0
    parsed_samples = 0
    if manifest is None or checks[-1]["passed"] is False:
        add_check(
            "SAMPLE_PAYLOAD_INTEGRITY",
            False,
            "Sample verification requires a valid dataset manifest.",
        )
    else:
        try:
            artifact = manifest.get("samples")
            if not isinstance(artifact, Mapping):
                raise ValueError("Dataset sample artifact record is missing.")
            if artifact.get("path") != "samples.jsonl":
                raise ValueError("Dataset sample path must be samples.jsonl.")
            samples_bytes = samples_path.stat().st_size
            if artifact.get("bytes") != samples_bytes:
                raise ValueError("Dataset sample byte count changed.")
            if artifact.get("sha256") != file_sha256(samples_path):
                raise ValueError("Dataset sample SHA-256 changed.")
            lines = samples_path.read_text(encoding="utf-8").splitlines()
            if len(lines) != manifest["sample_count"]:
                raise ValueError("JSONL line count disagrees with sample_count.")
            input_names = [record["name"] for record in manifest["inputs"]]
            output_names = [record["name"] for record in manifest["outputs"]]
            seen_cases: set[str] = set()
            seen_runs: set[str] = set()
            for index, (line, run) in enumerate(
                zip(lines, manifest["runs"], strict=True), start=1
            ):
                if not isinstance(run, Mapping) or run.get("line") != index:
                    raise ValueError(f"Dataset run index line {index} is malformed.")
                sample = strict_json_object(
                    line,
                    label=f"AgentCAE scientific sample line {index}",
                )
                if (
                    set(sample)
                    != {
                        "schema",
                        "schema_version",
                        "case_id",
                        "source",
                        "inputs",
                        "outputs",
                        "quantity_schema",
                        "trust_level",
                        "accepted",
                        "scientific_inputs",
                        "provenance",
                        "artifacts",
                    }
                    or sample.get("schema") != "agentcae.scientific-sample"
                    or sample.get("schema_version") != "0.1.0"
                    or sample.get("accepted") is not True
                ):
                    raise ValueError(
                        f"Dataset line {index} is not an accepted AgentCAE sample."
                    )
                if sample.get("trust_level") not in {
                    "not_computed",
                    "computed",
                    "converged",
                    "verified",
                    "validated",
                }:
                    raise ValueError(f"Dataset line {index} trust level is invalid.")
                source = sample.get("source")
                if (
                    not isinstance(source, Mapping)
                    or set(source) != {"product", "provider"}
                    or source.get("product") != "agentcfd"
                    or not isinstance(source.get("provider"), str)
                ):
                    raise ValueError(f"Dataset line {index} source is malformed.")
                if any(
                    not isinstance(container, Mapping)
                    for container in (
                        sample.get("inputs"),
                        sample.get("outputs"),
                        sample.get("scientific_inputs"),
                        sample.get("provenance"),
                        sample.get("artifacts"),
                    )
                ):
                    raise ValueError(f"Dataset line {index} mappings are malformed.")
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in (
                        *sample["inputs"].values(),
                        *sample["outputs"].values(),
                    )
                ):
                    raise ValueError(f"Dataset line {index} values are not finite numbers.")
                if set(sample.get("inputs", {})) != set(input_names):
                    raise ValueError(f"Dataset line {index} input schema changed.")
                if set(sample.get("outputs", {})) != set(output_names):
                    raise ValueError(f"Dataset line {index} output schema changed.")
                quantity_schema = [
                    {name: value for name, value in record.items() if name != "value"}
                    for record in sample.get("quantity_schema", [])
                    if isinstance(record, Mapping)
                ]
                if quantity_schema != manifest["outputs"]:
                    raise ValueError(f"Dataset line {index} quantity schema changed.")
                provenance = sample.get("provenance")
                export = (
                    provenance.get("sample_export")
                    if isinstance(provenance, Mapping)
                    else None
                )
                if not isinstance(export, Mapping):
                    raise ValueError(f"Dataset line {index} has no export provenance.")
                sample_input_schema = [
                    {
                        name: value
                        for name, value in record.items()
                        if name != "value"
                    }
                    for record in export.get("input_schema", [])
                    if isinstance(record, Mapping)
                ]
                if sample_input_schema != manifest["inputs"]:
                    raise ValueError(f"Dataset line {index} input metadata changed.")
                if (
                    sample.get("case_id") != run.get("case_id")
                    or export.get("run_id") != run.get("run_id")
                    or export.get("source_result_sha256")
                    != run.get("source_result_sha256")
                ):
                    raise ValueError(f"Dataset line {index} source identity changed.")
                case_id = str(sample["case_id"])
                run_id = str(run["run_id"])
                if case_id in seen_cases or run_id in seen_runs:
                    raise ValueError("Dataset contains duplicate case or run identities.")
                seen_cases.add(case_id)
                seen_runs.add(run_id)
                parsed_samples += 1
        except (OSError, KeyError, TypeError, UnicodeError, ValueError) as error:
            add_check("SAMPLE_PAYLOAD_INTEGRITY", False, str(error))
        else:
            add_check(
                "SAMPLE_PAYLOAD_INTEGRITY",
                True,
                f"All {parsed_samples} content-addressed AgentCAE samples are coherent.",
            )

    verified = all(check["passed"] is True for check in checks)
    return {
        "schema": "agentcfd.scientific-dataset-verification/0.1",
        "directory": str(root),
        "verified": verified,
        "sample_count": parsed_samples,
        "checks": checks,
        "observation_cost": {
            "manifest_json_bytes_read": (
                manifest_path.stat().st_size if manifest_path.is_file() else 0
            ),
            "samples_jsonl_bytes_read": samples_bytes,
            "field_payloads_opened": 0,
        },
    }
