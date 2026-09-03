"""Neutral exchange records for CFD, FEM, experiments, and learned models."""

from __future__ import annotations

from dataclasses import asdict, dataclass


_CANONICAL_FIELDS = {
    "fluid.velocity",
    "fluid.pressure",
    "fluid.density",
    "fluid.temperature",
    "fluid.wall_shear",
    "thermal.temperature",
    "thermal.heat_flux",
    "solid.displacement",
    "solid.velocity",
    "solid.traction",
}
_DIRECTIONS = {"cfd-to-fem", "fem-to-cfd", "bidirectional", "simulation-to-learning"}
_LOCATIONS = {"point", "cell", "facet", "global"}


@dataclass(frozen=True, slots=True)
class ExchangeField:
    name: str
    direction: str
    location: str
    unit: str
    conservative: bool = False

    def __post_init__(self) -> None:
        if self.name not in _CANONICAL_FIELDS:
            raise ValueError(f"Unknown canonical field {self.name!r}.")
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"Unknown exchange direction {self.direction!r}.")
        if self.location not in _LOCATIONS:
            raise ValueError(f"Unknown field location {self.location!r}.")
        if not self.unit:
            raise ValueError("Exchange fields require an explicit unit.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CouplingManifest:
    interface: str
    source_model_sha256: str
    target: str
    coordinate_frame: str
    mesh_sha256: str
    fields: tuple[ExchangeField, ...]
    time_coordinate: str = "time"
    schema: str = "agentcae.coupling-manifest/0.1"

    def __post_init__(self) -> None:
        if not self.interface or not self.target or not self.coordinate_frame:
            raise ValueError("Interface, target, and coordinate frame are required.")
        for digest in (self.source_model_sha256, self.mesh_sha256):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
                raise ValueError("Model and mesh identities must be SHA-256 hex digests.")
        if not self.fields:
            raise ValueError("At least one exchange field is required.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "interface": self.interface,
            "source_model_sha256": self.source_model_sha256,
            "target": self.target,
            "coordinate_frame": self.coordinate_frame,
            "mesh_sha256": self.mesh_sha256,
            "time_coordinate": self.time_coordinate,
            "fields": [field.to_dict() for field in self.fields],
        }


def fluid_loads_to_solid(
    *,
    interface: str,
    source_model_sha256: str,
    target: str,
    coordinate_frame: str,
    mesh_sha256: str,
    include_temperature: bool = True,
) -> CouplingManifest:
    fields = [
        ExchangeField("fluid.pressure", "cfd-to-fem", "facet", "Pa", conservative=True),
        ExchangeField("solid.traction", "cfd-to-fem", "facet", "Pa", conservative=True),
    ]
    if include_temperature:
        fields.append(ExchangeField("thermal.temperature", "cfd-to-fem", "facet", "K"))
    return CouplingManifest(
        interface=interface,
        source_model_sha256=source_model_sha256,
        target=target,
        coordinate_frame=coordinate_frame,
        mesh_sha256=mesh_sha256,
        fields=tuple(fields),
    )
