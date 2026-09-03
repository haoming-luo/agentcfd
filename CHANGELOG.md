# Changelog

All notable AgentCFD changes are recorded here. Versions follow semantic
versioning; scientific capability maturity remains independently visible in the
machine-readable capability catalog.

## Unreleased

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
