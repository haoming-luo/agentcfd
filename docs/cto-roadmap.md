# AgentCFD CTO roadmap

## North star

AgentCFD is the readable, inspectable, AI-native engineering layer for
industrial CFD. Its north-star experience is:

> Starting from one engineering question and one readable `case.py`, produce an
> accepted result, compact decision reports, and standard field data without
> requiring the user or an agent to maintain an OpenFOAM case tree.

The first market is process, energy, equipment, building-services, and general
mechanical engineering. Internal flows, flow distribution, heat and steam come
before high-Mach aerospace breadth.

## Product architecture

Six durable planes evolve together:

1. **Engineering intent** — studies, domains or imported geometry, named
   regions, fluids, zones, boundaries, sources, initialization, procedures,
   reports, and output policies.
2. **Planning** — deterministic analysis identity, provider capability
   negotiation, mesh and storage estimates, explicit decisions, addressable
   issues, and repair guidance before execution.
3. **Provider lowering** — bounded translation into OpenFOAM or a later engine;
   every consumed input is recorded and every unsupported input fails closed.
4. **Execution** — local, container, WSL2, workstation, and cluster execution
   behind one lifecycle with interruption, restart, progress, and bounded
   backend storage.
5. **Results and trust** — canonical quantities, histories, XDMF/H5 fields,
   optional learning arrays, provenance, conservation, convergence,
   verification, and validation.
6. **Agent surface** — versioned JSON schemas, stable error codes, small
   inspectable plans, safe operations, project-local knowledge, and the same
   APIs used by humans and future GUIs.

An LLM never replaces deterministic meshing, solving, or acceptance. It helps
construct, inspect, repair, compare, and automate the same explicit assets.

## Non-negotiable technical decisions

- The Python core and mandatory dependencies remain permissively licensed;
  OpenFOAM stays an external GPL program across a file/process boundary.
- `case.py` is scientific source, `output/` is the current answer,
  `campaigns/` is explicit history, and `.agentcfd/` is disposable backend
  state.
- The public language is solver neutral, but not vague. Provider capability is
  negotiated against the complete `Step`; no setting may be silently dropped.
- XDMF/H5 is the standard field product. NPZ is opt-in for array and learning
  workflows. Compact reports are preferred over frequent full-field dumps.
- Parametric geometry serves fast canonical workflows; imported CAD/mesh serves
  real equipment. They converge on the same named-region contract.
- Numerical maturity is promoted per bounded problem family, never inferred
  from one attractive visualization or one solver exit code.
- AgentCFD will not fork an OpenFOAM solver until profiling proves that provider
  orchestration, meshing, I/O, and configuration cannot meet a product need.

## Solver portfolio decision

OpenFOAM remains the primary computational base because its application and
physics breadth matches industrial internal flow, heat, steam, combustion, and
multiphase sequencing. Analytical and reduced reference providers remain
first-class for instant checks and verification. SU2 is a later optional
external provider candidate for independent cross-solver evidence and
gradient-based design: its official workflow uses a separate mesh plus compact
configuration, provides incompressible Navier--Stokes/RANS, restart, multiphysics,
and Python-driven optimization, and is LGPL-2.1. It does not displace the M1--M5
OpenFOAM path or justify a second public modeling language. PyFluent is treated
as workflow/UX research, not as a mandatory proprietary dependency.

## Release train

### M0 — coherent foundation (present)

Available:

- project `init → check → plan → run → inspect` lifecycle;
- circular-pipe reference and bounded OpenFOAM laminar/RANS workflows;
- content-addressed plans, cases, meshes, results, and evidence;
- standard XDMF/H5 field bundles, optional NPZ, frame/checkpoint/storage policy;
- named channel/baffle intent, initialization, mesh intent, probes, surface and
  force reports;
- machine-readable model, analysis, result, capability, and exchange contracts.

The former exit debt—an executable channel/baffle provider—entered M1 in this
development cycle.

### M1 — executable separated internal flow (now)

Deliver one short rectangular channel with a bottom-attached baffle end to end.
Case lowering, mesh execution, transient execution, compact report recovery,
and project planning are now implemented; remaining promotion gates are noted
below.

- deterministic conformal multi-block hexahedral mesh;
- transient incompressible laminar `pimpleFoam` lowering;
- uniform and `potentialFoam` initialization;
- velocity inlet, pressure outlet, and no-slip walls;
- adaptive time-step/Courant controls;
- point probes, patch area reports, baffle forces, mass balance, residual and
  mesh-quality evidence;
- sparse full fields, dense compact histories, rolling restart, XDMF/H5, and
  automatic backend cleanup;
- a runnable project whose defaults fit a laptop and show separation and
  recirculation clearly.

Promotion gate: deterministic preparation, `Mesh OK`, bounded mass imbalance,
declared convergence/stability evidence, complete requested outputs, a
published field animation, and an installed-wheel end-to-end smoke test.

Current M1 implementation publishes the rolling restart archive after successful
completion, verifies its result trust, model identity, and every member before
resume, and keeps public XDMF frames independent from checkpoint cadence.
Crash-safe periodic publication remains an M3 long-run reliability item. A
two-level temporal sensitivity API is available now; the formal matched study
is still required before M1 numerical promotion.

### M2 — practical geometry and meshing

- external STL/OBJ and common neutral CAD/mesh ingestion behind optional extras;
- watertightness, scale, orientation, connected-component, and boundary-name
  audit (released for STL/OBJ; CAD tessellation remains optional/future);
- `snappyHexMesh` surface, volume, refinement, feature, and layer lowering;
- mesh preview, cell/storage/runtime estimate, quality repair actions, and
  content-addressed mesh reuse;
- parametric straight duct, elbow, reducer/expander, tee, and manifold assets;
- internal cutting planes and reusable section definitions.

Promotion gate: three real geometry classes import reproducibly, preserve named
regions, pass quality policy, and survive rerun/cache/cleanup workflows.

### M3 — dependable turbulent industrial internal flow

- steady and transient k-omega SST and k-epsilon workflow selection;
- inlet turbulence and developed-inlet templates with explicit assumptions;
- wall-resolved and wall-function mesh plans with solved y-plus evidence;
- elbows, tees, diffusers, valves, and manifolds;
- pressure loss, branch flow split, uniformity, recirculation, forces, moments,
  and residence-time observables;
- parallel decomposition, restart/resume, and failure-safe long runs;
- mesh/time-step/model sensitivity certificates.

Promotion gate: benchmark matrix across Reynolds number and geometry, no global
turbulence default without evidence, and cross-version OpenFOAM reproducibility.

### M4 — heat, gases, and single-phase steam

- energy equation, gravity and buoyancy, temperature-dependent properties;
- CoolProp/IAPWS-IF97 state and table provenance;
- thermal walls, heat flux, convection, conjugate regions, and heat balance;
- heated pipes, ducts, heat-exchanger passages, throttling, and low-Mach gas;
- `rhoSimpleFoam`/`rhoPimpleFoam` provider slices selected by physics rather
  than exposed as user concepts;
- temperature, enthalpy, heat-transfer coefficient, heat flux, and entropy
  observables.

Promotion gate: mass and energy closure plus independent steam/gas benchmark
points over a declared single-phase applicability envelope.

### M5 — equipment and operating maps

- porous loss, screens, filters, fan and pump curves, rotating frames, and
  source-term zones;
- valves, fans, pumps, mixers, and simplified heat exchangers;
- parameter campaigns, operating maps, design comparison, cached meshes, and
  resumable parallel execution;
- report tables and compact engineering summaries as first-class artifacts.

Promotion gate: one realistic equipment decision workflow runs as a single
project and as a resumable campaign with identical semantics.

### M6 — reacting and multiphase expansion

- species, mixtures, chemistry provenance, Cantera-compatible mechanisms,
  radiation, premixed and non-premixed combustion;
- only after single-phase thermal maturity: particles, cavitation,
  condensation, boiling, and selected multiphase models;
- chemistry/turbulence/radiation uncertainty kept separate in plans and
  evidence.

Promotion gate: canonical flames and equipment benchmarks; no generic
"combustion supported" label from one solver template.

### M7 — AgentFEM and learning ecosystem

- conservative pressure, traction, temperature, heat-flux, and mesh-motion
  exchange with AgentFEM;
- partitioned conjugate heat transfer and FSI, optionally via preCICE;
- accepted campaign datasets, normalization and mesh identity contracts;
- user-owned surrogates, neural operators, calibration, optimization, and
  automatic high-fidelity fallback;
- agent tools for project creation, plan explanation, bounded repair, campaign
  management, and evidence review.

Promotion gate: learned or coupled paths retain the same applicability, trust,
and fallback semantics as direct CFD.

## Execution priorities

Work is selected by this order:

1. complete a valuable end-to-end user workflow;
2. remove a repeated usability or correctness failure;
3. generalize only abstractions exercised by at least two workflows;
4. improve performance or storage after measurement;
5. broaden physics only when the previous family has acceptance evidence.

For the next development cycles the order is therefore:

```text
executable baffle channel
→ compact report recovery and result UX
→ imported geometry + named-region audit
→ elbow / tee / manifold RANS evidence
→ heat and single-phase steam
```

Additional smooth-pipe tuning is maintenance evidence, not the product lead.

## Definition of done for every public capability

A capability is not done until all applicable items hold:

- one concise human-readable project and one machine analysis record;
- complete public-input validation and provider fail-closed behavior;
- deterministic preparation and content identity;
- bounded execution, interruption, restart, and storage policy;
- mesh, conservation, convergence, and output-completeness checks;
- canonical quantities/histories/fields with units and associations;
- benchmark or verification evidence with applicability limits;
- tests from source and installed wheel;
- documentation, capability catalog, changelog, and explicit known limits.

## Product metrics

- median time from project creation to first accepted result;
- accepted-result rate, separate from process success;
- percentage of failures with stable code and actionable repair;
- mass/energy imbalance, benchmark error, and mesh-quality distributions;
- peak backend bytes per published field byte;
- successful restart rate and campaign reuse rate;
- percentage of public API inputs consumed or explicitly rejected by providers;
- successful AgentFEM and dataset handoffs retaining units, association, mesh
  identity, and trust.

## Reference basis

The roadmap follows primary upstream workflows:

- [OpenFOAM blockMesh](https://doc.openfoam.com/2606/tools/pre-processing/mesh/generation/blockMesh/)
  for the first deterministic structured multi-block channel;
- [OpenFOAM pimpleFoam](https://doc.openfoam.com/2312/tools/processing/solvers/rtm/incompressible/pimpleFoam/)
  and [potentialFoam](https://doc.openfoam.com/2306/tools/processing/solvers/rtm/basic/potentialFoam/)
  for transient flow and initialization;
- [OpenFOAM probes](https://doc.openfoam.com/2606/tools/post-processing/function-objects/sampling/probes/),
  [surfaceFieldValue](https://doc.openfoam.com/2306/tools/post-processing/function-objects/field/surfaceFieldValue/),
  and [forces](https://doc.openfoam.com/2606/tools/post-processing/function-objects/forces/forces/)
  for compact observables;
- [OpenFOAM common boundary combinations](https://doc.openfoam.com/2606/tools/processing/boundary-conditions/common-combinations/)
  for robust pressure/flow pairing;
- [PyFluent meshing workflows](https://fluent.docs.pyansys.com/version/stable/user_guide/meshing/new_meshing_workflows.html)
  for staged import, sizing, region, boundary-layer, and volume-mesh UX.
- [SU2 solver setup](https://su2code.github.io/docs_v7/Solver-Setup/),
  [quick start](https://su2code.github.io/docs/Quick-Start/), and
  [official licensing/download](https://su2code.github.io/download.html) for a
  possible later LGPL external verification and optimization provider.
