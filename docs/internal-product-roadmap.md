# AgentCFD internal product roadmap

Status: active engineering plan, 2026-09-29. This document is an internal
decision record, not a maturity claim or release promise. Public capability
claims remain in the capability catalog and validation evidence.

## Product thesis

AgentCFD is not an OpenFOAM dictionary generator and not a collection of CFD
examples. It is the engineering operating layer that turns readable intent into
a reviewable decision with the least avoidable human work, compute, and storage.

The initial market remains industrial internal flow: pipes, ducts, branches,
valves, manifolds, heat, gases, steam, and later combustion. Aerospace breadth
does not lead the sequence. OpenFOAM stays the primary external numerical
provider; analytical and 0D models provide instant screening and boundary
conditions; AgentFEM, AgentCAE, and learning packages connect through versioned
scientific contracts.

The north-star interaction is:

```text
engineering question
  -> one readable project and case.py
  -> check and explain the plan before compute
  -> run through a bounded provider workspace
  -> compact decision results + selected XDMF/H5 fields
  -> verify, compare, couple, or learn without reconstructing semantics
```

## Evidence and consequences

### What we retain from AgentFEM

- one solver-neutral public language with progressive discovery levels;
- the same semantic objects for Python, CLI, agents, and future GUI clients;
- explicit Model/Step/Result lifecycles instead of backend commands as the
  product vocabulary;
- stable, versioned, strict machine contracts and addressable errors;
- optional extensions outside the core when a framework or runtime is not a
  scientific requirement;
- results that distinguish quantities, fields, histories, artifacts,
  provenance, acceptance, and scientific trust.

### What deliberately remains CFD-specific

- `case.py` is source, `output/` is the replaceable current answer,
  `campaigns/` is opt-in history, and `.agentcfd/` is disposable provider state;
- compact monitors and report histories are independent of sparse volumetric
  field frames and restart checkpoints;
- storage, mesh, convergence, conservation, and restart policy are planned
  before a provider starts;
- the public language describes fluids, regions, boundaries, procedures,
  observables, and engineering acceptance—not OpenFOAM applications or files.

### External research decisions

- Python extensions will use namespaced package entry points and
  `importlib.metadata`, following the PyPA entry-point and plugin-discovery
  specifications. Importing AgentCFD must not import optional ML or solver
  runtimes.
- OpenFOAM function objects are the default route for compact runtime results.
  Upstream documents that they avoid retaining all generated data and support
  standardized batch workflows. Full fields remain selected products rather
  than the only source of engineering observables.
- A future MCP adapter will be thin. Read-only resources expose project status,
  plans, summaries, schemas, and evidence; typed tools invoke bounded public
  operations. MCP's separation of resources, prompts, and tools means AgentCFD
  must first own a small, deterministic discovery and operation contract.
- AgentFEM coupling will use a partitioned participant boundary. preCICE already
  provides official OpenFOAM and FEniCSx adapters, explicit participants,
  exchanged data, interface meshes, mappings, and coupling schemes. AgentCFD
  should own the coupling manifest and semantic checks, not reimplement a
  general coupling library.
- Physics-AI support will be data-contract-first. PhysicsNeMo's modular models,
  datapipes, mesh utilities, distributed training, and active-learning patterns
  reinforce keeping framework/device/weight concerns outside the CFD core.

Primary references:

- <https://packaging.python.org/en/latest/specifications/entry-points/>
- <https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/>
- <https://doc.openfoam.com/2212/tools/post-processing/function-objects/>
- <https://doc.openfoam.com/2606/tools/post-processing/utilities/postProcess/>
- <https://modelcontextprotocol.io/specification/draft/server/index>
- <https://py.sdk.modelcontextprotocol.io/>
- <https://precice.org/adapters-overview>
- <https://precice.org/configuration-introduction>
- <https://precice.org/configuration-mapping>
- <https://docs.nvidia.com/physicsnemo/latest/overview.html>

## Architectural laws

1. A human-readable engineering project is the durable source of truth.
2. Core scientific objects never depend on OpenFOAM, PyTorch, a GUI, or MCP.
3. Every provider input is consumed, explicitly rejected, or reported as
   unsupported; nothing scientific is silently ignored.
4. Inspect and execute select the same provider and resolved options.
5. A successful process is not automatically an accepted or trusted result.
6. Compact quantities and histories are first-class products; fields are
   selective evidence and visualization products.
7. XDMF/H5 is the standard spatial exchange; scalar JSON/JSONL tables are the
   standard decision and dataset index; NPZ remains optional adaptation.
8. Extensions advertise themselves; core does not scan imports or hard-code
   framework packages.
9. Coupling and learning may consume only explicit units, associations, mesh
   identity, coordinate/time semantics, provenance, and trust.
10. An AI can propose intent and choose declared operations, but cannot bypass
    deterministic validation, provider gates, or acceptance policy.
11. Public breadth is promoted by completed workflows and evidence, not by the
    number of classes, CLI commands, or solver keywords.
12. Hosted CI, solver compute, disk, and user attention are product resources.

## Target architecture

```text
Human / Python / CLI / GUI / MCP
                |
       discovery + operation contracts
                |
 Project -> Model -> Step -> Result -> Verification
                |
      provider resolution + option contract
                |
 analytical | 0D network | OpenFOAM | future providers
                |
  compact reports | XDMF/H5 | AgentCAE exchange
                |
 AgentFEM coupling | learning extensions | operating maps
```

The core owns semantics, lifecycle, evidence, and file layout. Providers own
bounded lowering and recovery. Extensions own integration-specific runtimes.
No outer interface is allowed to invent a second simulation model.

## Release sequence and gates

### A6 — product-foundation consolidation (exit-gate review)

Goal: make the already broad product surface discoverable, stable, and easier
to maintain before adding more physics.

Deliverables:

- one capability/interface manifest containing progressive module tiers,
  Model/Step/Project facade methods, CLI inventory, preferred machine commands,
  workflow stages, version, and scientific capabilities;
- tests proving the manifest agrees with Python facades and the actual parser;
- one provider registry and provider-option contract used identically by check,
  plan, prepare, run, and resume;
- fold the experimental 0D result into the common result vocabulary without
  pretending it is a spatial CFD field result;
- extract coherent services from the oversized project and CLI modules while
  preserving the current public contract;
- freeze a compact, typed error and next-action vocabulary for all common
  project states.

Exit gate:

- a fresh agent can discover the core workflow and complete a standard project
  without parsing help text or importing provider internals;
- every advertised facade method and command exists and is tested;
- provider selection cannot differ between inspection and execution;
- no change regresses the replace-by-default project or output policy.

### A7 — safe AI operation and extension boundary (active)

Goal: let AI clients and optional packages operate AgentCFD without privileged
knowledge of its internals.

Deliverables:

- namespaced entry-point groups for providers, exporters, property packages,
  and learning adapters, with descriptor-first discovery and compatibility
  checks;
- a small operation catalog with read/write/execute, idempotence, solver-start,
  expected artifacts, retry, and approval metadata;
- thin MCP resources and tools generated from the same contracts;
- explicit project upgrade and contract-compatibility reports;
- golden agent journeys for create, check, plan, run, inspect, diagnose,
  compare, and verify.

Exit gate:

- optional packages can be installed and discovered without editing core;
- an AI tool never receives arbitrary shell or arbitrary Python execution;
- every mutating operation has a preview, bounded target, and structured result.

### B1 — industrial system-to-3D loop

Goal: make AgentCFD useful before a high-fidelity mesh is created and preserve
the relationship between system screening and selected 3D runs.

Deliverables:

- common-result 0D networks with pumps, valves, sources, and operating points;
- explicit conversion of accepted 0D port states into reviewable 3D boundary
  suggestions, never silent source edits;
- tee/manifold flow distribution, elbow/component pressure loss, and operating
  maps through compact reports;
- reusable RANS templates with declared wall treatment and sensitivity gates;
- campaign promotion that stores only selected field-rich runs by default.

Exit gate:

- one real system is screened in 0D, selected in a campaign, solved in 3D, and
  compared through common quantities with traceable assumptions;
- branch conservation and pressure-loss decisions do not require loading HDF5.

### 0.2 — heat, low-Mach gases, and single-phase steam

Goal: cover the first high-value thermal equipment workflows without claiming
general compressible or multiphase support.

Deliverables:

- thermodynamic-state and property-provider contracts with provenance and
  applicability envelopes;
- energy, enthalpy, heat-rate, heat-flux, and balance observables;
- heated ducts, low-Mach gas pipes, throttling screens, and single-phase steam;
- thermal result exchange compatible with AgentFEM and AgentCAE;
- mass and energy closure plus grid/time/model sensitivity evidence.

Exit gate: independent reference points and accepted end-to-end equipment
projects over a published single-phase envelope.

### 0.3 — multiphysics and Physics AI

Goal: connect trusted CFD work to structural/thermal coupling and learned
acceleration without moving those dependencies into the core.

Deliverables:

- versioned coupling manifests for pressure/traction, temperature/heat flux,
  displacement/mesh motion, participant identity, mapping, time windows, and
  convergence;
- preCICE-based CHT and then FSI pilots with OpenFOAM and AgentFEM/FEniCSx;
- learning extension protocol for dataset selection, normalization, splits,
  mesh representation, weights, metrics, applicability, and fallback;
- active-learning loop that promotes uncertain cases to direct CFD and retains
  the same result/trust vocabulary;
- neural operators or surrogates as replaceable accelerators, never as opaque
  sources of accepted truth.

Exit gate: a coupled or learned result can be audited with the same units,
identity, evidence, and fallback semantics as a direct AgentCFD result.

## Workstreams and measurable outcomes

| Workstream | Primary measure | Guardrail |
| --- | --- | --- |
| Project UX | median actions from init to first reviewable result | no hidden source rewrites |
| Agent UX | successful golden journeys using contracts only | no log/help-text parsing |
| Scientific trust | accepted/trust rates by bounded capability | process success never upgrades trust |
| Storage | peak provider bytes / published bytes | compact reports independent of field cadence |
| Runtime | comparable cell-updates and restart success | no fabricated energy or ETA claims |
| Maintainability | size/cohesion of project and CLI services | no public break during extraction |
| Ecosystem | lossless AgentCAE/FEM/learning handoffs | units, mesh identity, provenance required |
| CI | one local full gate and one Linux fast gate per coherent batch | no push-driven debugging loop |

## Immediate implementation status

The first A6 slice now provides the discovery contract required by every later
GUI, MCP server, extension loader, documentation generator, and AI workflow. It
answers four questions:

1. What is the recommended core workflow?
2. Which Model, Step, and Project methods are public?
3. Which CLI commands actually exist, and which machine paths are preferred?
4. Which scientific capabilities are mature enough to claim?

The capability catalog now includes those inventories and the package version,
while retaining the evidence/limitations records. It also embeds a provider
family and option catalog, plus a bounded operation catalog with conservative
side-effect and MCP annotations. Check, plan, run, and resume share the same
provider resolver; the 0D network has an explicit scalar-only common-result
bridge. Tests bind all inventories to the real facades, parser, schemas, and
installed wheel.

Provider planning and compact result-summary construction are now pure internal
services rather than additional branches inside the Project facade. A golden
contract-driven reference journey covers create, check, plan, run, inspect,
result, verify, and cleanup preview without parsing help text or invoking a
shell.

A7 is now implemented at its first usable boundary: versioned entry-point groups support descriptor-first
provider, exporter, property, and learning discovery without importing optional
code; explicit loads validate a minimal extension interface. A dependency-free
MCP manifest generates resources, project resource templates, typed inputs, and
risk annotations from the existing operation catalog. Separate `agentcfd-mcp`
and `agentcfd-learning` packages now provide a bounded stdio transport and a
leakage-safe scalar dataset adapter. A read-only compatibility report classifies
project schemas without importing user code. Later reviewed migrations and
field-learning adapters must still avoid widening the core dependency set.

## Explicitly deferred

- a broad MCP tool list beyond the bounded operation and risk contracts;
- automatic installation of OpenFOAM, ML frameworks, or coupling runtimes from
  a simulation request;
- generic combustion or multiphase labels from one example;
- a second public language for AgentCAE, a GUI, providers, or learning;
- automatic model edits in response to numerical failure;
- storing every time step or every campaign field payload by default;
- refactoring large modules without behavioral characterization tests.
