"""Geometry assets used by the public workflow."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class CircularPipe:
    length: float
    diameter: float
    roughness: float = 0.0
    name: str = "pipe"

    def __post_init__(self) -> None:
        if self.length <= 0.0:
            raise ValueError("Pipe length must be positive.")
        if self.diameter <= 0.0:
            raise ValueError("Pipe diameter must be positive.")
        if self.roughness < 0.0:
            raise ValueError("Pipe roughness cannot be negative.")

    @property
    def area(self) -> float:
        return math.pi * self.diameter**2 / 4.0

    def to_dict(self) -> dict[str, object]:
        return {"type": "circular-pipe", **asdict(self)}


def circular_pipe(*, length: float, diameter: float, roughness: float = 0.0, name: str = "pipe") -> CircularPipe:
    """Create a straight circular pipe using SI lengths."""

    return CircularPipe(length=length, diameter=diameter, roughness=roughness, name=name)
