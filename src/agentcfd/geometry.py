"""Geometry assets used by the public workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import math

from ._validation import nonnegative_float, positive_float


@dataclass(frozen=True, slots=True)
class CircularPipe:
    length: float
    diameter: float
    roughness: float = 0.0
    name: str = "pipe"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "length",
            positive_float(self.length, name="Pipe length"),
        )
        object.__setattr__(
            self,
            "diameter",
            positive_float(self.diameter, name="Pipe diameter"),
        )
        object.__setattr__(
            self,
            "roughness",
            nonnegative_float(self.roughness, name="Pipe roughness"),
        )
        if not str(self.name).strip():
            raise ValueError("Pipe name cannot be empty.")

    @property
    def area(self) -> float:
        return math.pi * self.diameter**2 / 4.0

    def to_dict(self) -> dict[str, object]:
        return {"type": "circular-pipe", **asdict(self)}


def circular_pipe(*, length: float, diameter: float, roughness: float = 0.0, name: str = "pipe") -> CircularPipe:
    """Create a straight circular pipe using SI lengths."""

    return CircularPipe(length=length, diameter=diameter, roughness=roughness, name=name)
