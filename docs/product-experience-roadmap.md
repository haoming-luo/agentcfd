# Product experience roadmap: less CFD labor per trusted decision

AgentCFD's product metric is not the number of exposed solver switches. It is
the human attention, wall time, and storage required to move from readable
engineering intent to a reviewable result. The numerical provider remains
OpenFOAM; AgentCFD owns the workflow, decisions, evidence, and recovery layer.

## Evidence behind the workflow

Primary product evidence:

- OpenFOAM separates run controls such as write cadence, rolling `purgeWrite`,
  compression, and runtime modification in `controlDict`:
  <https://doc.openfoam.com/2312/fundamentals/case-structure/controldict/>.
- OpenFOAM function objects compute compact monitoring and derived results at
  runtime, avoiding a requirement to retain every full field:
  <https://doc.openfoam.com/2212/tools/post-processing/function-objects/>.
- `postProcess` can operate on selected stored times and fields:
  <https://doc.openfoam.com/2306/tools/post-processing/utilities/postProcess/>.
- OpenFOAM command-line time selection is a shared application convention; the
  native `timeSelector` accepts selected times before utility execution:
  <https://doc.openfoam.com/2306/fundamentals/command-line/> and
  <https://api.openfoam.com/2412/classFoam_1_1timeSelector.html>.
- ParaView Catalyst treats in-situ extraction as the route to eliminating large
  write-read-rewrite cycles:
  <https://docs.paraview.org/en/v5.13.0/Catalyst/>.
- Fluent's established workflow independently separates report definitions,
  monitor histories, autosave retention, and final case/data publication:
  <https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/flu_ug_reporting_sec_monitoring_solution.html>
  and
  <https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/flu_ug/flu_ug_CaseDataFiles.html>.

Public user signals consistently point to transition costs rather than missing
individual solver keywords: moving from tutorials to owned geometry, finding
one complete geometry-to-ParaView path, silent boundary/setup mistakes, and
uncontrolled time-directory output. These are directional signals, not a
statistical survey:

- <https://www.reddit.com/r/CFD/comments/1qarrl0/beginner_in_openfoam_how_to_move_from_tutorials/>
- <https://www.reddit.com/r/CFD/comments/1qnwvnf/openfoam_tutorial_for_using_my_own_cad_models/>
- <https://www.reddit.com/r/OpenFOAM/comments/1mbgi8i/how_can_i_learn_open_foam_easily/>
- <https://ri.itservices.manchester.ac.uk/csf4/software/applications/openfoam/>

## Product architecture

```text
case.py intent
    -> status/check/plan decision layer
    -> disposable OpenFOAM workspace
    -> compact monitors + sparse restart
    -> selected-time/selected-field conversion
    -> output/README + result.json + XDMF/H5 + evidence
```

The beginner surface is `status -> run -> view`. Expert commands remain
available behind that path. Every state supplies a next command; every expected
machine failure supplies a code, repair, and retry signal. Generated provider
files stay hidden and reproducible.

## Delivery sequence

### Shipped in the current development line

- readable replace-by-default project with explicit campaign mode;
- unified lifecycle states and one recommended next action;
- active-run collision protection and atomic phase records;
- interrupted/failed run detection with retained diagnostic workspace;
- XDMF/H5 default, optional NPZ, canonical units and associations;
- scalar histories/reports separated from field frames and restart checkpoints;
- budget estimation, actual portable bytes, compression, storage inventory, and
  preview-first temporary cleanup;
- selected-time and selected-field `foamToVTK` conversion;
- bounded-memory live provider logs and field-free progress snapshots with
  coordinate, residual, Courant, mass/pressure monitor, elapsed, and optional
  workspace-size evidence;
- terminal-aware `watch` streaming for people and JSON-Line agents;
- automatic failed-workspace retention and bounded project-aware solver-log
  access with phase selection and a versioned agent contract;
- evidence-linked deterministic classification of common resource,
  configuration, mesh, numerical, and runtime failures without unreviewed
  automatic model edits;
- identity-gated transient checkpoint recovery with explicit `resume`, source
  provenance, automatic initialization skipping, and cleanup protection for the
  sole recoverable workspace;
- persistent cleanup protection for workspaces deliberately retained by CLI or
  project policy, with a separate previewable release scope;
- one-command project doctor covering readiness, runtime, storage headroom,
  recovery and comparable cell-update work without fabricated energy claims;
- declarative plane slices, scalar contours, and line-seeded streamlines that
  generate portable ParaView scripts without copying field payloads;
- explicit camera plus screenshot/PNG-sequence/MP4 render intent, disabled by
  default and recorded as expected outputs without duplicating field payloads;
- scalar and explicit vector-component/magnitude plot-over-line recipes with
  canonical two-column CSV plus interactive and headless execution paths;
- one-command post-processing target and self-documenting output directory;
- field-free campaign design-point index and unit-preserving CSV export, with
  recursive storage scans remaining explicit opt-in;
- explicit `case.py` factory parameters shared by check/plan/run, fingerprinted
  into every design point and rejected on unknown names before execution;
- all-points-first sweep preflight, accepted-identity reuse, atomic progress,
  serial execution, and continue/fail-fast runtime policy;
- zero-solve sweep preview with request-level deduplication and stale-result
  rejection before accepted markers may suppress computation;
- immutable run-scoped campaign logs and diagnosis, with failed-vs-review
  semantics and copyable recovery commands in the sweep report;
- hard pre-execution solver-count budgets and request-level deduplication even
  for failed or review outcomes, preventing agents from repeating wasted work;
- summary-only campaign screening with distinct full-field identity, compact
  evidence retention, lower temporary-storage estimates, and no permanent H5;
- one-command, identity-gated promotion of accepted lightweight points into
  provenance-linked full-field results, with exact accepted reuse;
- preview-first reversible-by-recomputation campaign compaction that removes
  spatial bulk without H5 reads while preserving engineering/derived evidence;
- dependency-free imported STL/OBJ inspection with explicit units, SI bounds,
  named regions, topology defects, enclosed volume, and a memory guard;
- content-addressed imported-surface `Model` intent with confirmed boundary
  roles, portable fingerprints, atomic inspection reports, and project asset
  presence/hash gates;
- JSON Schemas for status, diagnosis, logs, storage, cleanup, and repairable
  errors.

### Next: eliminate repeated setup work

1. Extend the released steady laminar imported-volume slice with controlled
   STEP/IGES tessellation, prism layers, turbulence, and thermal physics.
2. Reusable industrial templates for bend, tee, manifold, valve/porous loss,
   fan/pump, heated pipe, and buoyant enclosure workflows.
3. Extend the shipped filter recipes with multi-view layout and explicit normal
   or tangential vector projection intent while preserving one field payload.
4. Persist bounded progress history and learned ETA calibration across runs,
   without converting monitoring into high-frequency field output.

### Then: scale without scaling attention

1. Add bounded resource-aware concurrency, explicit retry limits, and
   operating-map plots to the shipped serial/deduplicated campaign sweep.
2. Remote/container/HPC executor protocol with the same run state contract.
3. Streaming XDMF/HDF5 publication, followed by Catalyst extraction for cases
   where intermediate VTK and native full-field retention dominate I/O.
4. Heat/steam and conjugate-transfer workflows, then reacting flow only after
   evidence gates are satisfied.
5. Agent policy layer that may propose edits but cannot conceal assumptions,
   bypass readiness gates, or promote an unaccepted result.

## Product gates

Each release should measure: minutes of user attention to first accepted run,
percentage of failures with a direct repair action, temporary-to-final storage
amplification, rerun setup time, and number of manual OpenFOAM-file edits. A
feature that adds provider breadth but increases those costs is not complete.
