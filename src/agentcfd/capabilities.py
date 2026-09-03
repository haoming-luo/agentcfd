"""Machine-readable capability boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    maturity: str
    scope: str
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_CAPABILITIES = (
    Capability(
        name="workflow.engineering-model",
        maturity="release",
        scope="Typed study, domain, fluid, boundary, procedure, output, and result lifecycle.",
        evidence=("public API tests", "JSON round-trip tests"),
        limitations=("The first release contains one executable reference provider.",),
    ),
    Capability(
        name="reference.hagen-poiseuille",
        maturity="release",
        scope="Steady fully developed incompressible Newtonian laminar flow in a circular pipe.",
        evidence=("closed-form Hagen-Poiseuille relation", "Darcy-Weisbach identity", "mass balance"),
        limitations=("Re < 2300", "constant properties", "straight circular pipe"),
    ),
    Capability(
        name="interoperability.agentcae-exchange",
        maturity="experimental",
        scope="Provider-neutral field exchange contract for AgentCFD, AgentFEM, learning, and coupling tools.",
        evidence=("schema and direction validation tests",),
        limitations=("No conservative mesh mapper or coupled time integrator is released yet.",),
    ),
    Capability(
        name="provider.openfoam",
        maturity="experimental",
        scope="Deterministic external OpenFOAM case lowering and bounded process execution.",
        evidence=("golden case-generation tests", "runtime discovery", "external-process boundary"),
        limitations=(
            "Only steady incompressible isothermal laminar flow in a smooth circular pipe is lowered.",
            "Mesh-field result recovery and scientific acceptance are not released.",
        ),
    ),
    Capability(
        name="openfoam.steady-laminar-circular-pipe",
        maturity="experimental",
        scope="OpenFOAM simpleFoam case generation for a full three-dimensional O-grid circular pipe.",
        evidence=("deterministic content hashes", "generated-case contract tests"),
        limitations=(
            "Targets installations exposing blockMesh and simpleFoam.",
            "A generated or completed case is not yet an accepted CFD result.",
        ),
    ),
)


def all() -> tuple[Capability, ...]:
    return _CAPABILITIES


def as_dict() -> dict[str, object]:
    return {
        "schema": "agentcfd.capabilities/0.1",
        "capabilities": [item.to_dict() for item in _CAPABILITIES],
    }
