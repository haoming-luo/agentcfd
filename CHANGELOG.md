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
