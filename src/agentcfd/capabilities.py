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
        name="engineering.pipe-loss",
        maturity="experimental",
        scope="Hydraulic diameter, Reynolds number, Darcy friction, and major/minor pressure loss.",
        evidence=(
            "Hagen--Poiseuille identity test",
            "Colebrook--White equation residual test",
            "transition-regime rejection test",
        ),
        limitations=(
            "Single-phase incompressible engineering correlations only.",
            "Reynolds numbers from 2300 up to 4000 require an explicit regime-specific model.",
        ),
    ),
    Capability(
        name="properties.coolprop",
        maturity="experimental",
        scope="Optional pressure-temperature thermophysical states, including IF97 water/steam.",
        evidence=(
            "lazy optional-provider tests",
            "SI state-record contract tests",
            "upstream CoolProp pressure-temperature and IF97 documentation",
            "CoolProp 8.0.0 installed-runtime IF97 evidence at 101325 Pa and 500 K",
        ),
        limitations=(
            "Property evaluation is not a CFD solver.",
            "The caller must select a valid fluid backend and state point.",
            "Two-phase and near-saturation CFD remain unsupported.",
        ),
    ),
    Capability(
        name="interoperability.agentcae-exchange",
        maturity="experimental",
        scope="Provider-neutral field exchange contract for AgentCFD, AgentFEM, learning, and coupling tools.",
        evidence=("schema and direction validation tests",),
        limitations=("No conservative mesh mapper or coupled time integrator is released yet.",),
    ),
    Capability(
        name="verification.grid-convergence-index",
        maturity="experimental",
        scope="Solver-neutral three-grid Richardson extrapolation and Grid Convergence Index.",
        evidence=(
            "synthetic equal-ratio and unequal-ratio order-recovery tests",
            "fail-closed oscillatory-sequence tests",
            "same-model converged-result ingestion and source-hash tests",
        ),
        limitations=(
            "Requires exactly three monotonically converging scalar solutions.",
            "Users must establish that the grids are geometrically similar and in the asymptotic range.",
        ),
    ),
    Capability(
        name="provider.openfoam",
        maturity="experimental",
        scope="Deterministic external OpenFOAM case lowering and bounded process execution.",
        evidence=(
            "golden case-generation tests",
            "runtime discovery",
            "external-process boundary",
            "automatic patch-history and mesh-quality recovery tests",
        ),
        limitations=(
            "Only steady incompressible isothermal laminar flow in a smooth circular pipe is lowered.",
            "OpenCFD v2606 is the currently exercised runtime dialect.",
            "Mesh-convergence evidence remains a promotion gate.",
        ),
    ),
    Capability(
        name="openfoam.steady-laminar-circular-pipe",
        maturity="experimental",
        scope="OpenFOAM simpleFoam case generation for a full three-dimensional O-grid circular pipe.",
        evidence=(
            "deterministic content hashes",
            "generated-case contract tests",
            "checkMesh, flow-balance, pressure-drop, convergence, and field-artifact recovery",
        ),
        limitations=(
            "Targets installations exposing blockMesh, checkMesh, and simpleFoam.",
            "The fully developed expression inlet is currently OpenCFD-dialect specific.",
            "Scientific acceptance also requires every per-run validation check to pass.",
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
