"""Output requests use canonical scientific field names."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class OutputRequest:
    fields: tuple[str, ...]
    histories: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def standard() -> OutputRequest:
    return OutputRequest(
        fields=("fluid.velocity", "fluid.pressure"),
        histories=("flow.mass_balance", "flow.pressure_drop"),
    )
