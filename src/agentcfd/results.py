"""Structured, verifiable, and learning-ready simulation results."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .jsonio import strict_json_object


_CLAIM_KINDS = {"runtime", "verification", "validation"}
_FIELD_LOCATIONS = {"point", "cell", "facet", "global"}
_TRUST_ORDER = {
    "not_computed": 0,
    "computed": 1,
    "converged": 2,
    "verified": 3,
    "validated": 4,
}


def _finite(value: float, *, label: str) -> float:
    selected = float(value)
    if not math.isfinite(selected):
        raise ValueError(f"{label} must be finite.")
    return selected


@dataclass(frozen=True, slots=True)
class Quantity:
    """One scalar result with physical meaning and a stable canonical name."""

    value: float
    unit: str | None
    kind: str = "quantity_of_interest"
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _finite(self.value, label="Quantity.value"))
        object.__setattr__(self, "kind", str(self.kind).strip() or "quantity_of_interest")
        if self.unit is not None:
            object.__setattr__(self, "unit", str(self.unit).strip() or None)

    def as_record(self, name: str) -> dict[str, object]:
        return {"name": name, "shape": [], **asdict(self)}


@dataclass(frozen=True, slots=True)
class Check:
    """One runtime, verification, or validation claim."""

    name: str
    passed: bool
    value: float | str | None = None
    limit: float | str | None = None
    message: str = ""
    kind: str = "verification"
    observable: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("Check.name must not be empty.")
        if self.kind not in _CLAIM_KINDS:
            raise ValueError(f"Unknown check kind {self.kind!r}.")
        if isinstance(self.value, float):
            _finite(self.value, label=f"Check {name!r} value")
        if isinstance(self.limit, float):
            _finite(self.limit, label=f"Check {name!r} limit")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))

    @property
    def status(self) -> str:
        return "passed" if self.passed else "failed"

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "status": self.status}


@dataclass(frozen=True, slots=True)
class History:
    """A scalar history on a declared monotonic coordinate."""

    abscissa: tuple[float, ...]
    values: tuple[float, ...]
    unit: str | None = None
    abscissa_name: str = "time"
    abscissa_unit: str | None = "s"
    description: str = ""

    def __post_init__(self) -> None:
        axis = tuple(_finite(value, label="History.abscissa") for value in self.abscissa)
        values = tuple(_finite(value, label="History.values") for value in self.values)
        if not axis or len(axis) != len(values):
            raise ValueError("History abscissa and values must be non-empty and equal in length.")
        if any(right <= left for left, right in zip(axis, axis[1:])):
            raise ValueError("History abscissa must be strictly increasing.")
        object.__setattr__(self, "abscissa", axis)
        object.__setattr__(self, "values", values)

    def as_record(self, name: str, *, include_values: bool = True) -> dict[str, object]:
        record: dict[str, object] = {
            "name": name,
            "sample_count": len(self.values),
            "unit": self.unit,
            "abscissa_name": self.abscissa_name,
            "abscissa_unit": self.abscissa_unit,
            "description": self.description,
        }
        if include_values:
            record.update({"abscissa": list(self.abscissa), "values": list(self.values)})
        return record


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """Portable metadata for a field stored in an external artifact."""

    unit: str | None
    location: str
    artifact: str
    components: tuple[str, ...] = ()
    representation: str = "provider-native"
    mesh_sha256: str | None = None
    description: str = ""
    processing: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.location not in _FIELD_LOCATIONS:
            raise ValueError(f"Unknown field location {self.location!r}.")
        if not str(self.artifact).strip():
            raise ValueError("FieldRecord.artifact must not be empty.")
        if self.mesh_sha256 is not None and not _is_sha256(self.mesh_sha256):
            raise ValueError("FieldRecord.mesh_sha256 must be a SHA-256 hex digest.")
        object.__setattr__(self, "components", tuple(str(item) for item in self.components))
        object.__setattr__(self, "processing", dict(self.processing))

    def as_record(self, name: str) -> dict[str, object]:
        return {"name": name, **asdict(self)}


@dataclass(frozen=True, slots=True)
class Artifact:
    """A content-addressed file that supports or contains a result."""

    path: str
    role: str = "result"
    media_type: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        if not str(self.path).strip():
            raise ValueError("Artifact.path must not be empty.")
        if self.sha256 is not None and not _is_sha256(self.sha256):
            raise ValueError("Artifact.sha256 must be a SHA-256 hex digest.")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("Artifact.size_bytes must not be negative.")

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        role: str = "result",
        media_type: str | None = None,
    ) -> "Artifact":
        selected = Path(path)
        if not selected.is_file():
            raise FileNotFoundError(selected)
        from .provenance import file_sha256

        return cls(
            path=str(selected),
            role=role,
            media_type=media_type,
            sha256=file_sha256(selected),
            size_bytes=selected.stat().st_size,
        )

    def as_dict(self, *, base: str | Path | None = None) -> dict[str, object]:
        record = asdict(self)
        if base is not None:
            selected = Path(self.path)
            resolved = selected.resolve() if selected.is_absolute() else (Path(base) / selected).resolve()
            try:
                record["path"] = str(resolved.relative_to(Path(base).resolve()))
            except ValueError:
                record["path"] = self.path
        return record


@dataclass(slots=True)
class SimulationResult:
    """A run record whose execution, trust, and scientific identity stay separate."""

    status: str
    converged: bool
    provider: str
    quantities: dict[str, Quantity]
    checks: tuple[Check, ...]
    arrays: dict[str, list[float]] = field(default_factory=dict)
    fields: dict[str, FieldRecord] = field(default_factory=dict)
    histories: dict[str, History] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    scientific_inputs: dict[str, object] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    messages: tuple[str, ...] = ()
    name: str = "result"
    schema: str = "agentcfd.simulation-result"
    schema_version: str = "0.1.0"

    def __post_init__(self) -> None:
        self.status = str(self.status).strip()
        self.provider = str(self.provider).strip()
        if not self.status or not self.provider:
            raise ValueError("Result status and provider must not be empty.")
        self.quantities = dict(self.quantities)
        self.checks = tuple(self.checks)
        self.arrays = {
            str(name): [_finite(item, label=f"Array {name!r}") for item in values]
            for name, values in self.arrays.items()
        }
        self.fields = dict(self.fields)
        self.histories = dict(self.histories)
        self.artifacts = dict(self.artifacts)
        self.scientific_inputs = dict(self.scientific_inputs)
        self.provenance = dict(self.provenance)
        self.messages = tuple(str(message) for message in self.messages)

    @property
    def accepted(self) -> bool:
        return (
            self.status == "completed"
            and self.converged
            and bool(self.checks)
            and all(check.passed for check in self.checks)
        )

    @property
    def trust_level(self) -> str:
        if self.status != "completed":
            return "not_computed"
        if not self.converged:
            return "computed"
        if not self.checks or any(not check.passed for check in self.checks):
            return "converged"
        kinds = {check.kind for check in self.checks}
        if "validation" in kinds:
            return "validated"
        if "verification" in kinds:
            return "verified"
        return "converged"

    def require_accepted(self) -> "SimulationResult":
        if not self.accepted:
            failed = ", ".join(check.name for check in self.checks if not check.passed)
            raise RuntimeError(f"Simulation result is not accepted. Failed checks: {failed or 'execution'}")
        return self

    def require_trust(self, minimum: str) -> "SimulationResult":
        if minimum not in _TRUST_ORDER:
            raise ValueError(f"Unknown trust level {minimum!r}.")
        if _TRUST_ORDER[self.trust_level] < _TRUST_ORDER[minimum]:
            raise RuntimeError(
                f"Simulation result trust level is {self.trust_level!r}; {minimum!r} is required."
            )
        return self

    def scientific_input_manifest(self) -> dict[str, object]:
        from .provenance import scientific_input_manifest

        return scientific_input_manifest(self.scientific_inputs)

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "converged": self.converged,
            "accepted": self.accepted,
            "trust_level": self.trust_level,
            "provider": self.provider,
            "quantities": {name: asdict(value) for name, value in self.quantities.items()},
            "fields": {name: value.as_record(name) for name, value in self.fields.items()},
            "histories": {name: value.as_record(name, include_values=False) for name, value in self.histories.items()},
            "artifacts": {name: value.path for name, value in self.artifacts.items()},
            "provenance": dict(self.provenance),
        }

    def manifest(
        self,
        *,
        include_histories: bool = False,
        artifact_base: str | Path | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            **self.summary(),
            "quantity_records": [value.as_record(name) for name, value in self.quantities.items()],
            "field_records": [value.as_record(name) for name, value in self.fields.items()],
            "history_records": [
                value.as_record(name, include_values=include_histories)
                for name, value in self.histories.items()
            ],
            "artifact_records": {
                name: value.as_dict(base=artifact_base) for name, value in self.artifacts.items()
            },
            "checks": [check.as_dict() for check in self.checks],
            "arrays": self.arrays,
            "scientific_inputs": self.scientific_input_manifest(),
            "messages": list(self.messages),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.manifest(include_histories=True)

    def to_exchange(self, *, include_histories: bool = False) -> dict[str, Any]:
        """Return the solver-neutral AgentCAE result record."""

        from ._version import __version__

        record = self.manifest(include_histories=include_histories)
        record.update(
            {
                "schema": "agentcae.simulation-result",
                "schema_version": "0.1.0",
                "source": {
                    "product": "agentcfd",
                    "version": __version__,
                    "provider": self.provider,
                },
            }
        )
        return record

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        record = self.manifest(include_histories=True, artifact_base=target.parent)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    def to_sample(
        self,
        *,
        inputs: Mapping[str, float] | None = None,
        outputs: Sequence[str] | None = None,
        case_id: str | None = None,
        parameters: Mapping[str, float] | None = None,
        responses: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create an AgentFEM-compatible, framework-neutral scientific sample.

        ``parameters`` and ``responses`` remain accepted aliases for the initial
        pre-alpha API.  The emitted ``inputs`` and numeric ``outputs`` map
        directly onto AgentFEM's ``datasets.Sample`` contract.
        """

        self.require_accepted()
        selected_inputs = inputs if inputs is not None else parameters
        selected_outputs = outputs if outputs is not None else responses
        if selected_inputs is None or selected_outputs is None:
            raise TypeError("to_sample requires inputs/outputs (or parameters/responses).")
        missing = [name for name in selected_outputs if name not in self.quantities]
        if missing:
            raise KeyError(f"Unknown output quantities: {', '.join(missing)}")
        model_identity = str(self.provenance.get("model_sha256", ""))
        selected_case_id = case_id or (f"agentcfd-{model_identity[:16]}" if model_identity else self.name)
        artifact_paths = {name: artifact.path for name, artifact in self.artifacts.items()}
        return {
            "schema": "agentcae.scientific-sample",
            "schema_version": "0.1.0",
            "case_id": selected_case_id,
            "source": {"product": "agentcfd", "provider": self.provider},
            "inputs": {name: _finite(value, label=f"Input {name!r}") for name, value in selected_inputs.items()},
            "outputs": {name: self.quantities[name].value for name in selected_outputs},
            "quantity_schema": [self.quantities[name].as_record(name) for name in selected_outputs],
            "trust_level": self.trust_level,
            "accepted": self.accepted,
            "scientific_inputs": self.scientific_input_manifest(),
            "provenance": dict(self.provenance),
            "artifacts": artifact_paths,
        }


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def read_result_record(
    path: str | Path,
    *,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    """Read a native result and verify its derived trust state and artifacts."""

    selected = Path(path)
    record = strict_json_object(
        selected.read_text(encoding="utf-8"),
        label=f"AgentCFD result {selected}",
    )
    if (
        record.get("schema") != "agentcfd.simulation-result"
        or record.get("schema_version") != "0.1.0"
    ):
        raise ValueError("Unsupported AgentCFD result schema or version.")
    checks = record.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) or not isinstance(check.get("passed"), bool)
        for check in checks
    ):
        raise ValueError("AgentCFD result checks are malformed.")
    status = record.get("status")
    converged = record.get("converged")
    if not isinstance(status, str) or not isinstance(converged, bool):
        raise ValueError("AgentCFD result execution state is malformed.")
    expected_accepted = (
        status == "completed"
        and converged
        and bool(checks)
        and all(check["passed"] for check in checks)
    )
    if record.get("accepted") is not expected_accepted:
        raise ValueError("AgentCFD result accepted flag is inconsistent with its checks.")
    if status != "completed":
        expected_trust = "not_computed"
    elif not converged:
        expected_trust = "computed"
    elif not checks or any(not check["passed"] for check in checks):
        expected_trust = "converged"
    else:
        kinds = {check.get("kind") for check in checks}
        expected_trust = (
            "validated"
            if "validation" in kinds
            else "verified"
            if "verification" in kinds
            else "converged"
        )
    if record.get("trust_level") != expected_trust:
        raise ValueError("AgentCFD result trust level is inconsistent with its checks.")

    artifacts = record.get("artifact_records")
    if not isinstance(artifacts, dict):
        raise ValueError("AgentCFD result artifact records are malformed.")
    if verify_artifacts:
        from .provenance import file_sha256

        for name, artifact in artifacts.items():
            if not isinstance(name, str) or not isinstance(artifact, dict):
                raise ValueError("AgentCFD result artifact record is malformed.")
            artifact_path = artifact.get("path")
            digest = artifact.get("sha256")
            size = artifact.get("size_bytes")
            if (
                not isinstance(artifact_path, str)
                or not isinstance(digest, str)
                or not _is_sha256(digest)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ValueError(f"Artifact {name!r} has incomplete identity metadata.")
            candidate = Path(artifact_path)
            if not candidate.is_absolute():
                candidate = selected.parent / candidate
            if not candidate.is_file():
                raise ValueError(f"Artifact {name!r} is missing: {candidate}")
            if candidate.stat().st_size != size or file_sha256(candidate) != digest:
                raise ValueError(f"Artifact {name!r} no longer matches its identity.")
    return record


__all__ = [
    "Artifact",
    "Check",
    "FieldRecord",
    "History",
    "Quantity",
    "SimulationResult",
    "read_result_record",
]
