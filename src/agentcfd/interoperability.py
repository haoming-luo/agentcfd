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
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("Exchange fields require an explicit unit.")
        if not isinstance(self.conservative, bool):
            raise ValueError("Exchange field conservative must be a boolean.")

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
        for name in ("interface", "target", "coordinate_frame", "time_coordinate"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "Interface, target, coordinate frame, and time coordinate are required."
                )
        if self.schema != "agentcae.coupling-manifest/0.1":
            raise ValueError("Unsupported coupling-manifest schema.")
        if not isinstance(self.fields, tuple) or any(
            not isinstance(field, ExchangeField) for field in self.fields
        ):
            raise ValueError("Coupling fields must be ExchangeField records.")
        if not self.fields:
            raise ValueError("At least one exchange field is required.")
        for digest in (self.source_model_sha256, self.mesh_sha256):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest.lower()
                )
            ):
                raise ValueError("Model and mesh identities must be SHA-256 hex digests.")
        names = tuple(field.name for field in self.fields)
        if len(set(names)) != len(names):
            raise ValueError("Coupling fields must not contain duplicate canonical names.")

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
