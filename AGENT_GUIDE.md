# Guide for AI agents

AgentCFD is operated through the same public Python workflow used by people.
Do not generate backend dictionaries until the resolved AgentCFD model passes
validation and the selected provider advertises the required capability.

## First run

```bash
python -m pip install -e .
agentcfd doctor --json
agentcfd capabilities --json
agentcfd init --template industrial-pipe first-flow
agentcfd project first-flow --json
agentcfd status first-flow --json
agentcfd run first-flow --json
agentcfd status first-flow --storage --json
agentcfd performance first-flow --json
agentcfd view first-flow --json
agentcfd verify project first-flow --json
python -m pytest -q
```

## Required behavior

1. State geometry, fluid properties, boundaries, procedure, and requested results.
2. Use SI units unless an API explicitly accepts another unit system.
3. Check provider capability before execution.
4. Preserve warnings, failed checks, provenance, and model fingerprints.
5. Never use a completed process as evidence that the physics is correct.
6. Do not train on a result unless `result.accepted` is true or a human explicitly
   records why a failed check is acceptable for that dataset.
7. Explain unsupported physics instead of silently changing the model.
8. Prefer the project lifecycle over case-specific CLI commands for new work.
9. Preserve XDMF, HDF5, NPZ, manifest, result, and plan as one field bundle;
   never infer physical time or point/cell association from shape alone.
10. Treat `status.next_action` as the default control loop. Use `check`, `plan`,
    and `inspect` only when the next action or an issue requires deeper detail.
    Use `project --json` or `open_project().snapshot()` when one request needs
    status, compact results, published paths, workspace retention, and the typed
    `next_action.operation` together; snapshots never authorize execution.
11. Preview `clean` before applying it. Never delete `output/`, `campaigns/`, or
    the protected workspace of a live run to save space.
12. During execution, use `watch --json` (JSON Lines) or poll `status --json`
    at a humane cadence and consume its `progress` object. Do not open native
    field files to estimate progress; request `--storage` only when disk
    accounting is needed.
13. Use `Project.discover()` or ordinary CLI `.` from nested project paths;
    never hard-code an output directory as if it were the project root.
14. On `failed` or `interrupted`, follow `status.next_action` to
    `diagnose --json`; retain its code, confidence, evidence, and repair in the
    agent trace. Use `logs --json --command …` only when diagnosis recommends
    raw drill-down. Never recursively ingest a retained OpenFOAM workspace just
    to locate the last error, and never turn a diagnosis into an unreviewed
    model mutation.
15. If `diagnosis.resume_after_repair` is present, repair the diagnosed cause
    first and then execute that exact `resume` command. Never resume after an
    analysis or execution fingerprint change, and never delete a
    `protected_recovery_run_ids` workspace.
16. Use `doctor PROJECT --json` for a deliberate full preflight or maintenance
    audit. Treat `cell_updates_proxy` as a relative work indicator only; never
    relabel it as runtime, cost, carbon, or energy, and preserve
    `energy.status=not-measured` without executor telemetry.
17. Keep camera/render recipes explicit. Prefer one screenshot for routine
    evidence and request PNG sequences or MP4 only when animation is a stated
    deliverable. Prefer a declared `render_layout` over launching and arranging
    the same named views manually; normal/tangential slice projection is
    post-processing intent and must not create another field bundle. Rendered
    images are derived products, never a replacement for the shared XDMF/HDF5
    field bundle.
18. Vary project design points only through declared `case.py` factory keyword
    arguments and repeatable `--param NAME=JSON_SCALAR`. Run `plan` with the
    same parameters first, retain them in provenance, and reject misspelled or
    unused names instead of editing generated dictionaries.
19. Use `campaigns --json` or its unit-preserving CSV for comparisons. Do not
    open every `result.json` history or HDF5 field merely to build a design
    table; request `--storage` only when recursive size is part of the decision.
20. Use a versioned `campaign-request` plus `sweep` for multiple named points.
    Preserve all-points preflight, reuse accepted matching identities, and keep
    serial execution until a bounded resource scheduler explicitly authorizes
    concurrency; do not parallelize OpenFOAM cases merely because CPUs exist.
21. Run `sweep --plan-only --json` before authorizing a campaign. Treat
    `would_execute_count` as the actual proposed solver-start count after
    request deduplication; never trust an accepted marker whose `result.json`
    is missing.
22. Put an explicit `--max-runs` boundary on unattended sweeps. Use
    `--summary-only` when the decision needs quantities and checks rather than
    fields, then call `promote <run-id>` only for accepted candidates that need
    animation, spatial review, AgentFEM exchange, or learned field data.
23. Diagnose campaign failures by immutable `--run-id`; never assume the latest
    run is the failed point after a continue-on-error sweep.
24. Treat `compact <run-id>` as destructive despite its safe default. Inspect
    the preview first and use `--apply` only when full fields are reproducible
    and no downstream consumer still needs that exact stored field payload.
25. Use `verify project --json` before archive, coupling, or dataset admission.
    Keep byte integrity, scientific acceptance, and trust level separate; a
    verified artifact set does not repair a failed physical check.
26. Use `performance --json` only for scheduling and ETA. Its bounded comparable
    runtime history is advisory and must never be treated as convergence,
    accuracy, acceptance, or permission to change the requested model.
27. For one-inlet/one-outlet imported equipment, preserve the generated
    `outputs.pressure_loss("system-loss", ...)` report. Use
    `report.system-loss.loss_coefficient` for compact design comparison and
    `report.system-loss.total_pressure_loss` for its dimensional value. This is
    the complete loss between the declared planes; never silently relabel it as
    a fitting-only K value or subtract straight-run friction without explicit
    baseline evidence.

## Current boundary

The laminar circular-pipe reference workflow is released. The bounded OpenCFD
v2606 fully developed laminar pipe has accepted 8/16/32 three-grid evidence.
Version 0.1.0a3 also exposes a diagnostic k-omega SST smooth-pipe slice with
explicit inlet turbulence inputs, wall functions, y-plus, friction, residual,
mass-balance, mesh, field, and runtime evidence. Its first real run converges
but is intentionally unaccepted because a developed inlet and turbulent grid
study have not passed. Do not extend either evidence record to another inlet,
OpenFOAM dialect, geometry, heat transfer, or rough wall. A generated case or a
zero process exit code is never a broader CFD claim.

The periodic circular-pipe precursor driven by OpenFOAM `meanVelocityForce`
can now be mapped into the downstream pipe through a content-addressed
`mapFields` contract. At the bounded c8 operating point, mapping transfer error
against the precursor pressure gradient is 1.05% and the result is verified.
The separate 3.81% Colebrook difference remains a model-form diagnostic; do
not extend this evidence to another Reynolds number, wall strategy, geometry,
or OpenFOAM dialect.

The fixed-wall-cell precursor workflow accepts an explicit
`nominal_wall_cell_fraction`. At Re 99,621, the 0.0625 c8/c16/c32 family kept
mean y-plus within 43.63--44.39 and its fine-pair pressure-gradient change was
0.630% after 50-sample stability checks. Use `verify turbulent-wall-study` for
this evidence. Never pass this family to Richardson/GCI: holding wall height
fixed changes radial grading, even though the final pressure-gradient sequence
is monotonic.

For a geometrically similar periodic precursor candidate, use
`verify turbulent-precursor-grid-study`; its characteristic size is
`h/D = 1/cross_section_cells`, not total cells to the `-1/3` power. The first
uniform c8/c12/c18 candidate retained wall y-plus above 30 but was oscillatory,
so its 0.212% fine-pair plateau is not a GCI or uncertainty certificate.

Use `openfoam-turbulent-wall-function-study` to isolate the supported SST
momentum wall functions on one mesh. At Re 99,621, Spalding reduces the c16
smooth-Colebrook difference to 1.851%. Its tighter-solver fixed-wall c16/c32
pressure-gradient change is only 0.00689%, with 1.851% and 1.858% correlation
differences. This is a plateau, not evidence that refinement removes model-form
difference.

The periodic precursor also supports standard k-epsilon paired with
`epsilonWallFunction` and `nutkWallFunction`. Use
`openfoam-turbulent-model-study` to hold mesh and non-model inputs fixed; its
first c16 screen finds 1.851% for SST/Spalding and 3.289% for k-epsilon/nutk.
Both are accepted at this point, but the assessment intentionally refuses
general default promotion. Downstream mapping remains SST-only.

For a Reynolds sweep, pass `--target-y-plus` to each
`prepare openfoam-turbulent-model-study` call. The resulting plan contains a
prediction-only wall-resolution screen; never replace the solved y-plus gate
with that estimate. Aggregate at least three point assessments with `verify
turbulent-model-sweep`. Cross-Re meshes may adapt wall spacing, but each point's
two model cases must retain one identical native mesh and the same non-model
inputs. The current four-point evidence changes winner between Re 99,621 and
199,242. Treat `diagnostic-ranking-only`, `model_selection_required=true`, or a
false range gate as an instruction to run sensitivity/validation—not as
permission to choose the aggregate ranking.

Prefer `prepare/run openfoam-turbulent-model-sweep` for unattended campaigns.
The campaign sorts distinct positive velocities, hashes every nested point
plan, writes `agentcfd-campaign-progress.json` after each point, and resumes by
reopening the underlying result artifacts rather than trusting a stale summary.
If progress says `failed`, repair the recorded point and rerun the same command;
do not edit a plan or mark the point complete manually.
