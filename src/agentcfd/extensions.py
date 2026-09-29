"""Descriptor-first discovery for optional AgentCFD extension packages.

Discovery reads installed distribution and entry-point metadata only. Optional
extension code is imported solely by the explicit :func:`load` operation after
compatibility and ambiguity checks pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from importlib import metadata
import re
from typing import Mapping


EXTENSION_API_VERSION = "1"
ENTRY_POINT_GROUPS = {
    "provider": "agentcfd.providers.v1",
    "exporter": "agentcfd.exporters.v1",
    "property": "agentcfd.properties.v1",
    "learning": "agentcfd.learning.v1",
}
_LEGACY_ENTRY_POINT_GROUPS = {
    kind: group.removesuffix(".v1") for kind, group in ENTRY_POINT_GROUPS.items()
}

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class ExtensionDescriptor:
    """One optional extension described without importing its implementation."""

    kind: str
    group: str
    name: str
    object_ref: str
    distribution: str | None
    distribution_version: str | None
    api_version: str | None
    compatible: bool
    compatibility_code: str
    compatibility_message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _distribution_record(
    entry_point: metadata.EntryPoint,
) -> tuple[str | None, str | None]:
    distribution = getattr(entry_point, "dist", None)
    if distribution is None:
        return None, None
    package_metadata = distribution.metadata
    name = package_metadata.get("Name") or getattr(distribution, "name", None)
    version = getattr(distribution, "version", None)
    return (
        None if name is None else str(name),
        None if version is None else str(version),
    )


def _describe(
    entry_point: metadata.EntryPoint,
    *,
    kind: str,
    group: str,
) -> ExtensionDescriptor:
    distribution, distribution_version = _distribution_record(entry_point)
    api_version = EXTENSION_API_VERSION if group == ENTRY_POINT_GROUPS[kind] else None
    name = str(entry_point.name)
    object_ref = str(entry_point.value)
    if not _NAME.fullmatch(name):
        compatible = False
        code = "invalid-name"
        message = "Extension names must use lowercase letters, numbers, underscores, or hyphens."
    elif api_version is None:
        compatible = False
        code = "unsupported-api-group"
        message = (
            f"Use the versioned entry-point group {ENTRY_POINT_GROUPS[kind]!r}; "
            f"the unversioned group {group!r} cannot declare compatibility."
        )
    else:
        compatible = True
        code = "compatible"
        message = "Descriptor is compatible; implementation remains unloaded."
    return ExtensionDescriptor(
        kind=kind,
        group=group,
        name=name,
        object_ref=object_ref,
        distribution=distribution,
        distribution_version=distribution_version,
        api_version=api_version,
        compatible=compatible,
        compatibility_code=code,
        compatibility_message=message,
    )


def discover(*, kind: str | None = None) -> tuple[ExtensionDescriptor, ...]:
    """Discover extension descriptors without importing extension code."""

    if kind is None:
        selected_groups = tuple(
            (selected_kind, group)
            for selected_kind in ENTRY_POINT_GROUPS
            for group in (
                ENTRY_POINT_GROUPS[selected_kind],
                _LEGACY_ENTRY_POINT_GROUPS[selected_kind],
            )
        )
    else:
        selected = str(kind).strip().lower()
        if selected not in ENTRY_POINT_GROUPS:
            raise ValueError(
                f"Unknown extension kind {kind!r}; expected one of {tuple(ENTRY_POINT_GROUPS)!r}."
            )
        selected_groups = tuple(
            (selected, group)
            for group in (
                ENTRY_POINT_GROUPS[selected],
                _LEGACY_ENTRY_POINT_GROUPS[selected],
            )
        )

    records: list[ExtensionDescriptor] = []
    for selected_kind, group in selected_groups:
        for entry_point in metadata.entry_points(group=group):
            records.append(_describe(entry_point, kind=selected_kind, group=group))

    identities: dict[tuple[str, str], int] = {}
    for record in records:
        identity = (record.group, record.name)
        identities[identity] = identities.get(identity, 0) + 1
    records = [
        replace(
            record,
            compatible=False,
            compatibility_code="ambiguous-name",
            compatibility_message=(
                "Multiple installed distributions publish this group/name; "
                "remove the ambiguity before loading."
            ),
        )
        if identities[(record.group, record.name)] > 1
        else record
        for record in records
    ]
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item.kind,
                item.name,
                item.distribution or "",
                item.object_ref,
            ),
        )
    )


def load(kind: str, name: str):
    """Explicitly import one compatible, unambiguous extension implementation."""

    selected_name = str(name).strip()
    matches = [item for item in discover(kind=kind) if item.name == selected_name]
    if not matches:
        raise ValueError(f"Unknown {kind!r} extension {name!r}.")
    compatible_matches = [item for item in matches if item.compatible]
    descriptor = compatible_matches[0] if len(compatible_matches) == 1 else matches[0]
    if not descriptor.compatible:
        raise RuntimeError(
            f"Extension {name!r} cannot be loaded: "
            f"[{descriptor.compatibility_code}] {descriptor.compatibility_message}"
        )
    candidates = [
        item
        for item in metadata.entry_points(group=descriptor.group)
        if item.name == descriptor.name and item.value == descriptor.object_ref
    ]
    if len(candidates) != 1:
        raise RuntimeError("Extension metadata changed during explicit loading.")
    implementation = candidates[0].load()
    describe = getattr(implementation, "descriptor", None)
    if not callable(describe):
        raise RuntimeError(
            "Loaded extension must expose a callable descriptor() contract."
        )
    record = describe()
    if not isinstance(record, Mapping):
        raise RuntimeError("Loaded extension descriptor() must return a mapping.")
    expected = {
        "schema": "agentcfd.extension/1",
        "kind": descriptor.kind,
        "name": descriptor.name,
    }
    mismatched = {
        key: {"expected": value, "actual": record.get(key)}
        for key, value in expected.items()
        if record.get(key) != value
    }
    capabilities = record.get("capabilities")
    if (
        mismatched
        or not isinstance(capabilities, (list, tuple))
        or any(not isinstance(item, str) or not item.strip() for item in capabilities)
    ):
        raise RuntimeError(
            "Loaded extension descriptor is incompatible: "
            f"mismatched={mismatched!r}; capabilities must be non-empty strings."
        )
    return implementation


def as_dict(*, kind: str | None = None) -> dict[str, object]:
    records = discover(kind=kind)
    return {
        "schema": "agentcfd.extension-catalog/0.1",
        "extension_api_version": EXTENSION_API_VERSION,
        "entry_point_groups": dict(ENTRY_POINT_GROUPS),
        "extensions": [record.to_dict() for record in records],
        "summary": {
            "discovered": len(records),
            "compatible": sum(record.compatible for record in records),
            "incompatible": sum(not record.compatible for record in records),
        },
    }


__all__ = [
    "ENTRY_POINT_GROUPS",
    "EXTENSION_API_VERSION",
    "ExtensionDescriptor",
    "as_dict",
    "discover",
    "load",
]
