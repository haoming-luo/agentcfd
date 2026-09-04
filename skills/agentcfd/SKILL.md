---
name: agentcfd
description: Build, run, inspect, and verify AgentCFD computational-fluid-dynamics workflows.
---

# AgentCFD

Use this skill when a user asks an AI agent to create or operate an AgentCFD
model.

## Workflow

1. Run `agentcfd doctor --json` and `agentcfd capabilities --json`.
2. State the intended physics, geometry, properties, boundaries, procedure,
   outputs, and acceptance checks before execution.
3. Use the public `Study -> Model -> Step -> SimulationResult` API.
4. Reject a provider that does not advertise the required capability.
5. Run the model and preserve structured results and provenance.
6. Report convergence, conservation, applicability, and failed checks.
7. Export learning data only from accepted results unless the user explicitly
   records another scientific disposition.

## Current capability boundary

The released in-process scientific path is the steady incompressible Newtonian
laminar circular-pipe reference workflow. The external OpenCFD v2606 provider
also has experimental, evidence-gated laminar and smooth-pipe RANS slices.

For turbulent fully developed inlet evidence:

- use `openfoam-turbulent-precursor` with an explicit `--turbulence-model`;
- pair k-omega SST with a declared supported momentum wall function;
- pair standard k-epsilon only with `nutkWallFunction` and its generated
  `epsilonWallFunction`;
- use `openfoam-turbulent-wall-function-study` to isolate SST wall treatment;
- use `openfoam-turbulent-model-study` to compare the supported SST/Spalding
  and k-epsilon/nutk pairs on an identical mesh;
- treat every recommendation as benchmark-specific when
  `default_promotion_accepted` is false.

Downstream developed-field mapping remains k-omega SST-only. Do not silently
map k-epsilon fields, extend evidence to rough walls or another geometry, or
equate a correlation screen with experimental validation.
