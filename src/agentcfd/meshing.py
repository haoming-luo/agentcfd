"""Solver-neutral mesh intent and quality gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ._validation import integer_at_least, positive_float


@dataclass(frozen=True, slots=True)
class LocalSizing:
    regions: tuple[str, ...]
    size: float

    def __post_init__(self) -> None:
        selected = tuple(self.regions)
        if not selected or any(
            not isinstance(name, str) or not name.strip() for name in selected
        ):
            raise ValueError("Local sizing requires non-empty region names.")
        if len(set(selected)) != len(selected):
            raise ValueError("Local sizing region names must be unique.")
        object.__setattr__(self, "regions", selected)
        object.__setattr__(
            self, "size", positive_float(self.size, name="Local mesh size")
        )

    def to_dict(self) -> dict[str, object]:
        return {"regions": list(self.regions), "size": self.size}


@dataclass(frozen=True, slots=True)
class BoundaryLayers:
    regions: tuple[str, ...]
    count: int
    first_height: float
    growth_rate: float = 1.2

    def __post_init__(self) -> None:
        selected = tuple(self.regions)
        if not selected or any(
            not isinstance(name, str) or not name.strip() for name in selected
        ):
            raise ValueError("Boundary layers require non-empty wall region names.")
        if len(set(selected)) != len(selected):
            raise ValueError("Boundary-layer region names must be unique.")
        object.__setattr__(self, "regions", selected)
        object.__setattr__(
            self,
            "count",
            integer_at_least(self.count, name="Boundary-layer count", minimum=1),
        )
        object.__setattr__(
            self,
            "first_height",
            positive_float(self.first_height, name="First boundary-layer height"),
        )
        rate = positive_float(self.growth_rate, name="Boundary-layer growth rate")
        if rate < 1.0:
            raise ValueError("Boundary-layer growth rate must be at least one.")
        object.__setattr__(self, "growth_rate", rate)

    @property
    def total_thickness(self) -> float:
        if self.growth_rate == 1.0:
            return self.count * self.first_height
        return (
            self.first_height
            * (self.growth_rate**self.count - 1.0)
            / (self.growth_rate - 1.0)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "regions": list(self.regions),
            "total_thickness": self.total_thickness,
        }


@dataclass(frozen=True, slots=True)
class MeshQuality:
    maximum_non_orthogonality: float = 65.0
    maximum_skewness: float = 4.0
    maximum_aspect_ratio: float = 1000.0
    maximum_concavity: float = 80.0

    def __post_init__(self) -> None:
        for attribute, label in (
            ("maximum_non_orthogonality", "Maximum mesh non-orthogonality"),
            ("maximum_skewness", "Maximum mesh skewness"),
            ("maximum_aspect_ratio", "Maximum mesh aspect ratio"),
            ("maximum_concavity", "Maximum mesh concavity"),
        ):
            object.__setattr__(
                self,
                attribute,
                positive_float(getattr(self, attribute), name=label),
            )
        if self.maximum_non_orthogonality >= 90.0:
            raise ValueError("Maximum mesh non-orthogonality must be below 90 degrees.")
        if self.maximum_concavity >= 180.0:
            raise ValueError("Maximum mesh concavity must be below 180 degrees.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MeshIntent:
    method: str
    base_size: float
    growth_rate: float = 1.2
    local_sizing: tuple[LocalSizing, ...] = ()
    boundary_layers: tuple[BoundaryLayers, ...] = ()
    quality: MeshQuality = MeshQuality()
    maximum_cells: int = 2_000_000

    def __post_init__(self) -> None:
        if self.method not in {"automatic", "structured"}:
            raise ValueError("Mesh method must be 'automatic' or 'structured'.")
        object.__setattr__(
            self, "base_size", positive_float(self.base_size, name="Base mesh size")
        )
        rate = positive_float(self.growth_rate, name="Mesh growth rate")
        if rate < 1.0:
            raise ValueError("Mesh growth rate must be at least one.")
        object.__setattr__(self, "growth_rate", rate)
        local = tuple(self.local_sizing)
        layers = tuple(self.boundary_layers)
        if any(not isinstance(item, LocalSizing) for item in local):
            raise TypeError("Mesh local_sizing must contain LocalSizing objects.")
        if any(not isinstance(item, BoundaryLayers) for item in layers):
            raise TypeError("Mesh boundary_layers must contain BoundaryLayers objects.")
        if not isinstance(self.quality, MeshQuality):
            raise TypeError("Mesh quality must be an AgentCFD MeshQuality object.")
        object.__setattr__(
            self,
            "maximum_cells",
            integer_at_least(
                self.maximum_cells,
                name="Maximum mesh cells",
                minimum=1_000,
            ),
        )
        local_regions = [name for item in local for name in item.regions]
        if len(set(local_regions)) != len(local_regions):
            raise ValueError(
                "A region cannot have multiple conflicting local mesh sizes."
            )
        layer_regions = [name for item in layers for name in item.regions]
        if len(set(layer_regions)) != len(layer_regions):
            raise ValueError(
                "A region cannot have multiple conflicting boundary-layer controls."
            )
        coarsened = sorted(
            name
            for item in local
            if item.size > self.base_size
            for name in item.regions
        )
        if coarsened:
            raise ValueError(
                "Local refinement size cannot exceed the base size; invalid regions: "
                + ", ".join(coarsened)
                + "."
            )
        object.__setattr__(self, "local_sizing", local)
        object.__setattr__(self, "boundary_layers", layers)

    @property
    def referenced_regions(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                name
                for control in (*self.local_sizing, *self.boundary_layers)
                for name in control.regions
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "base_size": self.base_size,
            "growth_rate": self.growth_rate,
            "local_sizing": [item.to_dict() for item in self.local_sizing],
            "boundary_layers": [item.to_dict() for item in self.boundary_layers],
            "quality": self.quality.to_dict(),
            "maximum_cells": self.maximum_cells,
        }


def refine(*regions: str, size: float) -> LocalSizing:
    return LocalSizing(regions=tuple(regions), size=size)


def layers(
    *regions: str,
    count: int,
    first_height: float,
    growth_rate: float = 1.2,
) -> BoundaryLayers:
    return BoundaryLayers(
        regions=tuple(regions),
        count=count,
        first_height=first_height,
        growth_rate=growth_rate,
    )


def automatic(
    *,
    base_size: float,
    growth_rate: float = 1.2,
    local_sizing: tuple[LocalSizing, ...] = (),
    boundary_layers: tuple[BoundaryLayers, ...] = (),
    quality: MeshQuality | None = None,
    maximum_cells: int = 2_000_000,
) -> MeshIntent:
    return MeshIntent(
        method="automatic",
        base_size=base_size,
        growth_rate=growth_rate,
        local_sizing=local_sizing,
        boundary_layers=boundary_layers,
        quality=quality or MeshQuality(),
        maximum_cells=maximum_cells,
    )


def structured(
    *,
    base_size: float,
    quality: MeshQuality | None = None,
    maximum_cells: int = 2_000_000,
) -> MeshIntent:
    return MeshIntent(
        method="structured",
        base_size=base_size,
        quality=quality or MeshQuality(),
        maximum_cells=maximum_cells,
    )


__all__ = [
    "BoundaryLayers",
    "LocalSizing",
    "MeshIntent",
    "MeshQuality",
    "automatic",
    "layers",
    "refine",
    "structured",
]
