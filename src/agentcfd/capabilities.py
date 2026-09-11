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
        record = asdict(self)
        record["evidence"] = list(self.evidence)
        record["limitations"] = list(self.limitations)
        return record


_CAPABILITIES = (
    Capability(
        name="workflow.engineering-model",
        maturity="release",
        scope="Typed study, domain, fluid, boundary, procedure, output, and result lifecycle.",
        evidence=("public API tests", "JSON round-trip tests"),
        limitations=(
            "Provider support remains narrower than the solver-neutral public vocabulary.",
        ),
    ),
    Capability(
        name="workflow.common-cfd-intent",
        maturity="experimental",
        scope=(
            "Named circular-pipe and rectangular-channel regions, attached baffles, "
            "role-checked boundaries, initialization, mesh intent, compact reports, "
            "and typed result discovery."
        ),
        evidence=(
            "common-workflow public API tests",
            "executable bottom-baffle project plan",
            "OpenCFD v2606 mesh and short-run integration evidence",
        ),
        limitations=(
            "The first channel slice is limited to one bottom-attached baffle and low-Re laminar flow.",
            "Turbulent channel flow and vector surface reductions are pending.",
        ),
    ),
    Capability(
        name="workflow.project-lifecycle",
        maturity="experimental",
        scope=(
            "One init, check, inspectable-plan, run, and inspect lifecycle shared "
            "by people, agents, CLI automation, and future GUIs."
        ),
        evidence=(
            "content-addressed plan and run lifecycle tests",
            "reference-provider end-to-end project execution",
            "OpenFOAM project execution with automatic portable fields",
        ),
        limitations=(
            "Templates currently cover pipe, one bottom-baffle channel, and the bounded imported internal-flow slice.",
            "Project creation requests do not replace case.py as the continuing scientific source.",
        ),
    ),
    Capability(
        name="workflow.thermal-internal-flow-intent",
        maturity="experimental",
        scope=(
            "Solver-neutral energy-study intent with inlet temperature, explicit "
            "adiabatic/fixed-temperature/signed-heat-flux walls, complete constant "
            "properties, and canonical temperature-field output."
        ),
        evidence=(
            "thermal boundary serialization and finite-input tests",
            "energy-study property, inlet, wall, and output completeness gates",
            "CoolProp state to constant-property fluid bridge",
            "dimensionless and first-law thermal-flow planning record",
        ),
        limitations=(
            "The first OpenFOAM lowering is limited to one-way-coupled constant-property laminar heat transport in a circular pipe.",
            "Phase change, conjugate heat transfer, radiation, species, and combustion remain unsupported intent.",
        ),
    ),
    Capability(
        name="geometry.imported-surface-inspection",
        maturity="experimental",
        scope=(
            "Read-only STL/OBJ format, SI-unit, bounds, region, degeneracy, "
            "watertightness, manifoldness, orientation, named-patch geometry, "
            "inlet-vector direction, and memory-guard preflight."
        ),
        evidence=(
            "closed and open ASCII STL topology tests",
            "binary STL and OBJ polygon tests",
            "explicit internal-flow boundary-role mapping tests",
            "named-patch SI area, centroid, area-vector, and normal tests",
            "pre-solve signed inlet-vector direction tests",
            "fail-closed explicit acceptance of complete name-role suggestions",
            "installed geometry-inspection JSON contract",
        ),
        limitations=(
            "STEP/IGES are recognized but require controlled tessellation.",
            "The standard-library check does not replace OpenFOAM surfaceCheck before meshing.",
        ),
    ),
    Capability(
        name="openfoam.imported-surface-mesh",
        maturity="experimental",
        scope=(
            "Content-addressed STL/OBJ volume meshing through a padded blockMesh "
            "background, native geometry dry-run, snappyHexMesh, and checkMesh gates."
        ),
        evidence=(
            "deterministic mesh-plan and prepared-case tests",
            "explicit locationInMesh and maxGlobalCells lowering",
            "native command, cell-budget, non-orthogonality, skewness, and aspect-ratio gates",
            "6,400-cell accepted OpenCFD v2606 imported-duct integration mesh",
        ),
        limitations=(
            "Requires a user-confirmed interior point and OpenFOAM-compatible region names.",
            "The first slice supports no prism layers and only inlet/outlet/wall/symmetry/empty roles.",
            "Real-geometry validation beyond the integration duct is pending.",
        ),
    ),
    Capability(
        name="openfoam.steady-laminar-imported-surface",
        maturity="experimental",
        scope=(
            "Steady incompressible isothermal laminar simpleFoam solution on a "
            "content-addressed, role-confirmed imported fluid volume with compact "
            "point, pressure-surface, wall-force, total-pressure-loss coefficient, "
            "and bidirectional flow reports."
        ),
        evidence=(
            "explicit Cartesian velocity, constant-density mass flow, or total-pressure inlet lowering tests",
            "documented total-pressure inlet/static-pressure outlet OpenFOAM combination",
            "recovered mass-flow target error as a mandatory acceptance gate",
            "signed inlet/outlet flow direction plus mass-conservation gates",
            "solver, SIMPLE convergence, mesh, runtime, and output gates",
            "accepted 6,400-cell OpenCFD v2606 duct run with verified XDMF/H5",
            "accepted pressure-driven OpenCFD v2606 run with recovered mass and volume flow",
            "real compact-report recovery with no additional full-field frames",
            "real mass-flow-averaged total-pressure-loss report with post-mesh inlet area",
        ),
        limitations=(
            "Exactly one velocity, mass-flow, or total-pressure inlet and one static-pressure outlet are supported.",
            "No turbulence, heat, compressibility, reactions, prism layers, or vector surface reductions yet.",
            "Acceptance is workflow/numerical evidence and does not claim physical validation.",
        ),
    ),
    Capability(
        name="openfoam.steady-rans-imported-surface",
        maturity="experimental",
        scope=(
            "Steady incompressible isothermal k-omega SST flow on a "
            "content-addressed imported fluid volume with explicit Cartesian "
            "velocity, turbulence intensity, length scale, and blended wall treatment."
        ),
        evidence=(
            "deterministic arbitrary-patch turbulence-field lowering tests",
            "explicit vector inlet and turbulence-assumption identity",
            "runtime y-plus range, convergence, conservation, mesh, and output gates",
        ),
        limitations=(
            "Only k-omega SST and blended wall functions are currently supported.",
            "Prism-layer automation, automatic y-plus correction, grid sensitivity, and physical validation remain open gates.",
            "Heat, compressibility, reactions, rough walls, and vector surface reductions remain unsupported.",
        ),
    ),
    Capability(
        name="reference.hagen-poiseuille",
        maturity="release",
        scope="Steady fully developed incompressible Newtonian laminar flow in a circular pipe.",
        evidence=(
            "closed-form Hagen-Poiseuille relation",
            "Darcy-Weisbach identity",
            "mass balance",
        ),
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
        name="engineering.gas-screening",
        maturity="experimental",
        scope=(
            "Ideal-gas state, sound speed, Mach number, and explicit "
            "incompressible-model screening."
        ),
        evidence=(
            "ideal-gas identity tests",
            "low-Mach threshold decision tests",
            "NASA low-speed compressibility guidance",
        ),
        limitations=(
            "The Mach threshold is a preliminary model-selection screen, not validation.",
            "Non-ideal steam and large thermal or composition changes need dedicated models.",
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
        limitations=(
            "No conservative mesh mapper or coupled time integrator is released yet.",
        ),
    ),
    Capability(
        name="interoperability.portable-field-bundle",
        maturity="experimental",
        scope=(
            "Canonical fixed-mesh time series in XDMF/HDF5 plus pickle-free NPZ "
            "for visualization, AgentFEM exchange, and learned workflows."
        ),
        evidence=(
            "XDMF/HDF5 and NPZ cross-format round-trip tests",
            "field-unit, association, axis, and artifact-hash contract tests",
            "16-frame OpenCFD v2606 turbulent-pipe export",
        ),
        limitations=(
            "The first exporter consumes foamToVTK fixed-mesh volume fields.",
            "Moving meshes, decomposed parallel fields, and conservative CFD-to-FEM mapping remain open.",
        ),
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
        name="validation.single-observable-uncertainty",
        maturity="experimental",
        scope=(
            "Solver-neutral comparison of one simulated observable with reference "
            "data and combined validation uncertainty."
        ),
        evidence=(
            "root-sum-square uncertainty tests",
            "coverage-factor acceptance tests",
            "installed JSON contract",
        ),
        limitations=(
            "Input uncertainty components must be defensible standard uncertainties.",
            "Passing one observable does not validate a model outside that comparison point.",
        ),
    ),
    Capability(
        name="verification.component-loss-baseline",
        maturity="experimental",
        scope=(
            "Solver-neutral local loss coefficient from two accepted total-pressure-loss "
            "reports with an explicitly equivalent straight-run baseline."
        ),
        evidence=(
            "unit and coefficient-identity tests",
            "provider/runtime and operating-reference compatibility gates",
            "content-addressed two-result CLI evidence contract",
        ),
        limitations=(
            "The caller must confirm equivalent distributed length, cross-section, measurement planes, walls, roughness, fluid, and operating point.",
            "Baseline subtraction does not establish experimental validation or numerical uncertainty.",
        ),
    ),
    Capability(
        name="verification.turbulent-model-reynolds-sweep",
        maturity="experimental",
        scope=(
            "Source-hashed multi-Re smooth-pipe comparison of SST/Spalding and "
            "standard k-epsilon/nutk with pairwise-identical meshes and adaptive "
            "near-wall spacing."
        ),
        evidence=(
            "installed point-study and sweep JSON contracts",
            "content-addressed prepare/run campaign with point-level resume",
            "correlation-based preflight followed by solved y-plus gates",
            "four-point OpenCFD v2606 matrix over Re 49,810--498,104",
            "explicit model-ranking transition detection",
        ),
        limitations=(
            "Smooth-Colebrook agreement is a model-form diagnostic, not experimental validation.",
            "The two high-Re best-model differences remain above the 2% point target.",
            "No single turbulence model is promoted across the sampled range.",
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
            "OpenCFD v2606 k-omega SST and k-epsilon precursor execution evidence",
        ),
        limitations=(
            "Steady imported-volume flow supports explicit laminar and experimental k-omega SST slices.",
            "Downstream turbulent pipes remain limited to k-omega SST; k-epsilon is precursor-only.",
            "OpenCFD v2606 is the currently exercised runtime dialect.",
            "Each laminar or turbulent slice has its own validation and grid-evidence gate.",
        ),
    ),
    Capability(
        name="openfoam.transient-laminar-baffled-channel",
        maturity="experimental",
        scope=(
            "OpenCFD v2606 transient incompressible laminar flow through a rectangular "
            "channel containing one wall-attached baffle."
        ),
        evidence=(
            "deterministic five-block conformal hexahedral case generation",
            "23,880-cell Mesh OK OpenCFD v2606 integration run",
            "potentialFoam and pimpleFoam process completion",
            "mass-balance, pressure-drop, probe, surface-report, force, and field recovery",
            "separated XDMF field-frame cadence and verified rolling restart archive",
            "adaptive time-step bound and Courant-control evidence",
            "accepted 0.002/0.001-second pairwise time-step sensitivity at a matched 0.5-second startup state",
        ),
        limitations=(
            "Hydraulic inlet Reynolds number must be below 2300.",
            "Only one bottom-attached baffle, structured uniform sizing, and constant Newtonian properties are supported.",
            "The two-level startup sensitivity screen cannot establish temporal order, uncertainty, wake stationarity, or physical validation.",
            "Restart archives are published after successful completion and are not yet crash-safe during a running solve.",
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
    Capability(
        name="openfoam.steady-rans-smooth-circular-pipe",
        maturity="experimental",
        scope=(
            "OpenCFD v2606 steady incompressible smooth circular-pipe flow "
            "with the k-omega SST RANS model."
        ),
        evidence=(
            "deterministic turbulence-field and wall-treatment lowering tests",
            "explicit inlet intensity and length-scale identity",
            "runtime y-plus, mass-balance, pressure-loss, and friction-factor checks",
            "verified same-resolution periodic-precursor mapping at Re 99,621",
            "multi-Re model sensitivity with adaptive wall spacing",
        ),
        limitations=(
            "Only a flow-rate-constrained mean-velocity inlet is supported.",
            "The first slice uses automatic blended wall treatment on a smooth wall.",
            "Physical accuracy remains diagnostic until grid and Reynolds-range validation pass.",
        ),
    ),
    Capability(
        name="openfoam.steady-laminar-heated-circular-pipe",
        maturity="experimental",
        scope=(
            "OpenCFD v2606 steady incompressible laminar circular-pipe flow "
            "with one-way constant-property temperature transport and prescribed "
            "wall heat flux."
        ),
        evidence=(
            "deterministic T field, scalarTransport, diffusivity, and heat-flux lowering tests",
            "mass-flow-weighted inlet and outlet bulk-temperature recovery",
            "first-law advected-enthalpy versus wall-heat acceptance gate",
            "accepted 102,400-cell OpenCFD v2606 run with 0.052% energy imbalance and 0.78% pressure error",
        ),
        limitations=(
            "Velocity and properties do not respond to temperature; buoyancy and viscous heating are excluded.",
            "Only a non-zero prescribed heat flux on the single circular-pipe wall is lowered.",
            "One accepted grid is recorded; formal thermal grid-convergence evidence remains pending.",
        ),
    ),
    Capability(
        name="openfoam.periodic-k-omega-sst-circular-pipe-precursor",
        maturity="experimental",
        scope=(
            "Periodic smooth circular-pipe k-omega SST flow driven to a target "
            "bulk velocity for reusable developed-inlet fields."
        ),
        evidence=(
            "deterministic cyclic O-grid and meanVelocityForce lowering tests",
            "content-addressed U, k, omega, nut, mesh, and container identities",
            "OpenCFD v2606 real execution with pressure-gradient, residual, y-plus, and friction checks",
        ),
        limitations=(
            "Wall-function and model screens remain benchmark-specific, not global defaults.",
            "A geometrically similar turbulent grid certificate remains an open gate.",
            "OpenFOAM boundaryFoam is not used because its implementation requires parallel planar walls.",
        ),
    ),
    Capability(
        name="openfoam.periodic-k-epsilon-circular-pipe-precursor",
        maturity="experimental",
        scope=(
            "Periodic smooth circular-pipe standard k-epsilon flow driven to a "
            "target bulk velocity for model sensitivity and developed-field evidence."
        ),
        evidence=(
            "model-specific epsilon and wall-function lowering tests",
            "content-addressed prepare/run and source-hashed comparison contracts",
            "accepted OpenCFD v2606 c16 execution from Re 49,810 to 498,104",
            "pairwise-identical-mesh SST versus k-epsilon sensitivity evidence",
        ),
        limitations=(
            "Correlation differences exceed 2% at the two high-Re matrix points.",
            "Downstream k-epsilon field mapping is not supported.",
            "No general turbulence-model default is promoted from the correlation screen.",
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
