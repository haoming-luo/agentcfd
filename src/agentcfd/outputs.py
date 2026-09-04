"""Backend-neutral output intent for fields, histories, restart, and storage."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from ._validation import integer_at_least, positive_float


_BYTE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}


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
            raise ValueError("Storage on_exceed currently supports only fail-closed 'error'.")

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

    def __post_init__(self) -> None:
        for name in ("fields", "histories"):
            selected = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item.strip() for item in selected):
                raise ValueError(f"Output {name} must contain non-empty canonical names.")
            if len(set(selected)) != len(selected):
                raise ValueError(f"Output {name} must not contain duplicates.")
            object.__setattr__(self, name, selected)
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
            raise TypeError("Output checkpoints must be an AgentCFD Checkpoints policy.")
        if not isinstance(self.storage, StoragePolicy):
            raise TypeError("Output storage must be an AgentCFD StoragePolicy.")

    def to_dict(self) -> dict[str, object]:
        return {
            "fields": list(self.fields),
            "histories": list(self.histories),
            "portable_profile": self.portable_profile,
            "portable_formats": list(self.portable_formats),
            "frames": self.frames.to_dict(),
            "checkpoints": self.checkpoints.to_dict(),
            "storage": self.storage.to_dict(),
        }


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
    )


def turbulent_internal_flow(
    *,
    turbulence_model: str = "k-omega-sst",
    portable_profile: str = "visualization",
    portable_formats: tuple[str, ...] = ("xdmf",),
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
    )


__all__ = [
    "Checkpoints",
    "FieldFrames",
    "OutputRequest",
    "StoragePolicy",
    "animation",
    "checkpoints",
    "parse_storage_size",
    "standard",
    "storage",
    "turbulent_internal_flow",
]
