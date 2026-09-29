"""Pure provider-planning decisions shared by project inspection and execution."""

from __future__ import annotations

from typing import Mapping

from .geometry import ImportedSurface, RectangularChannel
from .model import Step
from .providers.registry import variant as provider_variant


def decision_terms(
    step: Step,
    *,
    family: str,
    compatible: bool,
    effective_options: Mapping[str, object],
) -> dict[str, object]:
    """Resolve auditable provider decisions without filesystem or process work."""

    study = step.model.study
    if isinstance(step.model.domain, ImportedSurface):
        required_capability = (
            "openfoam.steady-laminar-imported-surface"
            if study.laminar
            else "openfoam.steady-rans-imported-surface"
        )
    elif family == "reference":
        required_capability = "reference.hagen-poiseuille"
    elif isinstance(step.model.domain, RectangularChannel):
        required_capability = "openfoam.transient-laminar-baffled-channel"
    else:
        required_capability = (
            "openfoam.steady-laminar-heated-circular-pipe"
            if study.energy
            else "openfoam.steady-laminar-circular-pipe"
            if study.laminar
            else "openfoam.steady-rans-smooth-circular-pipe"
        )

    mesh_strategy = (
        f"intent:{step.mesh.method}"
        if step.mesh is not None
        else "structured-circular-pipe-o-grid"
        if family == "openfoam"
        else "analytical"
    )
    solver = (
        "unresolved-provider-lowering"
        if not compatible
        else "simpleFoam + scalarTransport(T)"
        if family == "openfoam" and study.energy
        else "pimpleFoam"
        if family == "openfoam" and not study.steady
        else "simpleFoam"
        if family == "openfoam"
        else "Hagen-Poiseuille"
    )
    return {
        "provider_resolution": {
            "family": family,
            "variant": provider_variant(family, step=step),
            "effective_options": dict(effective_options),
        },
        "required_capability": required_capability,
        "mesh_strategy": mesh_strategy,
        "solver": solver,
    }


__all__ = ["decision_terms"]
