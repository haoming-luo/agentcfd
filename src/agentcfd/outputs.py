"""Backend-neutral output intent for fields, histories, restart, and storage."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from ._validation import finite_float, integer_at_least, positive_float


_BYTE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}


def _canonical_names(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    selected = tuple(values)
    if any(not isinstance(item, str) or not item.strip() for item in selected):
        raise ValueError(f"{label} must contain non-empty canonical names.")
    if len(set(selected)) != len(selected):
        raise ValueError(f"{label} must not contain duplicates.")
    return selected


@dataclass(frozen=True, slots=True)
class PointProbe:
    """Sample canonical fields at a fixed physical location."""

    name: str
    location: tuple[float, float, float]
    fields: tuple[str, ...] = ("fluid.velocity", "fluid.pressure")
    every: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Probe name must be a non-empty string.")
        if not isinstance(self.location, (tuple, list)) or len(self.location) != 3:
            raise ValueError("Probe location must contain three coordinates.")
        object.__setattr__(
            self,
            "location",
            tuple(
                finite_float(value, name="Probe coordinate") for value in self.location
            ),
        )
        object.__setattr__(
            self,
            "fields",
            _canonical_names(tuple(self.fields), label="Probe fields"),
        )
        object.__setattr__(
            self,
            "every",
            integer_at_least(self.every, name="Probe interval", minimum=1),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "point-probe",
            "name": self.name,
            "location": list(self.location),
            "fields": list(self.fields),
            "every": self.every,
        }


@dataclass(frozen=True, slots=True)
class SurfaceReport:
    """Reduce a field over a named surface without retaining a full field frame."""

    name: str
    region: str
    field: str
    operation: str = "area-average"
    every: int = 1

    def __post_init__(self) -> None:
        for attribute, label in (
            ("name", "Surface report name"),
            ("region", "Surface report region"),
            ("field", "Surface report field"),
        ):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string.")
        operations = {
            "minimum",
            "maximum",
            "area-average",
            "area-integral",
            "mass-flow-average",
            "flow-rate",
            "uniformity",
        }
        if self.operation not in operations:
            raise ValueError(
                "Surface report operation must be minimum, maximum, area-average, "
                "area-integral, mass-flow-average, flow-rate, or uniformity."
            )
        object.__setattr__(
            self,
            "every",
            integer_at_least(self.every, name="Surface report interval", minimum=1),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "surface-report", **asdict(self)}


@dataclass(frozen=True, slots=True)
class ForceReport:
    """Integrate pressure and viscous force over named wall surfaces."""

    name: str
    regions: tuple[str, ...]
    direction: tuple[float, float, float] = (1.0, 0.0, 0.0)
    center: tuple[float, float, float] | None = None
    every: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Force report name must be a non-empty string.")
        object.__setattr__(
            self,
            "regions",
            _canonical_names(tuple(self.regions), label="Force report regions"),
        )
        if not isinstance(self.direction, (tuple, list)) or len(self.direction) != 3:
            raise ValueError("Force direction must contain three components.")
        direction = tuple(
            finite_float(value, name="Force direction component")
            for value in self.direction
        )
        magnitude = math.sqrt(sum(value * value for value in direction))
        if magnitude == 0.0:
            raise ValueError("Force direction cannot be the zero vector.")
        object.__setattr__(
            self, "direction", tuple(value / magnitude for value in direction)
        )
        if self.center is not None:
            if not isinstance(self.center, (tuple, list)) or len(self.center) != 3:
                raise ValueError("Force center must contain three coordinates.")
            object.__setattr__(
                self,
                "center",
                tuple(
                    finite_float(value, name="Force center coordinate")
                    for value in self.center
                ),
            )
        object.__setattr__(
            self,
            "every",
            integer_at_least(self.every, name="Force report interval", minimum=1),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "force-report",
            "name": self.name,
            "regions": list(self.regions),
            "direction": list(self.direction),
            "center": None if self.center is None else list(self.center),
            "every": self.every,
        }


Report = PointProbe | SurfaceReport | ForceReport


def _view_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) is None
    ):
        raise ValueError(
            "Post-processing view names must start with a letter and contain only "
            "letters, numbers, underscores, or hyphens."
        )
    return value


def _view_vector(
    value: tuple[float, float, float], *, label: str
) -> tuple[float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{label} must contain three coordinates.")
    return tuple(finite_float(item, name=f"{label} coordinate") for item in value)


@dataclass(frozen=True, slots=True)
class SliceView:
    """A reproducible plane slice colored by one canonical field."""

    name: str
    field: str
    origin: tuple[float, float, float]
    normal: tuple[float, float, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _view_name(self.name))
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("Slice field must be a non-empty canonical name.")
        object.__setattr__(
            self, "origin", _view_vector(self.origin, label="Slice origin")
        )
        normal = _view_vector(self.normal, label="Slice normal")
        magnitude = math.sqrt(sum(value * value for value in normal))
        if magnitude == 0.0:
            raise ValueError("Slice normal cannot be the zero vector.")
        object.__setattr__(self, "normal", tuple(value / magnitude for value in normal))

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "slice",
            "name": self.name,
            "field": self.field,
            "origin": list(self.origin),
            "normal": list(self.normal),
        }


@dataclass(frozen=True, slots=True)
class ContourView:
    """Reproducible scalar isosurfaces at explicit engineering values."""

    name: str
    field: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _view_name(self.name))
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("Contour field must be a non-empty canonical name.")
        values = tuple(
            finite_float(value, name="Contour value") for value in self.values
        )
        if not values:
            raise ValueError("Contour values cannot be empty.")
        if len(set(values)) != len(values):
            raise ValueError("Contour values must not contain duplicates.")
        object.__setattr__(self, "values", values)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "contour",
            "name": self.name,
            "field": self.field,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class StreamlineView:
    """Reproducible streamlines seeded from a physical line segment."""

    name: str
    field: str = "fluid.velocity"
    seed_start: tuple[float, float, float] = (0.0, 0.0, 0.0)
    seed_end: tuple[float, float, float] = (0.0, 1.0, 0.0)
    seeds: int = 40
    direction: str = "both"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _view_name(self.name))
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("Streamline field must be a non-empty canonical name.")
        object.__setattr__(
            self,
            "seed_start",
            _view_vector(self.seed_start, label="Streamline seed start"),
        )
        object.__setattr__(
            self,
            "seed_end",
            _view_vector(self.seed_end, label="Streamline seed end"),
        )
        if self.seed_start == self.seed_end:
            raise ValueError("Streamline seed line must have nonzero length.")
        object.__setattr__(
            self,
            "seeds",
            integer_at_least(self.seeds, name="Streamline seed count", minimum=2),
        )
        if self.direction not in {"forward", "backward", "both"}:
            raise ValueError("Streamline direction must be forward, backward, or both.")

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "streamlines",
            "name": self.name,
            "field": self.field,
            "seed_start": list(self.seed_start),
            "seed_end": list(self.seed_end),
            "seeds": self.seeds,
            "direction": self.direction,
        }


ViewRecipe = SliceView | ContourView | StreamlineView


def parse_storage_size(value: int | str) -> int:
    """Normalize a human-readable storage budget to bytes.

    Integer values are already bytes. Strings accept SI and IEC units, for
    example ``"750 MB"`` and ``"2 GiB"``.
    """

    if isinstance(value, bool):
        raise ValueError("Storage budget must be bytes or a value such as '2 GiB'.")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("Storage budget must be positive.")
        return value
    if not isinstance(value, str):
        raise ValueError("Storage budget must be bytes or a value such as '2 GiB'.")
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|KiB|MiB|GiB)\s*",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("Storage budget must look like '750 MB', '2 GiB', or bytes.")
    size = float(match.group(1))
    result = size * _BYTE_UNITS[match.group(2).lower()]
    if not math.isfinite(result) or result < 1 or result > 2**63 - 1:
        raise ValueError("Storage budget is outside the supported range.")
    return int(result)


@dataclass(frozen=True, slots=True)
class FieldFrames:
    """Retention policy for full mesh fields, separate from solver timesteps."""

    mode: str = "final"
    every: float | None = None
    coordinate: str = "physical-time"
    maximum: int = 200
    include_initial: bool = False
    include_final: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"final", "interval"}:
            raise ValueError("Field frame mode must be 'final' or 'interval'.")
        if self.coordinate not in {"physical-time", "solver-iteration"}:
            raise ValueError(
                "Field frame coordinate must be 'physical-time' or 'solver-iteration'."
            )
        if self.mode == "interval":
            object.__setattr__(
                self,
                "every",
                positive_float(self.every, name="Field frame interval"),
            )
        elif self.every is not None:
            raise ValueError("Final-only fields do not accept an interval.")
        object.__setattr__(
            self,
            "maximum",
            integer_at_least(self.maximum, name="Maximum field frames", minimum=1),
        )
        for name in ("include_initial", "include_final"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"Field frames {name} must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Checkpoints:
    """Sparse native solver state retained only for restart and recovery."""

    every: float | None = None
    coordinate: str = "physical-time"
    keep: int = 2

    def __post_init__(self) -> None:
        if self.coordinate not in {"physical-time", "solver-iteration"}:
            raise ValueError(
                "Checkpoint coordinate must be 'physical-time' or 'solver-iteration'."
            )
        if self.every is not None:
            object.__setattr__(
                self,
                "every",
                positive_float(self.every, name="Checkpoint interval"),
            )
        object.__setattr__(
            self,
            "keep",
            integer_at_least(self.keep, name="Retained checkpoints", minimum=1),
        )

    @property
    def enabled(self) -> bool:
        return self.every is not None

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    """A fail-closed budget for portable and temporary full-field data."""

    maximum_bytes: int = 2 * 1024**3
    compression: str = "gzip"
    on_exceed: str = "error"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_bytes",
            parse_storage_size(self.maximum_bytes),
        )
        if self.compression not in {"none", "gzip", "lzf"}:
            raise ValueError("Portable compression must be 'none', 'gzip', or 'lzf'.")
        if self.on_exceed != "error":
            raise ValueError(
                "Storage on_exceed currently supports only fail-closed 'error'."
            )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OutputRequest:
    fields: tuple[str, ...]
    histories: tuple[str, ...]
    portable_profile: str = "visualization"
    portable_formats: tuple[str, ...] = ("xdmf",)
    frames: FieldFrames = field(default_factory=FieldFrames)
    checkpoints: Checkpoints = field(default_factory=Checkpoints)
    storage: StoragePolicy = field(default_factory=StoragePolicy)
    reports: tuple[Report, ...] = ()
    views: tuple[ViewRecipe, ...] = ()

    def __post_init__(self) -> None:
        for name in ("fields", "histories"):
            object.__setattr__(
                self,
                name,
                _canonical_names(tuple(getattr(self, name)), label=f"Output {name}"),
            )
        if self.portable_profile not in {"visualization", "native", "both"}:
            raise ValueError(
                "portable_profile must be 'visualization', 'native', or 'both'."
            )
        formats = tuple(self.portable_formats)
        if (
            not formats
            or any(item not in {"xdmf", "npz"} for item in formats)
            or len(set(formats)) != len(formats)
        ):
            raise ValueError(
                "portable_formats must be unique and contain only 'xdmf' and optional 'npz'."
            )
        if "xdmf" not in formats:
            raise ValueError("portable_formats must include 'xdmf'.")
        object.__setattr__(self, "portable_formats", formats)
        if not isinstance(self.frames, FieldFrames):
            raise TypeError("Output frames must be an AgentCFD FieldFrames policy.")
        if not isinstance(self.checkpoints, Checkpoints):
            raise TypeError(
                "Output checkpoints must be an AgentCFD Checkpoints policy."
            )
        if not isinstance(self.storage, StoragePolicy):
            raise TypeError("Output storage must be an AgentCFD StoragePolicy.")
        selected_reports = tuple(self.reports)
        if any(
            not isinstance(item, (PointProbe, SurfaceReport, ForceReport))
            for item in selected_reports
        ):
            raise TypeError("Output reports must be AgentCFD report definitions.")
        if len({item.name for item in selected_reports}) != len(selected_reports):
            raise ValueError("Output report names must be unique.")
        object.__setattr__(self, "reports", selected_reports)
        selected_views = tuple(self.views)
        if any(
            not isinstance(item, (SliceView, ContourView, StreamlineView))
            for item in selected_views
        ):
            raise TypeError("Output views must be AgentCFD view recipes.")
        if len({item.name for item in selected_views}) != len(selected_views):
            raise ValueError("Output view names must be unique.")
        unavailable_fields = sorted(
            {item.field for item in selected_views} - set(self.fields)
        )
        if unavailable_fields:
            raise ValueError(
                "Post-processing views require their fields in output.fields; missing: "
                + ", ".join(unavailable_fields)
                + "."
            )
        object.__setattr__(self, "views", selected_views)

    def to_dict(self) -> dict[str, object]:
        return {
            "fields": list(self.fields),
            "histories": list(self.histories),
            "portable_profile": self.portable_profile,
            "portable_formats": list(self.portable_formats),
            "frames": self.frames.to_dict(),
            "checkpoints": self.checkpoints.to_dict(),
            "storage": self.storage.to_dict(),
            "reports": [item.to_dict() for item in self.reports],
            "views": [item.to_dict() for item in self.views],
        }


def probe(
    name: str,
    *,
    at: tuple[float, float, float],
    fields: tuple[str, ...] = ("fluid.velocity", "fluid.pressure"),
    every: int = 1,
) -> PointProbe:
    return PointProbe(name=name, location=at, fields=fields, every=every)


def surface_report(
    name: str,
    *,
    region: str,
    field: str,
    operation: str = "area-average",
    every: int = 1,
) -> SurfaceReport:
    return SurfaceReport(
        name=name,
        region=region,
        field=field,
        operation=operation,
        every=every,
    )


def force_report(
    name: str,
    *,
    regions: tuple[str, ...],
    direction: tuple[float, float, float] = (1.0, 0.0, 0.0),
    center: tuple[float, float, float] | None = None,
    every: int = 1,
) -> ForceReport:
    return ForceReport(
        name=name,
        regions=regions,
        direction=direction,
        center=center,
        every=every,
    )


def slice_view(
    name: str,
    *,
    field: str,
    origin: tuple[float, float, float],
    normal: tuple[float, float, float],
) -> SliceView:
    return SliceView(name=name, field=field, origin=origin, normal=normal)


def contour_view(
    name: str,
    *,
    field: str,
    values: tuple[float, ...],
) -> ContourView:
    return ContourView(name=name, field=field, values=values)


def streamline_view(
    name: str,
    *,
    seed_start: tuple[float, float, float],
    seed_end: tuple[float, float, float],
    seeds: int = 40,
    direction: str = "both",
    field: str = "fluid.velocity",
) -> StreamlineView:
    return StreamlineView(
        name=name,
        field=field,
        seed_start=seed_start,
        seed_end=seed_end,
        seeds=seeds,
        direction=direction,
    )


def checkpoints(
    *,
    every: float,
    keep: int = 2,
    coordinate: str = "physical-time",
) -> Checkpoints:
    return Checkpoints(every=every, keep=keep, coordinate=coordinate)


def storage(
    budget: int | str = "2 GiB",
    *,
    compression: str = "gzip",
) -> StoragePolicy:
    return StoragePolicy(
        maximum_bytes=parse_storage_size(budget),
        compression=compression,
    )


def standard(
    *,
    portable_profile: str = "visualization",
    portable_formats: tuple[str, ...] = ("xdmf",),
    storage_policy: StoragePolicy | None = None,
    reports: tuple[Report, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
) -> OutputRequest:
    """Keep the final full field plus compact histories."""

    return OutputRequest(
        fields=("fluid.velocity", "fluid.pressure"),
        histories=("flow.mass_balance", "flow.pressure_drop"),
        portable_profile=portable_profile,
        portable_formats=portable_formats,
        frames=FieldFrames(coordinate="solver-iteration"),
        checkpoints=Checkpoints(coordinate="solver-iteration"),
        storage=storage_policy or storage(),
        reports=reports,
        views=views,
    )


def animation(
    *,
    every: float,
    fields: tuple[str, ...] = (
        "fluid.velocity",
        "fluid.pressure",
        "fluid.vorticity",
    ),
    histories: tuple[str, ...] = ("flow.mass_balance", "flow.pressure_drop"),
    maximum_frames: int = 300,
    restart: Checkpoints | None = None,
    storage_budget: int | str = "2 GiB",
    compression: str = "gzip",
    portable_profile: str = "visualization",
    portable_formats: tuple[str, ...] = ("xdmf",),
    reports: tuple[Report, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
) -> OutputRequest:
    """Request physical-time animation without equating frames to solver steps."""

    return OutputRequest(
        fields=fields,
        histories=histories,
        portable_profile=portable_profile,
        portable_formats=portable_formats,
        frames=FieldFrames(
            mode="interval",
            every=every,
            coordinate="physical-time",
            maximum=maximum_frames,
        ),
        checkpoints=restart or Checkpoints(),
        storage=storage(storage_budget, compression=compression),
        reports=reports,
        views=views,
    )


def turbulent_internal_flow(
    *,
    turbulence_model: str = "k-omega-sst",
    portable_profile: str = "visualization",
    portable_formats: tuple[str, ...] = ("xdmf",),
    reports: tuple[Report, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
) -> OutputRequest:
    """Request the minimum auditable field set for two-equation RANS flow."""

    dissipation_fields = {
        "k-omega-sst": "turbulence.specific_dissipation_rate",
        "k-epsilon": "turbulence.dissipation_rate",
    }
    try:
        dissipation_field = dissipation_fields[turbulence_model]
    except KeyError as error:
        raise ValueError(
            "turbulence_model must be 'k-omega-sst' or 'k-epsilon'."
        ) from error

    return OutputRequest(
        fields=(
            "fluid.velocity",
            "fluid.pressure",
            "turbulence.kinetic_energy",
            dissipation_field,
            "turbulence.kinematic_eddy_viscosity",
        ),
        histories=(
            "flow.mass_balance",
            "flow.pressure_drop",
            "wall.y_plus",
        ),
        portable_profile=portable_profile,
        portable_formats=portable_formats,
        frames=FieldFrames(coordinate="solver-iteration"),
        checkpoints=Checkpoints(coordinate="solver-iteration"),
        reports=reports,
        views=views,
    )


__all__ = [
    "Checkpoints",
    "ContourView",
    "FieldFrames",
    "ForceReport",
    "OutputRequest",
    "PointProbe",
    "Report",
    "StoragePolicy",
    "StreamlineView",
    "SurfaceReport",
    "SliceView",
    "ViewRecipe",
    "animation",
    "checkpoints",
    "contour_view",
    "force_report",
    "parse_storage_size",
    "probe",
    "slice_view",
    "standard",
    "storage",
    "surface_report",
    "streamline_view",
    "turbulent_internal_flow",
]
