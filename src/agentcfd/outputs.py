"""Backend-neutral output intent for fields, histories, restart, and storage."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
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
class PressureLossReport:
    """Report total-pressure loss and its inlet-bulk loss coefficient."""

    name: str
    inlet: str
    outlet: str
    reference_area: float | None = None
    averaging: str = "mass-flow"
    every: int = 1

    def __post_init__(self) -> None:
        for attribute, label in (
            ("name", "Pressure-loss report name"),
            ("inlet", "Pressure-loss inlet"),
            ("outlet", "Pressure-loss outlet"),
        ):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string.")
        if self.inlet == self.outlet:
            raise ValueError(
                "Pressure-loss inlet and outlet must be different regions."
            )
        if self.reference_area is not None:
            object.__setattr__(
                self,
                "reference_area",
                positive_float(
                    self.reference_area,
                    name="Pressure-loss reference area",
                ),
            )
        if self.averaging != "mass-flow":
            raise ValueError(
                "Pressure-loss averaging currently supports only 'mass-flow'."
            )
        object.__setattr__(
            self,
            "every",
            integer_at_least(self.every, name="Pressure-loss interval", minimum=1),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "pressure-loss-report", **asdict(self)}


@dataclass(frozen=True, slots=True)
class FlowUniformityReport:
    """Report velocity uniformity and signed mean normal speed on a surface."""

    name: str
    region: str
    every: int = 1

    def __post_init__(self) -> None:
        for attribute, label in (
            ("name", "Flow-uniformity report name"),
            ("region", "Flow-uniformity report region"),
        ):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string.")
        object.__setattr__(
            self,
            "every",
            integer_at_least(
                self.every, name="Flow-uniformity report interval", minimum=1
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "flow-uniformity-report", **asdict(self)}


@dataclass(frozen=True, slots=True)
class FlowDistributionReport:
    """Report inlet-to-branch flow allocation without retaining field frames."""

    name: str
    inlet: str
    outlets: tuple[str, ...]
    target_fractions: tuple[tuple[str, float], ...] = ()
    every: int = 1

    def __post_init__(self) -> None:
        for attribute, label in (
            ("name", "Flow-distribution report name"),
            ("inlet", "Flow-distribution inlet"),
        ):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string.")
        if isinstance(self.outlets, (str, bytes)):
            raise ValueError(
                "Flow-distribution outlets must be a sequence of region names."
            )
        selected_outlets = _canonical_names(
            tuple(self.outlets), label="Flow-distribution outlets"
        )
        if len(selected_outlets) < 2:
            raise ValueError("Flow-distribution reports require at least two outlets.")
        if self.inlet in selected_outlets:
            raise ValueError("Flow-distribution inlet must not also be an outlet.")
        object.__setattr__(self, "outlets", selected_outlets)

        selected_targets: list[tuple[str, float]] = []
        for item in tuple(self.target_fractions):
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError(
                    "Flow-distribution target fractions must contain (outlet, fraction) pairs."
                )
            outlet, value = item
            if not isinstance(outlet, str) or not outlet.strip():
                raise ValueError(
                    "Flow-distribution target outlet must be a non-empty string."
                )
            selected_targets.append(
                (
                    outlet,
                    positive_float(value, name=f"Target fraction for {outlet!r}"),
                )
            )
        target_names = tuple(name for name, _ in selected_targets)
        if len(set(target_names)) != len(target_names):
            raise ValueError("Flow-distribution target outlets must be unique.")
        if selected_targets and set(target_names) != set(selected_outlets):
            raise ValueError(
                "Flow-distribution targets must specify every declared outlet exactly once."
            )
        if selected_targets and not math.isclose(
            sum(value for _, value in selected_targets),
            1.0,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        ):
            raise ValueError("Flow-distribution target fractions must sum to one.")
        target_by_name = dict(selected_targets)
        object.__setattr__(
            self,
            "target_fractions",
            tuple((name, target_by_name[name]) for name in selected_outlets)
            if selected_targets
            else (),
        )
        object.__setattr__(
            self,
            "every",
            integer_at_least(
                self.every, name="Flow-distribution report interval", minimum=1
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "flow-distribution-report",
            "name": self.name,
            "inlet": self.inlet,
            "outlets": list(self.outlets),
            "target_fractions": dict(self.target_fractions),
            "every": self.every,
        }


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


Report = (
    PointProbe
    | SurfaceReport
    | PressureLossReport
    | FlowUniformityReport
    | FlowDistributionReport
    | ForceReport
)


@dataclass(frozen=True, slots=True)
class QuantityCriterion:
    """A user-declared inclusive design bound on one canonical scalar result."""

    name: str
    quantity: str
    unit: str
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        for attribute, label in (
            ("name", "Quantity criterion name"),
            ("quantity", "Quantity criterion result"),
            ("unit", "Quantity criterion unit"),
        ):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string.")
        if self.minimum is None and self.maximum is None:
            raise ValueError("Quantity criterion requires minimum and/or maximum.")
        if self.minimum is not None:
            object.__setattr__(
                self,
                "minimum",
                finite_float(self.minimum, name="Quantity criterion minimum"),
            )
        if self.maximum is not None:
            object.__setattr__(
                self,
                "maximum",
                finite_float(self.maximum, name="Quantity criterion maximum"),
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("Quantity criterion minimum cannot exceed maximum.")

    def to_dict(self) -> dict[str, object]:
        return {"type": "quantity-criterion", **asdict(self)}


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
class ViewCamera:
    """Explicit ParaView camera intent in physical model coordinates."""

    position: tuple[float, float, float]
    focal_point: tuple[float, float, float]
    view_up: tuple[float, float, float] = (0.0, 1.0, 0.0)
    parallel_scale: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "position", _view_vector(self.position, label="Camera position")
        )
        object.__setattr__(
            self,
            "focal_point",
            _view_vector(self.focal_point, label="Camera focal point"),
        )
        view_up = _view_vector(self.view_up, label="Camera view-up")
        if math.sqrt(sum(value * value for value in view_up)) == 0.0:
            raise ValueError("Camera view-up cannot be the zero vector.")
        object.__setattr__(self, "view_up", view_up)
        if self.position == self.focal_point:
            raise ValueError("Camera position and focal point must differ.")
        if self.parallel_scale is not None:
            object.__setattr__(
                self,
                "parallel_scale",
                positive_float(self.parallel_scale, name="Camera parallel scale"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "position": list(self.position),
            "focal_point": list(self.focal_point),
            "view_up": list(self.view_up),
            "parallel_scale": self.parallel_scale,
        }


@dataclass(frozen=True, slots=True)
class ViewExport:
    """Optional reproducible screenshot or animation rendering intent."""

    size: tuple[int, int] = (1280, 720)
    screenshot: bool = True
    animation: str | None = None
    frame_rate: int = 24
    transparent_background: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.size, (tuple, list)) or len(self.size) != 2:
            raise ValueError("View export size must contain width and height.")
        size = tuple(
            integer_at_least(value, name="View export dimension", minimum=64)
            for value in self.size
        )
        object.__setattr__(self, "size", size)
        for name in ("screenshot", "transparent_background"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"View export {name} must be a boolean.")
        if self.animation not in {None, "png-sequence", "mp4"}:
            raise ValueError(
                "View export animation must be None, 'png-sequence', or 'mp4'."
            )
        object.__setattr__(
            self,
            "frame_rate",
            integer_at_least(self.frame_rate, name="Animation frame rate", minimum=1),
        )
        if not self.screenshot and self.animation is None:
            raise ValueError("View export must request a screenshot or animation.")

    def to_dict(self) -> dict[str, object]:
        return {
            "size": list(self.size),
            "screenshot": self.screenshot,
            "animation": self.animation,
            "frame_rate": self.frame_rate,
            "transparent_background": self.transparent_background,
        }


@dataclass(frozen=True, slots=True)
class SliceView:
    """A reproducible plane slice colored by one canonical field."""

    name: str
    field: str
    origin: tuple[float, float, float]
    normal: tuple[float, float, float]
    component: str | None = None
    camera: ViewCamera | None = None
    export: ViewExport | None = None

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
        if self.component not in {
            None,
            "x",
            "y",
            "z",
            "magnitude",
            "normal",
            "tangential",
        }:
            raise ValueError(
                "Slice component must be None, x, y, z, magnitude, normal, or "
                "tangential."
            )
        _validate_view_presentation(self.camera, self.export)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "slice",
            "name": self.name,
            "field": self.field,
            "origin": list(self.origin),
            "normal": list(self.normal),
            "component": self.component,
            "camera": None if self.camera is None else self.camera.to_dict(),
            "export": None if self.export is None else self.export.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ContourView:
    """Reproducible scalar isosurfaces at explicit engineering values."""

    name: str
    field: str
    values: tuple[float, ...]
    camera: ViewCamera | None = None
    export: ViewExport | None = None

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
        _validate_view_presentation(self.camera, self.export)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "contour",
            "name": self.name,
            "field": self.field,
            "values": list(self.values),
            "camera": None if self.camera is None else self.camera.to_dict(),
            "export": None if self.export is None else self.export.to_dict(),
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
    camera: ViewCamera | None = None
    export: ViewExport | None = None

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
        _validate_view_presentation(self.camera, self.export)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "streamlines",
            "name": self.name,
            "field": self.field,
            "seed_start": list(self.seed_start),
            "seed_end": list(self.seed_end),
            "seeds": self.seeds,
            "direction": self.direction,
            "camera": None if self.camera is None else self.camera.to_dict(),
            "export": None if self.export is None else self.export.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LineProfile:
    """Sample one canonical field along a physical line as compact CSV."""

    name: str
    field: str
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    samples: int = 101
    component: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _view_name(self.name))
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("Line-profile field must be a non-empty canonical name.")
        object.__setattr__(
            self, "start", _view_vector(self.start, label="Line-profile start")
        )
        object.__setattr__(
            self, "end", _view_vector(self.end, label="Line-profile end")
        )
        if self.start == self.end:
            raise ValueError("Line-profile segment must have nonzero length.")
        object.__setattr__(
            self,
            "samples",
            integer_at_least(self.samples, name="Line-profile sample count", minimum=2),
        )
        if self.component not in {None, "x", "y", "z", "magnitude"}:
            raise ValueError(
                "Line-profile component must be None, x, y, z, or magnitude."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "line-profile",
            "name": self.name,
            "field": self.field,
            "start": list(self.start),
            "end": list(self.end),
            "samples": self.samples,
            "component": self.component,
            "camera": None,
            "export": None,
        }


ViewRecipe = SliceView | ContourView | StreamlineView | LineProfile


@dataclass(frozen=True, slots=True)
class RenderLayout:
    """Arrange named render recipes into one reproducible engineering overview."""

    name: str
    views: tuple[str, ...]
    columns: int = 2
    export: ViewExport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _view_name(self.name))
        selected = _canonical_names(tuple(self.views), label="Layout views")
        if not 2 <= len(selected) <= 9:
            raise ValueError("Render layouts require between 2 and 9 named views.")
        object.__setattr__(self, "views", selected)
        columns = integer_at_least(
            self.columns, name="Render layout columns", minimum=1
        )
        if columns > min(4, len(selected)):
            raise ValueError(
                "Render layout columns cannot exceed four or the number of views."
            )
        object.__setattr__(self, "columns", columns)
        if self.export is not None and not isinstance(self.export, ViewExport):
            raise TypeError("Render layout export must be an AgentCFD ViewExport.")

    @property
    def rows(self) -> int:
        return math.ceil(len(self.views) / self.columns)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "render-layout",
            "name": self.name,
            "views": list(self.views),
            "columns": self.columns,
            "rows": self.rows,
            "export": None if self.export is None else self.export.to_dict(),
        }


def _validate_view_presentation(
    camera: ViewCamera | None,
    export: ViewExport | None,
) -> None:
    if camera is not None and not isinstance(camera, ViewCamera):
        raise TypeError("View camera must be an AgentCFD ViewCamera.")
    if export is not None and not isinstance(export, ViewExport):
        raise TypeError("View export must be an AgentCFD ViewExport.")


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
    criteria: tuple[QuantityCriterion, ...] = ()
    views: tuple[ViewRecipe, ...] = ()
    layouts: tuple[RenderLayout, ...] = ()

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
            not isinstance(
                item,
                (
                    PointProbe,
                    SurfaceReport,
                    PressureLossReport,
                    FlowUniformityReport,
                    FlowDistributionReport,
                    ForceReport,
                ),
            )
            for item in selected_reports
        ):
            raise TypeError("Output reports must be AgentCFD report definitions.")
        if len({item.name for item in selected_reports}) != len(selected_reports):
            raise ValueError("Output report names must be unique.")
        object.__setattr__(self, "reports", selected_reports)
        selected_criteria = tuple(self.criteria)
        if any(not isinstance(item, QuantityCriterion) for item in selected_criteria):
            raise TypeError("Output criteria must be AgentCFD quantity criteria.")
        if len({item.name for item in selected_criteria}) != len(selected_criteria):
            raise ValueError("Output criterion names must be unique.")
        report_names = {item.name for item in selected_reports}
        for criterion in selected_criteria:
            parts = criterion.quantity.split(".")
            if len(parts) >= 3 and parts[0] == "report" and parts[1] not in report_names:
                raise ValueError(
                    f"Output criterion {criterion.name!r} references unknown report "
                    f"{parts[1]!r}."
                )
        object.__setattr__(self, "criteria", selected_criteria)
        selected_views = tuple(self.views)
        if any(
            not isinstance(item, (SliceView, ContourView, StreamlineView, LineProfile))
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
        selected_layouts = tuple(self.layouts)
        if any(not isinstance(item, RenderLayout) for item in selected_layouts):
            raise TypeError("Output layouts must be AgentCFD render layouts.")
        if len({item.name for item in selected_layouts}) != len(selected_layouts):
            raise ValueError("Output layout names must be unique.")
        if {item.name for item in selected_layouts} & {
            item.name for item in selected_views
        }:
            raise ValueError("Output layout and view names must not collide.")
        view_index = {item.name: item for item in selected_views}
        for layout in selected_layouts:
            missing = sorted(set(layout.views) - set(view_index))
            if missing:
                raise ValueError(
                    f"Render layout {layout.name!r} references unknown views: "
                    + ", ".join(missing)
                    + "."
                )
            profiles = [view_index[name] for name in layout.views]
            if any(isinstance(item, LineProfile) for item in profiles):
                raise ValueError(
                    f"Render layout {layout.name!r} accepts slice, contour, and "
                    "streamline views; line profiles remain compact chart outputs."
                )
        object.__setattr__(self, "layouts", selected_layouts)

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "fields": list(self.fields),
            "histories": list(self.histories),
            "portable_profile": self.portable_profile,
            "portable_formats": list(self.portable_formats),
            "frames": self.frames.to_dict(),
            "checkpoints": self.checkpoints.to_dict(),
            "storage": self.storage.to_dict(),
            "reports": [item.to_dict() for item in self.reports],
            "views": [item.to_dict() for item in self.views],
            "layouts": [item.to_dict() for item in self.layouts],
        }
        if self.criteria:
            record["criteria"] = [item.to_dict() for item in self.criteria]
        return record


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


def pressure_loss(
    name: str,
    *,
    inlet: str,
    outlet: str,
    reference_area: float | None = None,
    every: int = 1,
) -> PressureLossReport:
    """Request compact total-pressure loss and inlet-bulk loss coefficient."""

    return PressureLossReport(
        name=name,
        inlet=inlet,
        outlet=outlet,
        reference_area=reference_area,
        every=every,
    )


def flow_uniformity(
    name: str,
    *,
    region: str,
    every: int = 1,
) -> FlowUniformityReport:
    """Request compact outlet/section velocity distribution metrics."""

    return FlowUniformityReport(name=name, region=region, every=every)


def flow_distribution(
    name: str,
    *,
    inlet: str,
    outlets: tuple[str, ...],
    targets: Mapping[str, float] | None = None,
    every: int = 1,
) -> FlowDistributionReport:
    """Request compact branch flows, fractions, balance, and target errors."""

    if targets is not None and not isinstance(targets, Mapping):
        raise TypeError("Flow-distribution targets must be a mapping by outlet name.")
    target_fractions = () if targets is None else tuple(targets.items())
    return FlowDistributionReport(
        name=name,
        inlet=inlet,
        outlets=outlets,
        target_fractions=target_fractions,
        every=every,
    )


def require(
    name: str,
    *,
    quantity: str,
    unit: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> QuantityCriterion:
    """Declare an explicit design requirement on a canonical scalar result."""

    return QuantityCriterion(
        name=name,
        quantity=quantity,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
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
    component: str | None = None,
    camera: ViewCamera | None = None,
    export: ViewExport | None = None,
) -> SliceView:
    return SliceView(
        name=name,
        field=field,
        origin=origin,
        normal=normal,
        component=component,
        camera=camera,
        export=export,
    )


def contour_view(
    name: str,
    *,
    field: str,
    values: tuple[float, ...],
    camera: ViewCamera | None = None,
    export: ViewExport | None = None,
) -> ContourView:
    return ContourView(
        name=name,
        field=field,
        values=values,
        camera=camera,
        export=export,
    )


def streamline_view(
    name: str,
    *,
    seed_start: tuple[float, float, float],
    seed_end: tuple[float, float, float],
    seeds: int = 40,
    direction: str = "both",
    field: str = "fluid.velocity",
    camera: ViewCamera | None = None,
    export: ViewExport | None = None,
) -> StreamlineView:
    return StreamlineView(
        name=name,
        field=field,
        seed_start=seed_start,
        seed_end=seed_end,
        seeds=seeds,
        direction=direction,
        camera=camera,
        export=export,
    )


def line_profile(
    name: str,
    *,
    field: str,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    samples: int = 101,
    component: str | None = None,
) -> LineProfile:
    return LineProfile(
        name=name,
        field=field,
        start=start,
        end=end,
        samples=samples,
        component=component,
    )


def camera(
    *,
    position: tuple[float, float, float],
    focal_point: tuple[float, float, float],
    view_up: tuple[float, float, float] = (0.0, 1.0, 0.0),
    parallel_scale: float | None = None,
) -> ViewCamera:
    return ViewCamera(
        position=position,
        focal_point=focal_point,
        view_up=view_up,
        parallel_scale=parallel_scale,
    )


def render(
    *,
    size: tuple[int, int] = (1280, 720),
    screenshot: bool = True,
    animation: str | None = None,
    frame_rate: int = 24,
    transparent_background: bool = False,
) -> ViewExport:
    return ViewExport(
        size=size,
        screenshot=screenshot,
        animation=animation,
        frame_rate=frame_rate,
        transparent_background=transparent_background,
    )


def render_layout(
    name: str,
    *,
    views: tuple[str, ...],
    columns: int = 2,
    export: ViewExport | None = None,
) -> RenderLayout:
    """Compose existing render recipes without another field payload."""

    return RenderLayout(
        name=name,
        views=views,
        columns=columns,
        export=export,
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
    criteria: tuple[QuantityCriterion, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
    layouts: tuple[RenderLayout, ...] = (),
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
        criteria=criteria,
        views=views,
        layouts=layouts,
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
    criteria: tuple[QuantityCriterion, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
    layouts: tuple[RenderLayout, ...] = (),
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
        criteria=criteria,
        views=views,
        layouts=layouts,
    )


def turbulent_internal_flow(
    *,
    turbulence_model: str = "k-omega-sst",
    portable_profile: str = "visualization",
    portable_formats: tuple[str, ...] = ("xdmf",),
    reports: tuple[Report, ...] = (),
    criteria: tuple[QuantityCriterion, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
    layouts: tuple[RenderLayout, ...] = (),
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
        criteria=criteria,
        views=views,
        layouts=layouts,
    )


def thermal_internal_flow(
    *,
    turbulence_model: str | None = None,
    portable_profile: str = "visualization",
    portable_formats: tuple[str, ...] = ("xdmf",),
    storage_policy: StoragePolicy | None = None,
    reports: tuple[Report, ...] = (),
    criteria: tuple[QuantityCriterion, ...] = (),
    views: tuple[ViewRecipe, ...] = (),
    layouts: tuple[RenderLayout, ...] = (),
) -> OutputRequest:
    """Request flow plus absolute temperature for an energy study."""

    base = (
        standard(
            portable_profile=portable_profile,
            portable_formats=portable_formats,
            storage_policy=storage_policy,
            reports=reports,
            criteria=criteria,
            views=views,
            layouts=layouts,
        )
        if turbulence_model is None
        else turbulent_internal_flow(
            turbulence_model=turbulence_model,
            portable_profile=portable_profile,
            portable_formats=portable_formats,
            reports=reports,
            criteria=criteria,
            views=views,
            layouts=layouts,
        )
    )
    return OutputRequest(
        fields=(*base.fields, "thermal.temperature"),
        histories=(*base.histories, "thermal.energy_balance"),
        portable_profile=base.portable_profile,
        portable_formats=base.portable_formats,
        frames=base.frames,
        checkpoints=base.checkpoints,
        storage=storage_policy or base.storage,
        reports=base.reports,
        criteria=base.criteria,
        views=base.views,
        layouts=base.layouts,
    )


__all__ = [
    "Checkpoints",
    "ContourView",
    "FieldFrames",
    "FlowUniformityReport",
    "FlowDistributionReport",
    "ForceReport",
    "LineProfile",
    "OutputRequest",
    "PointProbe",
    "PressureLossReport",
    "QuantityCriterion",
    "RenderLayout",
    "Report",
    "StoragePolicy",
    "StreamlineView",
    "SurfaceReport",
    "SliceView",
    "ViewRecipe",
    "ViewCamera",
    "ViewExport",
    "animation",
    "camera",
    "checkpoints",
    "contour_view",
    "force_report",
    "flow_uniformity",
    "flow_distribution",
    "parse_storage_size",
    "probe",
    "pressure_loss",
    "require",
    "render",
    "render_layout",
    "slice_view",
    "standard",
    "storage",
    "surface_report",
    "streamline_view",
    "line_profile",
    "turbulent_internal_flow",
    "thermal_internal_flow",
]
