"""Strict JSON parsing for scientific and execution-control records."""

from __future__ import annotations

import json
from typing import Any


def strict_json_object(text: str, *, label: str) -> dict[str, Any]:
    """Parse one standards-compliant object while rejecting ambiguous keys."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains non-finite JSON number {value}.")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for key, value in pairs:
            if key in record:
                raise ValueError(f"{label} contains duplicate key {key!r}.")
            record[key] = value
        return record

    try:
        record = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid {label} JSON.") from error
    if not isinstance(record, dict):
        raise ValueError(f"{label} must contain a JSON object.")
    return record


__all__ = ["strict_json_object"]
