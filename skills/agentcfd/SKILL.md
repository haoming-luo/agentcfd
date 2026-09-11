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
8. Before a multi-point campaign, run `sweep --plan-only --json` and set an
   explicit `--max-runs` compute boundary.
9. Prefer `--summary-only` for scalar screening; use `promote <run-id>` only
   when an accepted candidate needs XDMF/HDF5 fields or spatial postprocessing.
10. Use `logs` and `diagnose` with the failed point's immutable `--run-id`
    after any continue-on-error campaign.
11. Never apply `compact <run-id> --apply` without first showing its preview and
    confirming that the exact full-field payload is no longer required.
12. For a new supported STL/OBJ internal-flow case, prefer
    `init --template imported-internal-flow` with explicit unit, role map,
    interior point, inlet vector, mesh size, and maximum cells; never invent
    any of those inputs from appearance or filenames.
13. When an agent already has all confirmed setup inputs, prefer the installed
    `project-creation-request.schema.json` plus `init --request`; treat that JSON
    as an ephemeral creation envelope and `case.py` as the continuing source of
    truth.
14. Keep the generated `outputs.pressure_loss("system-loss", ...)` report for
    one-inlet/one-outlet component studies. Rank designs from compact
    `report.system-loss.loss_coefficient` values through `result` or campaign
    tables; do not open XDMF/H5 for a scalar decision and do not call the value
    a fitting-only K unless a straight-run baseline was explicitly removed.
15. For a one-parameter campaign decision, use `campaigns --plot-svg` with a
    declared numeric x parameter and canonical scalar y quantity. Keep the
    accepted-only default; showing unaccepted evidence must remain explicit.
16. Use `verify component-loss --confirm-equivalent-baseline` only after
    confirming equivalent distributed length, section/measurement planes,
    walls/roughness, fluid, and operating point. Provider/runtime/physics or
    reference mismatches and negative local K remain unaccepted evidence.

## Current capability boundary

The released in-process scientific path is the steady incompressible Newtonian
laminar circular-pipe reference workflow. The external OpenCFD v2606 provider
also has experimental, evidence-gated laminar and smooth-pipe RANS slices.
Its released imported-volume slices are steady, incompressible, and isothermal,
with exactly one inlet and one pressure outlet. Laminar flow accepts an explicit
Cartesian velocity, constant-density mass flow, or total gauge pressure; the
experimental k-omega SST slice requires Cartesian velocity, intensity, length
scale, and blended wall treatment. Reject requests outside those boundaries
instead of editing generated dictionaries.

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
