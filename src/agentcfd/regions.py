"""Named geometric targets shared by models, boundaries, meshes, and outputs."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ._validation import finite_float


_KINDS = {"volume", "surface", "point", "section"}


@dataclass(frozen=True, slots=True)
class Region:
    """A stable public name for part of a CFD domain."""

    name: str
    kind: str
    role: str = ""
    location: tuple[float, float, float] | None = None
    normal: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Region name must be a non-empty string.")
        object.__setattr__(self, "name", self.name.strip())
        if self.kind not in _KINDS:
            raise ValueError(f"Region kind must be one of {sorted(_KINDS)}.")
        if not isinstance(self.role, str):
            raise ValueError("Region role must be a string.")
        object.__setattr__(self, "role", self.role.strip())
        if self.location is not None:
            if not isinstance(self.location, (tuple, list)) or len(self.location) != 3:
                raise ValueError(
                    "Region location must contain exactly three coordinates."
                )
            object.__setattr__(
                self,
                "location",
                tuple(
                    finite_float(value, name="Region coordinate")
                    for value in self.location
                ),
            )
        if self.normal is not None:
            if not isinstance(self.normal, (tuple, list)) or len(self.normal) != 3:
                raise ValueError("Region normal must contain exactly three coordinates.")
            normal = tuple(
                finite_float(value, name="Region normal coordinate")
                for value in self.normal
            )
            magnitude = math.hypot(*normal)
            if magnitude == 0.0:
                raise ValueError("Region normal cannot be the zero vector.")
            object.__setattr__(
                self,
                "normal",
                tuple(value / magnitude for value in normal),
            )
        if self.kind == "point" and self.location is None:
            raise ValueError("Point regions require a location.")
        if self.kind == "section" and (
            self.location is None or self.normal is None
        ):
            raise ValueError("Section regions require an origin and normal.")
        if self.kind not in {"point", "section"} and self.location is not None:
            raise ValueError("Only point and section regions accept a location.")
        if self.kind != "section" and self.normal is not None:
            raise ValueError("Only section regions accept a normal.")

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        if self.location is None:
            record.pop("location")
        elif self.kind == "section":
            record["origin"] = list(record.pop("location"))
        else:
            record["location"] = list(record["location"])
        if self.normal is None:
            record.pop("normal")
        else:
            record["normal"] = list(record["normal"])
        return record


def volume(name: str, *, role: str = "fluid") -> Region:
    return Region(name=name, kind="volume", role=role)


def surface(name: str, *, role: str = "") -> Region:
    return Region(name=name, kind="surface", role=role)


def point(
    name: str,
    *,
    at: tuple[float, float, float],
    role: str = "observation",
) -> Region:
    return Region(name=name, kind="point", role=role, location=at)


def plane(
    name: str,
    *,
    origin: tuple[float, float, float],
    normal: tuple[float, float, float],
    role: str = "observation",
) -> Region:
    """Declare a reusable infinite measurement plane in SI coordinates."""

    return Region(
        name=name,
        kind="section",
        role=role,
        location=origin,
        normal=normal,
    )


__all__ = ["Region", "plane", "point", "surface", "volume"]
