"""Deterministic discovery and resolution for AgentCFD provider families."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TYPE_CHECKING

from .base import ProviderOption, ProviderOptionContract

if TYPE_CHECKING:
    from ..model import Step


@dataclass(frozen=True, slots=True)
class ProviderFamily:
    """Public family record independent of host runtime availability."""

    name: str
    display_name: str
    license: str
    execution_boundary: str
    variants: tuple[str, ...]
    option_contract: ProviderOptionContract

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "license": self.license,
            "execution_boundary": self.execution_boundary,
            "variants": list(self.variants),
            "option_contract": self.option_contract.to_dict(),
        }


_OPENFOAM_OPTIONS = ProviderOptionContract(
    (
        ProviderOption(
            "container_image",
            "string",
            "execution",
            "Immutable or user-selected container image for the external runtime.",
        ),
        ProviderOption(
            "cross_section_cells",
            "integer",
            "mesh",
            "Structured circular-pipe cross-section resolution.",
        ),
        ProviderOption(
            "axial_cells",
            "integer",
            "mesh",
            "Structured circular-pipe axial resolution.",
        ),
        ProviderOption(
            "nominal_wall_cell_fraction",
            "number",
            "mesh",
            "Nominal near-wall cell height as a fraction of pipe radius.",
        ),
        ProviderOption(
            "export_fields",
            "boolean",
            "output",
            "Publish selected portable XDMF/H5 fields after result recovery.",
        ),
        ProviderOption(
            "keep_workspace",
            "boolean",
            "retention",
            "Retain the generated provider workspace after a completed run.",
        ),
        ProviderOption(
            "timeout_seconds",
            "number",
            "execution",
            "Positive wall-time limit for each bounded external command.",
        ),
    )
)


_FAMILIES = (
    ProviderFamily(
        name="reference",
        display_name="Analytical reference",
        license="Apache-2.0",
        execution_boundary="in-process",
        variants=("hagen-poiseuille",),
        option_contract=ProviderOptionContract(),
    ),
    ProviderFamily(
        name="openfoam",
        display_name="OpenFOAM",
        license="GPL-3.0-or-later (external program)",
        execution_boundary="filesystem-and-subprocess",
        variants=("circular-pipe", "baffled-channel", "imported-internal-flow"),
        option_contract=_OPENFOAM_OPTIONS,
    ),
)


def all() -> tuple[ProviderFamily, ...]:
    return _FAMILIES


def ids() -> tuple[str, ...]:
    return tuple(provider.name for provider in _FAMILIES)


def get(name: str) -> ProviderFamily:
    selected = str(name).strip().lower()
    for provider in _FAMILIES:
        if provider.name == selected:
            return provider
    raise ValueError(f"Unknown provider {name!r}; expected one of {ids()!r}.")


def as_dict() -> dict[str, object]:
    return {
        "schema": "agentcfd.provider-catalog/0.1",
        "providers": [provider.to_dict() for provider in _FAMILIES],
    }


def validate_options(name: str, values: Mapping[str, object]) -> None:
    get(name).option_contract.validate(values, provider=name)


def variant(name: str, *, step: "Step | None" = None) -> str:
    """Return the deterministic provider variant selected for one Step."""

    selected = get(name)
    if selected.name == "reference":
        return "hagen-poiseuille"

    from ..geometry import ImportedSurface, RectangularChannel

    if step is not None and isinstance(step.model.domain, ImportedSurface):
        return "imported-internal-flow"
    if step is not None and isinstance(step.model.domain, RectangularChannel):
        return "baffled-channel"
    return "circular-pipe"


def resolve(
    name: str,
    *,
    step: "Step | None" = None,
    case_directory: str | Path | None = None,
    imported_source: str | Path | None = None,
    mesh_cache_directory: str | Path | None = None,
    container_image: str | None = None,
    options: Mapping[str, object] | None = None,
):
    """Resolve one built-in provider through the shared inspection/run path."""

    selected = get(name)
    settings = dict(options or {})
    selected.option_contract.validate(settings, provider=selected.name)
    selected_variant = variant(selected.name, step=step)
    if selected_variant == "hagen-poiseuille":
        from .reference import ReferencePipeProvider

        return ReferencePipeProvider()

    from .openfoam import OpenFOAMMeshControls, OpenFOAMProvider
    from .openfoam_channel import OpenFOAMChannelProvider
    from .openfoam_imported import OpenFOAMImportedProvider

    selected_image = container_image or settings.get("container_image")
    timeout_seconds = float(settings.get("timeout_seconds", 3600.0))
    if selected_variant == "imported-internal-flow":
        if imported_source is None:
            raise ValueError("Imported OpenFOAM resolution requires imported_source.")
        return OpenFOAMImportedProvider(
            source=imported_source,
            case_directory=case_directory,
            mesh_cache_directory=mesh_cache_directory,
            container_image=str(selected_image) if selected_image else None,
            timeout_seconds=timeout_seconds,
        )
    if selected_variant == "baffled-channel":
        return OpenFOAMChannelProvider(
            case_directory=case_directory,
            container_image=str(selected_image) if selected_image else None,
            timeout_seconds=timeout_seconds,
        )
    mesh = OpenFOAMMeshControls(
        cross_section_cells=settings.get("cross_section_cells", 8),
        axial_cells=settings.get("axial_cells"),
        nominal_wall_cell_fraction=settings.get("nominal_wall_cell_fraction"),
    )
    return OpenFOAMProvider(
        case_directory=case_directory,
        container_image=str(selected_image) if selected_image else None,
        mesh=mesh,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "ProviderFamily",
    "all",
    "as_dict",
    "get",
    "ids",
    "resolve",
    "validate_options",
    "variant",
]
