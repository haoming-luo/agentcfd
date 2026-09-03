# Changelog

All notable AgentCFD changes are recorded here. Versions follow semantic
versioning; scientific capability maturity remains independently visible in the
machine-readable capability catalog.

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
