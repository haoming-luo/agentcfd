# Changelog

## Unreleased

- Add a content-addressed periodic `simpleFoam`/`meanVelocityForce` k-omega SST
  circular-pipe precursor with CLI prepare/run workflows.
- Recover developed `U`, `p`, `k`, `omega`, and `nut`, pressure-gradient and friction
  evidence, residuals, y-plus, mesh identity, and immutable container identity.
- Record OpenCFD v2606 c8 execution evidence and the fail-closed rejection of
  `boundaryFoam` for circular geometry.
- Add accepted-precursor mapping to downstream turbulent pipes with source,
  mesh, runtime, and field identity checks before and after OpenFOAM `mapFields`.
- Separate 1.05% precursor-to-target transfer verification from the 3.81%
  smooth-Colebrook model-form diagnostic, and solve mapped velocity fields to
  their absolute linear tolerance.
- Publish the precursor-map JSON Schema and a verified 38,400-cell OpenCFD
  v2606 execution record.

All notable AgentCFD changes are recorded here. Versions follow semantic
versioning; scientific capability maturity remains independently visible in the
machine-readable capability catalog.

## 0.1.0a3 — 2026-09-04

- add an explicit `k-omega-sst` smooth circular-pipe model, declared wall
  treatment, and a typed turbulence-intensity/length-scale inlet that all
  remain part of model identity;
- lower the turbulent slice to OpenCFD v2606 `simpleFoam` using an exact
  flow-rate inlet, `kOmegaSST`, `k`, `omega`, and blended wall functions;
- recover native `U`, `p`, `k`, `omega`, and `nut` fields plus per-iteration
  y-plus, flow, pressure, residual, and linear-iteration histories;
- add explicit wall-y-plus, turbulent residual, observable-stability, friction,
  runtime-version, mesh, conservation, and output-completeness gates;
- keep the smooth-pipe friction comparison diagnostic and fail reference
  applicability closed until developed-inlet and turbulent grid evidence pass;
- add human- and agent-facing `prepare` and `run openfoam-turbulent-pipe`
  commands with deterministic manifests, stable completed-unaccepted status,
  and compact failed-gate guidance in both JSON and human output;
- reduce the measured 38,400-cell benchmark solve from a multi-minute strict
  linear-solver configuration to about 15 seconds while preserving explicit
  RANS convergence evidence;
- stop the exact Docker container on keyboard interruption as well as timeout,
  avoiding orphaned OpenFOAM work;
- validate the first real OpenCFD v2606 arm64 run at Re 99,621: converged,
  relative mass imbalance `5.27e-7`, average y-plus `86.55`, and intentionally
  unaccepted 10.94% smooth-pipe friction difference.

## 0.1.0a2 — 2026-09-03

- validate the fully developed OpenCFD v2606 pipe workflow with an accepted
  8/16/32 three-grid study: observed order 2.0443, fine-grid GCI 0.5079%, and
  fine-grid Hagen--Poiseuille pressure-drop error 0.2873%;
- normalize the analytic inlet profile by its discrete area integral so every
  mesh matches the requested physical circular-pipe flow to machine precision;
- accept bounded axis-aligned pipe convergence from axial residual, pressure
  stability, and conservation evidence when zero transverse-component
  normalized residuals prevent OpenFOAM's aggregate marker;
- bind prepared cases to model, procedure, and output request with an analysis
  SHA-256, and require matching analysis identities in new GCI result records;
- update the validated grid-study default to 12,800 / 102,400 / 819,200 cells
  and repair the GCI schema for its three acceptance checks.

- recover OpenFOAM inlet/outlet flow and pressure histories automatically;
- add `checkMesh` execution, structured mesh-quality observables, physical-Pa
  pressure drop, mass balance, final native fields, and runtime provenance;
- add an explicit non-compiling fully developed circular-pipe velocity inlet;
- add end-to-end OpenFOAM execution to the CLI;
- add explicit Docker-backed OpenFOAM execution for macOS and CI without
  wrapper scripts;
- add solver-neutral three-grid Richardson extrapolation and GCI utilities with
  unequal-ratio support and fail-closed oscillatory-convergence handling.
- add dependency-free hydraulic diameter, Reynolds number, Darcy--Weisbach,
  bracketed Colebrook--White, and local-loss engineering functions; transitional
  flow is rejected instead of silently interpolated;
- add an auditable composite pipe-loss estimate for laminar and turbulent
  incompressible screening calculations;
- reject non-finite physical inputs and non-integral solver/mesh controls at the
  public boundary;
- expose auditable cross-section and axial mesh resolution controls in the CLI;
- add a CLI workflow and JSON schema that computes GCI directly from three
  converged same-model AgentCFD result files and hashes every source;
- make the core runtime dependency-free while retaining NumPy array support as
  an optional `arrays` extra;
- add ideal-gas/Mach screening functions and a lazy optional CoolProp property
  provider with structured IF97 water/steam state records;
- add a fail-closed same-model OpenFOAM three-grid preparation workflow with
  isotropic resolution scaling, per-case identities, and a JSON study schema;
- make OpenFOAM mass-balance and pressure-error acceptance thresholds explicit,
  validated, and part of every result's scientific inputs;
- reject unknown boundary objects, non-boolean study flags, duplicate output
  names, and non-finite or non-JSON model metadata before fingerprinting;
- report Reynolds number and an explicit uniform-inlet laminar development
  length diagnostic instead of conflating entrance effects with mesh error;
- verify model, file, combined-case, and path-containment identities before
  executing an existing prepared OpenFOAM case;
- execute a prepared three-grid family end to end and automatically write
  source-hashed GCI evidence, while rejecting mixed prior execution output.
- recover structured per-equation initial residual, final residual, and linear
  iteration histories from OpenFOAM solver evidence.
- reject unrecorded prepared-case files, directories, and symbolic links that
  could alter solver semantics outside the content-addressed manifest.
- make the console fail closed with stable exit codes for execution failure,
  expected input errors, and completed-but-unaccepted results.
- record the immutable Docker image SHA-256, repository digests, and platform,
  and fail container-run acceptance when that provenance cannot be verified.
- make generated-case byte identities and unrecorded-path checks portable
  across LF and CRLF hosts.
- add fail-closed turbulent-pipe wall-resolution and target-`y+` screening
  functions for future RANS mesh setup.
- compare recovered inlet flow against the public boundary request under an
  explicit policy, so coarse inlet integration error cannot be hidden.
- record resolved mesh controls, verify expected versus actual cell count, and
  bind final fields to a content-addressed native `polyMesh` manifest.
- add a dependency-free installed-contract discovery API and CLI for AgentCFD,
  AgentFEM, AI pipelines, and external validators.
- add dependency-free result reopening that recomputes trust and verifies every
  artifact identity; use it automatically before file-based GCI.
- reject duplicate JSON keys and non-standard non-finite numbers when reopening
  scientific result evidence.
- stop the exact Docker container identified by a per-command CID file when an
  OpenFOAM subprocess times out.
- add explicit turbulence-intensity/length-scale initialization for `k`,
  `omega`, and `epsilon` as a bounded RANS setup primitive.
- add a bracketed circular-pipe operating-point solver that inverts available
  pressure into flow without hiding laminar, transitional, or turbulent choice.
- expose forward pipe loss and inverse pressure-to-flow calculations through a
  dependency-free JSON CLI for engineers and agents.
- publish the content-addressed OpenFOAM mesh manifest as an installed,
  versioned JSON Schema contract.
- add an explicit GCI promotion policy for fine-grid uncertainty and
  asymptotic-range evidence, with fail-closed CLI status.
- fail scientific acceptance closed for unvalidated OpenFOAM runtime versions.
- validate OpenFOAM output requests and fail acceptance when requested native
  fields or histories are missing after execution.
- record command-level return codes and monotonic wall-clock durations for mesh,
  mesh checking, and solver execution.
- require structured pressure and velocity outer-residual evidence below the
  configured tolerance in addition to OpenFOAM's convergence marker.
- expose a validated per-command OpenFOAM timeout for single and three-grid CLI
  executions.
- require a configurable stable tail window for pressure drop in addition to
  algebraic residual convergence.
- reject ambiguous or non-standard JSON in prepared OpenFOAM case and grid-study
  control records.
- make non-orthogonality, skewness, and aspect-ratio mesh limits explicit
  scientific acceptance inputs instead of relying only on `Mesh OK`.
- separate uniform-inlet entrance effects from discretization error by making
  fully developed pressure-reference applicability an explicit validation gate.
- add authoritative IAEA thermal-mixing, Sandia turbulent-flame, and NIST spray
  combustion cases to the machine-readable benchmark roadmap.
- mark every benchmark dataset link-only until redistribution terms are
  explicitly reviewed.
- expand the development dependency inventory and record NumPy's composite
  permissive license expression rather than reducing it to one top-level label.
- expose a dependency-free machine-readable license catalog for core, optional
  Python extras, and the external OpenFOAM process boundary.
- publish installed JSON Schema contracts for benchmark and license catalogs.
- add a solver-neutral single-point validation assessment with explicit
  numerical, input, experimental, and coverage-factor uncertainty components.
- expose validation-point assessment through the CLI and an installed JSON
  Schema with fail-closed scientific exit status.
- reject boolean values consistently across public engineering correlations,
  including roughness and local-loss inputs.
- reject boolean numeric quantities and non-boolean result/check state before
  computing scientific acceptance or trust.
- enforce unused-import linting in the default quality gate.
- reject boolean serialized quantities and non-boolean derived acceptance state
  before reopening result evidence or computing GCI.
- validate serialized check names/kinds and reject boolean artifact sizes during
  result evidence reopening.
- reject non-string mapping keys before model or scientific-input fingerprinting
  to prevent JSON key-normalization collisions.
- require identical quantity units and dimensionless cell counts across all
  serialized results in a GCI study.
- validate typed quantity, field, history, artifact, and check collections when
  constructing a result instead of failing later during serialization.
- add an explicit, auditable low-Mach incompressible-model screening result.
- reject ambiguous non-string array and learning-sample names, and duplicate
  requested outputs, before AgentCAE serialization.
- fail closed on malformed or duplicate CFD-to-FEM coupling fields and validate
  emitted coupling manifests against the installed contract.
- expose low-Mach model screening through the human- and agent-facing CLI.
- publish a capability-catalog JSON contract and advertise gas screening and
  single-observable validation with explicit maturity boundaries.
- enforce runtime types for model/step components and non-empty string names
  for physical geometry and fluid assets.
- cross-check quantity records and artifact indexes when reopening a result so
  redundant exchange representations cannot silently disagree.
- validate every thermophysical-state identity and positive SI property at its
  construction boundary, including manually created records.
- expose versioned CoolProp/IF97 pressure-temperature states through the CLI.
- version thermophysical-state records and ship their JSON Schema contract.
- document agent-facing gas screening, IF97 state evaluation, and installed
  contract discovery in the primary quickstart.
- exercise low-Mach screening in both offline-wheel CI and release smoke gates.
- fail closed with domain errors when GCI receives malformed record, label,
  policy, or solution types instead of leaking incidental attribute failures.
- validate constructed GCI result records for finite values, ordered refinement
  ratios, non-negative uncertainty, boolean state, and a valid safety factor.
- include the declared monotonic-convergence state in GCI promotion acceptance.
- keep the turbulent inverse pipe-flow bracket strictly above Re 4000 despite
  floating-point reconstruction roundoff.

## 0.1.0a1 — 2026-09-03

- establish the Apache-2.0 AI-native CFD engineering workflow;
- add typed internal-flow studies, circular-pipe geometry, Newtonian fluids,
  boundaries, procedures, outputs, model fingerprints, and structured results;
- add the released Hagen–Poiseuille reference provider with applicability,
  Darcy–Weisbach identity, and mass-balance checks;
- add experimental content-addressed OpenFOAM `simpleFoam` case lowering for a
  full three-dimensional O-grid circular pipe;
- preserve the circular pipe boundary with explicit `blockMesh` arc edges and
  add a reproducible OpenCFD v2606 Linux/arm64 execution-evidence record;
- distinguish normal solver completion from OpenFOAM's explicit numerical
  convergence marker in structured result trust semantics;
- add bounded external-process execution that remains scientifically unaccepted
  until field conservation and pressure-loss recovery are implemented;
- add AgentFEM interoperability records and versioned JSON schemas;
- align results with AgentFEM semantics for quantities, fields, histories,
  artifacts, scientific-input fingerprints, evidence claims, and trust levels;
- add solver-neutral `agentcae.simulation-result` and AgentFEM-compatible
  `agentcae.scientific-sample` records, with schemas included in release wheels;
- document concepts, workflow, validation, licensing, roadmap, market strategy,
  and PyPI trusted publishing;
- test Python 3.11–3.13 across Linux, macOS, and Windows in CI.
