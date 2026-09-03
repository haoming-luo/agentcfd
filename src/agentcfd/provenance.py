"""Deterministic identities for scientific inputs and result artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Mapping

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised by installed-wheel smoke tests
    np = None


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_fingerprint(value: object) -> str:
    payload = json.dumps(
        _scientific_record(value, path="value", missing=[]),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def scientific_input_manifest(value: object) -> dict[str, object]:
    """Return a transparent, content-addressed record of solver inputs."""

    missing: list[dict[str, str]] = []
    record = _scientific_record(value, path="scientific_inputs", missing=missing)
    return {
        "schema": "agentcae.scientific-input-manifest",
        "schema_version": "0.1.0",
        "complete": not missing,
        "missing": missing,
        "record": record,
        "fingerprint": content_fingerprint(record),
    }


def _scientific_record(value: object, *, path: str, missing: list[dict[str, str]]) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) or (np is not None and isinstance(value, np.floating)):
        selected = float(value)
        if not math.isfinite(selected):
            raise ValueError(f"{path} contains a non-finite value.")
        return selected
    if np is not None and isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Path):
        if not value.is_file():
            missing.append({"path": path, "reason": "file_missing"})
            return {"kind": "file", "name": value.name, "status": "missing"}
        return {
            "kind": "file",
            "name": value.name,
            "status": "hashed",
            "size_bytes": value.stat().st_size,
            "sha256": file_sha256(value),
        }
    if np is not None and isinstance(value, np.ndarray):
        contiguous = np.ascontiguousarray(value)
        if not np.all(np.isfinite(contiguous)):
            raise ValueError(f"{path} contains non-finite array values.")
        return {
            "kind": "array",
            "dtype": contiguous.dtype.str,
            "shape": list(contiguous.shape),
            "sha256": hashlib.sha256(contiguous.tobytes(order="C")).hexdigest(),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _scientific_record(value[key], path=f"{path}.{key}", missing=missing)
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [
            _scientific_record(item, path=f"{path}[{index}]", missing=missing)
            for index, item in enumerate(value)
        ]
    for method_name in ("to_dict", "as_dict", "summary"):
        method = getattr(value, method_name, None)
        if callable(method):
            return {
                "kind": "declared_scientific_object",
                "python_type": f"{type(value).__module__}.{type(value).__qualname__}",
                "contract": method_name,
                "value": _scientific_record(method(), path=f"{path}.{method_name}", missing=missing),
            }
    missing.append({"path": path, "reason": "no_scientific_identity_contract"})
    return {
        "kind": "opaque",
        "python_type": f"{type(value).__module__}.{type(value).__qualname__}",
    }


__all__ = ["content_fingerprint", "file_sha256", "scientific_input_manifest"]
