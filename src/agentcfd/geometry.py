"""Geometry assets used by the public workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import PurePosixPath
from typing import Mapping

import math

from ._validation import nonnegative_float, positive_float
from .regions import Region, surface, volume


@dataclass(frozen=True, slots=True)
class ImportedSurface:
    """Content-addressed triangulated fluid domain with confirmed surface roles."""

    asset: str
    source_sha256: str
    source_format: str
    unit: str
    scale_to_m: float
    boundary_roles: tuple[tuple[str, str], ...]
    bounds_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    enclosed_volume_m3: float | None = None
    merge_tolerance_native: float = 0.0
    interior_point_m: tuple[float, float, float] | None = None
    connected_component_count: int | None = None
    multiple_components_accepted: bool = False
    name: str = "imported-fluid"

    def __post_init__(self) -> None:
        path = PurePosixPath(str(self.asset))
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError(
                "Imported surface asset must be a safe project-relative path."
            )
        object.__setattr__(self, "asset", path.as_posix())
        digest = str(self.source_sha256).lower()
        if not (
            digest.startswith("sha256:")
            and len(digest) == 71
            and all(character in "0123456789abcdef" for character in digest[7:])
        ):
            raise ValueError("Imported surface source_sha256 must be sha256:<64 hex>.")
        object.__setattr__(self, "source_sha256", digest)
        if self.source_format not in {"stl", "obj"}:
            raise ValueError("Imported surface format must be 'stl' or 'obj'.")
        if self.unit not in {"m", "mm", "cm", "um", "in", "ft"}:
            raise ValueError("Imported surface unit is unsupported.")
        object.__setattr__(
            self, "scale_to_m", positive_float(self.scale_to_m, name="Geometry scale")
        )
        roles = tuple(
            sorted((str(name), str(role)) for name, role in self.boundary_roles)
        )
        if not roles or any(
            not name.strip() or not role.strip() for name, role in roles
        ):
            raise ValueError(
                "Imported surface requires non-empty confirmed boundary roles."
            )
        if len({name for name, _role in roles}) != len(roles):
            raise ValueError("Imported surface boundary region names must be unique.")
        object.__setattr__(self, "boundary_roles", roles)
        bounds = tuple(
            tuple(float(value) for value in point) for point in self.bounds_m
        )
        if len(bounds) != 2 or any(len(point) != 3 for point in bounds):
            raise ValueError(
                "Imported surface SI bounds require minimum and maximum 3-vectors."
            )
        if any(not math.isfinite(value) for point in bounds for value in point):
            raise ValueError("Imported surface SI bounds must be finite.")
        if any(high <= low for low, high in zip(bounds[0], bounds[1])):
            raise ValueError(
                "Imported surface SI bounds must have positive extent on every axis."
            )
        object.__setattr__(self, "bounds_m", bounds)
        if self.interior_point_m is not None:
            point = tuple(float(value) for value in self.interior_point_m)
            if len(point) != 3 or any(not math.isfinite(value) for value in point):
                raise ValueError(
                    "Imported surface interior point must be a finite 3-vector."
                )
            if any(
                value <= low or value >= high
                for value, low, high in zip(point, bounds[0], bounds[1])
            ):
                raise ValueError(
                    "Imported surface interior point must lie strictly inside its SI bounds."
                )
            object.__setattr__(self, "interior_point_m", point)
        if self.enclosed_volume_m3 is not None:
            object.__setattr__(
                self,
                "enclosed_volume_m3",
                positive_float(self.enclosed_volume_m3, name="Enclosed volume"),
            )
        object.__setattr__(
            self,
            "merge_tolerance_native",
            nonnegative_float(
                self.merge_tolerance_native, name="Geometry merge tolerance"
            ),
        )
        if self.connected_component_count is not None:
            if (
                isinstance(self.connected_component_count, bool)
                or not isinstance(self.connected_component_count, int)
                or self.connected_component_count < 1
            ):
                raise ValueError(
                    "Imported surface connected_component_count must be a positive integer."
                )
        if not isinstance(self.multiple_components_accepted, bool):
            raise ValueError(
                "Imported surface multiple_components_accepted must be boolean."
            )
        if self.multiple_components_accepted and (
            self.connected_component_count is None
            or self.connected_component_count <= 1
        ):
            raise ValueError(
                "Multiple-component acceptance requires a component count above one."
            )
        if (
            self.connected_component_count is not None
            and self.connected_component_count > 1
            and not self.multiple_components_accepted
        ):
            raise ValueError(
                "Imported surface with multiple components requires explicit acceptance."
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Imported surface name must be a non-empty string.")
        object.__setattr__(self, "name", self.name.strip())

    @property
    def regions(self) -> tuple[Region, ...]:
        return (
            volume("fluid", role="fluid"),
            *(surface(name, role=role) for name, role in self.boundary_roles),
        )

    @property
    def surface_names(self) -> tuple[str, ...]:
        return tuple(name for name, _role in self.boundary_roles)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "imported-surface",
            "name": self.name,
            "asset": self.asset,
            "source_sha256": self.source_sha256,
            "source_format": self.source_format,
            "unit": self.unit,
            "scale_to_m": self.scale_to_m,
            "merge_tolerance_native": self.merge_tolerance_native,
            "connected_component_count": self.connected_component_count,
            "multiple_components_accepted": self.multiple_components_accepted,
            "bounds_m": [list(point) for point in self.bounds_m],
            "enclosed_volume_m3": self.enclosed_volume_m3,
            "interior_point_m": (
                None if self.interior_point_m is None else list(self.interior_point_m)
            ),
            "boundary_roles": dict(self.boundary_roles),
            "regions": [item.to_dict() for item in self.regions],
        }


def imported_surface_from_inspection(
    inspection: Mapping[str, object],
    *,
    asset: str,
    name: str = "imported-fluid",
    interior_point_m: tuple[float, float, float] | None = None,
) -> ImportedSurface:
    """Create deterministic model intent from a geometry-check report."""

    if inspection.get("schema") != "agentcfd.geometry-inspection/0.1":
        raise ValueError("Inspection must use agentcfd.geometry-inspection/0.1.")
    readiness = inspection.get("readiness")
    if not isinstance(readiness, Mapping) or not (
        readiness.get("geometry_ready") is True
        and readiness.get("boundary_roles_ready") is True
    ):
        raise ValueError(
            "Imported surface requires geometry and boundary-role readiness."
        )
    source = inspection.get("source")
    surface_record = inspection.get("surface")
    role_record = inspection.get("boundary_roles")
    policy = inspection.get("policy")
    if not all(
        isinstance(record, Mapping)
        for record in (source, surface_record, role_record, policy)
    ):
        raise ValueError("Geometry inspection is missing required records.")
    if (
        surface_record.get("watertight") is not True
        or surface_record.get("enclosed_volume_m3") is None
    ):
        raise ValueError(
            "Imported volume CFD domain requires a watertight surface with enclosed volume."
        )
    confirmed = role_record.get("confirmed")
    bounds = surface_record.get("bounds_m")
    if not isinstance(confirmed, Mapping) or not isinstance(bounds, Mapping):
        raise ValueError("Geometry inspection lacks confirmed roles or SI bounds.")
    minimum = bounds.get("minimum")
    maximum = bounds.get("maximum")
    if not isinstance(minimum, list) or not isinstance(maximum, list):
        raise ValueError("Geometry inspection SI bounds are malformed.")
    raw_component_count = surface_record.get("connected_component_count")
    if isinstance(raw_component_count, bool) or (
        raw_component_count is not None and not isinstance(raw_component_count, int)
    ):
        raise ValueError("Geometry inspection connected-component count is malformed.")
    return ImportedSurface(
        asset=asset,
        source_sha256=str(source.get("sha256")),
        source_format=str(source.get("format")),
        unit=str(source.get("unit")),
        scale_to_m=float(source.get("scale_to_m")),
        boundary_roles=tuple(
            (str(key), str(value)) for key, value in confirmed.items()
        ),
        bounds_m=(tuple(minimum), tuple(maximum)),
        enclosed_volume_m3=surface_record.get("enclosed_volume_m3"),
        merge_tolerance_native=float(policy.get("merge_tolerance_native", 0.0)),
        connected_component_count=(
            int(raw_component_count) if raw_component_count is not None else None
        ),
        multiple_components_accepted=(
            raw_component_count is not None
            and raw_component_count > 1
            and policy.get("accept_multiple_components") is True
        ),
        interior_point_m=interior_point_m,
        name=name,
    )


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


Domain = CircularPipe | RectangularChannel | ImportedSurface


__all__ = [
    "CircularPipe",
    "Domain",
    "ImportedSurface",
    "RectangularChannel",
    "WallBaffle",
    "circular_pipe",
    "imported_surface_from_inspection",
    "rectangular_channel",
]
