"""Output requests use canonical scientific field names."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class OutputRequest:
    fields: tuple[str, ...]
    histories: tuple[str, ...]
    portable_profile: str = "visualization"

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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def standard(*, portable_profile: str = "visualization") -> OutputRequest:
    return OutputRequest(
        fields=("fluid.velocity", "fluid.pressure"),
        histories=("flow.mass_balance", "flow.pressure_drop"),
        portable_profile=portable_profile,
    )


def turbulent_internal_flow(
    *,
    turbulence_model: str = "k-omega-sst",
    portable_profile: str = "visualization",
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
    )
