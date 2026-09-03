"""Provider contracts isolate scientific intent from numerical backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..model import Step
    from ..results import SimulationResult


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    name: str
    version: str
    license: str
    available: bool
    execution_boundary: str
    capabilities: tuple[str, ...]


class Provider(Protocol):
    def descriptor(self) -> ProviderDescriptor: ...

    def run(self, step: "Step") -> "SimulationResult": ...
