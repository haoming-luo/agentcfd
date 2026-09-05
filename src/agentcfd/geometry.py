"""Geometry assets used by the public workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import math

from ._validation import nonnegative_float, positive_float
from .regions import Region, surface, volume


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
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Pipe name must be a non-empty string.")

    @property
    def area(self) -> float:
        return math.pi * self.diameter**2 / 4.0

    def to_dict(self) -> dict[str, object]:
        return {"type": "circular-pipe", **asdict(self)}

    @property
    def hydraulic_diameter(self) -> float:
        return self.diameter

    @property
    def regions(self) -> tuple[Region, ...]:
        return (
            volume("fluid", role="fluid"),
            surface("inlet", role="inlet"),
            surface("outlet", role="outlet"),
            surface("wall", role="wall"),
        )

    @property
    def surface_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.regions if item.kind == "surface")


@dataclass(frozen=True, slots=True)
class WallBaffle:
    """A thin wall feature attached to a rectangular-channel wall."""

    x: float
    height: float
    thickness: float
    attached_to: str = "bottom"
    name: str = "baffle"

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", positive_float(self.x, name="Baffle x"))
        object.__setattr__(
            self,
            "height",
            positive_float(self.height, name="Baffle height"),
        )
        object.__setattr__(
            self,
            "thickness",
            positive_float(self.thickness, name="Baffle thickness"),
        )
        if self.attached_to not in {"bottom", "top"}:
            raise ValueError("Baffle attached_to must be 'bottom' or 'top'.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Baffle name must be a non-empty string.")
        object.__setattr__(self, "name", self.name.strip())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RectangularChannel:
    """A straight internal-flow channel with optional wall-attached baffles."""

    length: float
    height: float
    width: float
    roughness: float = 0.0
    name: str = "channel"
    baffles: tuple[WallBaffle, ...] = ()

    def __post_init__(self) -> None:
        for attribute, label in (
            ("length", "Channel length"),
            ("height", "Channel height"),
            ("width", "Channel width"),
        ):
            object.__setattr__(
                self,
                attribute,
                positive_float(getattr(self, attribute), name=label),
            )
        object.__setattr__(
            self,
            "roughness",
            nonnegative_float(self.roughness, name="Channel roughness"),
        )
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Channel name must be a non-empty string.")
        object.__setattr__(self, "name", self.name.strip())
        selected = tuple(self.baffles)
        if any(not isinstance(item, WallBaffle) for item in selected):
            raise TypeError("Channel baffles must be AgentCFD WallBaffle objects.")
        if len({item.name for item in selected}) != len(selected):
            raise ValueError("Channel baffle names must be unique.")
        ordered = tuple(sorted(selected, key=lambda item: (item.x, item.name)))
        previous_end = 0.0
        for item in ordered:
            if item.x + item.thickness >= self.length:
                raise ValueError(
                    "A baffle must end strictly before the channel outlet."
                )
            if item.height >= self.height:
                raise ValueError("A baffle must leave a positive open channel gap.")
            if item.x < previous_end:
                raise ValueError("Channel baffles must not overlap.")
            previous_end = item.x + item.thickness
        object.__setattr__(self, "baffles", ordered)

    @property
    def area(self) -> float:
        return self.height * self.width

    @property
    def hydraulic_diameter(self) -> float:
        return 2.0 * self.height * self.width / (self.height + self.width)

    @property
    def regions(self) -> tuple[Region, ...]:
        return (
            volume("fluid", role="fluid"),
            surface("inlet", role="inlet"),
            surface("outlet", role="outlet"),
            surface("walls", role="wall"),
            *(surface(item.name, role="wall") for item in self.baffles),
        )

    @property
    def surface_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.regions if item.kind == "surface")

    def with_baffle(
        self,
        *,
        x: float,
        height: float,
        thickness: float,
        attached_to: str = "bottom",
        name: str = "baffle",
    ) -> "RectangularChannel":
        """Return a new channel containing one additional named wall baffle."""

        return replace(
            self,
            baffles=(
                *self.baffles,
                WallBaffle(
                    x=x,
                    height=height,
                    thickness=thickness,
                    attached_to=attached_to,
                    name=name,
                ),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "rectangular-channel",
            "name": self.name,
            "length": self.length,
            "height": self.height,
            "width": self.width,
            "roughness": self.roughness,
            "baffles": [item.to_dict() for item in self.baffles],
            "regions": [item.to_dict() for item in self.regions],
        }


def circular_pipe(
    *, length: float, diameter: float, roughness: float = 0.0, name: str = "pipe"
) -> CircularPipe:
    """Create a straight circular pipe using SI lengths."""

    return CircularPipe(
        length=length, diameter=diameter, roughness=roughness, name=name
    )


def rectangular_channel(
    *,
    length: float,
    height: float,
    width: float,
    roughness: float = 0.0,
    name: str = "channel",
) -> RectangularChannel:
    return RectangularChannel(
        length=length,
        height=height,
        width=width,
        roughness=roughness,
        name=name,
    )


Domain = CircularPipe | RectangularChannel


__all__ = [
    "CircularPipe",
    "Domain",
    "RectangularChannel",
    "WallBaffle",
    "circular_pipe",
    "rectangular_channel",
]
