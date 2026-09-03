"""Output requests use canonical scientific field names."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class OutputRequest:
    fields: tuple[str, ...]
    histories: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("fields", "histories"):
            selected = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item.strip() for item in selected):
                raise ValueError(f"Output {name} must contain non-empty canonical names.")
            if len(set(selected)) != len(selected):
                raise ValueError(f"Output {name} must not contain duplicates.")
            object.__setattr__(self, name, selected)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def standard() -> OutputRequest:
    return OutputRequest(
        fields=("fluid.velocity", "fluid.pressure"),
        histories=("flow.mass_balance", "flow.pressure_drop"),
    )
