"""Machine-readable benchmark roadmap for evidence-gated CFD development."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    id: str
    stage: str
    physics: str
    observables: tuple[str, ...]
    source: str
    source_url: str
    status: str
    next_gate: str

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        record["observables"] = list(self.observables)
        return record


_CASES = (
    BenchmarkCase(
        id="laminar-fully-developed-pipe",
        stage="unit",
        physics="Steady incompressible Newtonian laminar circular-pipe flow.",
        observables=("volume flow", "pressure gradient", "wall shear"),
        source="Hagen--Poiseuille closed form and NASA GCI procedure",
        source_url="https://www.grc.nasa.gov/www/wind/valid/tutorial/spatconv.html",
        status="active",
        next_gate="Complete three-grid OpenFOAM GCI with pressure-drop error below 2%.",
    ),
    BenchmarkCase(
        id="laminar-driven-cavity",
        stage="unit",
        physics="Two-dimensional laminar incompressible recirculating flow.",
        observables=("centreline velocity", "primary vortex location"),
        source="NASA NPARC driven-cavity validation case",
        source_url="https://www.grc.nasa.gov/WWW/wind/valid/cavity/cavity.html",
        status="planned",
        next_gate="Freeze geometry, Reynolds numbers, reference profiles, and grid sequence.",
    ),
    BenchmarkCase(
        id="pitz-daily-diffuser",
        stage="benchmark",
        physics="Steady turbulent incompressible separated internal flow.",
        observables=("reattachment length", "wall pressure", "velocity profiles"),
        source="OpenFOAM v2606 Pitz--Daily quickstart and originating experiment",
        source_url="https://doc.openfoam.com/2606/quickstart/",
        status="planned",
        next_gate="Add RANS boundary semantics and turbulence-model sensitivity policy.",
    ),
    BenchmarkCase(
        id="fda-benchmark-nozzle",
        stage="benchmark",
        physics="Internal nozzle flow with acceleration, jet, and downstream recirculation.",
        observables=("pressure", "axial velocity", "wall shear"),
        source="US FDA benchmark CFD validation dataset",
        source_url="https://www.origin-cdrh-rst.fda.gov/benchmark-dataset-validating-computational-fluid-dynamic-cfd-simulation-blood-flow-through",
        status="planned",
        next_gate="Review dataset terms and freeze one laminar and one turbulent operating point.",
    ),
    BenchmarkCase(
        id="laminar-cylinder-re150",
        stage="benchmark",
        physics="Unsteady laminar vortex shedding around a circular cylinder.",
        observables=("mean drag", "lift amplitude", "Strouhal number"),
        source="NASA NPARC laminar-cylinder validation case",
        source_url="https://www.grc.nasa.gov/www/wind/valid/lamcyl/lam_cyl.html",
        status="planned",
        next_gate="Add transient procedure, force histories, and time-step convergence.",
    ),
    BenchmarkCase(
        id="single-phase-if97-steam-pipe",
        stage="industrial-unit",
        physics="Single-phase compressible steam transport with variable properties.",
        observables=("mass flow", "pressure loss", "enthalpy loss", "Mach number"),
        source="CoolProp IF97 industrial water/steam formulation",
        source_url="https://coolprop.org/fluid_properties/IF97.html",
        status="property-provider-active",
        next_gate="Add compressible energy solver lowering and a public experimental dataset.",
    ),
)


def all() -> tuple[BenchmarkCase, ...]:
    return _CASES


def as_dict() -> dict[str, object]:
    return {
        "schema": "agentcfd.benchmarks/0.1",
        "cases": [case.to_dict() for case in _CASES],
    }


__all__ = ["BenchmarkCase", "all", "as_dict"]
