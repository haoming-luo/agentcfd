"""Named geometric targets shared by models, boundaries, meshes, and outputs."""

from __future__ import annotations

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
        if self.kind == "point" and self.location is None:
            raise ValueError("Point regions require a location.")
        if self.kind != "point" and self.location is not None:
            raise ValueError("Only point regions accept a location.")

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        if self.location is None:
            record.pop("location")
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


__all__ = ["Region", "point", "surface", "volume"]
