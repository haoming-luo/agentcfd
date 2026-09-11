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
- one truthful template catalog shared by CLI validation, provider defaults,
  people, agents, and future frontends, including required inputs and limits;
- unified lifecycle states and one recommended next action;
- active-run collision protection and atomic phase records;
- interrupted/failed run detection with retained diagnostic workspace;
- XDMF/H5 default, optional NPZ, canonical units and associations;
- isolated selected-field/time VTK conversion consumed one frame at a time into
  HDF5, with no reuse or deletion of a user's existing `VTK/` tree;
- scalar histories/reports separated from field frames and restart checkpoints;
- budget estimation, actual portable bytes, compression, storage inventory, and
  preview-first temporary cleanup;
- selected-time and selected-field `foamToVTK` conversion;
- bounded-memory live provider logs and field-free progress snapshots with
  coordinate, residual, Courant, mass/pressure monitor, elapsed, and optional
  workspace-size evidence;
- terminal-aware `watch` streaming for people and JSON-Line agents;
- bounded 50-sample runtime history with comparable-setup calibration used by
  `performance`, `status`, and `watch`, without reading or retaining fields;
- automatic failed-workspace retention and bounded project-aware solver-log
  access with phase selection and a versioned agent contract;
- evidence-linked deterministic classification of common resource,
  configuration, mesh, numerical, and runtime failures without unreviewed
  automatic model edits;
- identity-gated transient checkpoint recovery with explicit `resume`, source
  provenance, automatic initialization skipping, and cleanup protection for the
  sole recoverable workspace;
- in-run rolling checkpoint publication using stable-directory detection,
  bounded-memory ZIP streaming, atomic replacement, and low-cost progress
  visibility with constant-size publication telemetry before solver completion;
- persistent cleanup protection for workspaces deliberately retained by CLI or
  project policy, with a separate previewable release scope;
- preview-first decision/portable project archives that exclude native solver
  state by default, stream large fields, and verify every member independently;
- one-command project doctor covering readiness, runtime, storage headroom,
  recovery and comparable cell-update work without fabricated energy claims;
- declarative plane slices, scalar contours, and line-seeded streamlines that
  generate portable ParaView scripts without copying field payloads;
- shared-payload multi-view engineering layouts plus explicit Cartesian,
  magnitude, plane-normal, and tangential vector coloring for slice recipes;
- explicit camera plus screenshot/PNG-sequence/MP4 render intent, disabled by
  default and recorded as expected outputs without duplicating field payloads;
- scalar and explicit vector-component/magnitude plot-over-line recipes with
  canonical two-column CSV plus interactive and headless execution paths;
- one-command post-processing target and self-documenting output directory;
- field-free campaign design-point index and unit-preserving CSV export, with
  recursive storage scans remaining explicit opt-in;
- verified atomic scalar-dataset export with fixed parameter/quantity semantics,
  streamable AgentCAE sample lines, content hashing, and explicit exclusions;
- fail-closed dependency-free dataset reading with raw-unit matrices, optional
  NumPy adaptation, and bounded machine-readable shape/unit/range inspection;
- content-bound deterministic train/validation planning with explicit raw-unit
  z-score statistics and no duplicated sample payload;
- dependency-free, unit-labelled SVG operating maps over compact campaign
  markers, with accepted-only decision curves and explicit exclusions;
- explicit `case.py` factory parameters shared by check/plan/run, fingerprinted
  into every design point and rejected on unknown names before execution;
- all-points-first sweep preflight, accepted-identity reuse, atomic progress,
  serial-default execution, and unambiguous continue/fail-fast runtime policy;
- zero-solve sweep preview with request-level deduplication and stale-result
  rejection before accepted markers may suppress computation;
- immutable run-scoped campaign logs and diagnosis, with failed-vs-review
  semantics and copyable recovery commands in the sweep report;
- hard pre-execution solver-count budgets and request-level deduplication even
  for failed or review outcomes, preventing agents from repeating wasted work;
- opt-in, CPU-capped campaign concurrency with aggregate temporary-storage
  admission, deterministic request-order reports, collision-free run
  allocation, synchronized shared cache/history writes, and zero automatic
  retries; serial execution remains the default;
- summary-only campaign screening with distinct full-field identity, compact
  evidence retention, lower temporary-storage estimates, and no permanent H5;
- one-command, identity-gated promotion of accepted lightweight points into
  provenance-linked full-field results, with exact accepted reuse;
- preview-first reversible-by-recomputation campaign compaction that removes
  spatial bulk without H5 reads while preserving engineering/derived evidence;
- dependency-free imported STL/OBJ inspection with explicit units, SI bounds,
  named regions, topology defects, enclosed volume, and a memory guard;
- one-command imported internal-flow project creation that owns inspected
  geometry and emits explicit editable velocity, fluid, mesh, and cell-budget
  intent without manual provider-case assembly;
- content-addressed imported-surface `Model` intent with confirmed boundary
  roles, portable fingerprints, atomic inspection reports, and project asset
  presence/hash gates;
- explicit arbitrary-geometry k-omega SST intent and lowering, including a
  Cartesian turbulent inlet, blended wall functions, portable turbulence
  fields, and compact wall y-plus evidence;
- byte-verified content-addressed imported meshes shared across compatible
  operating points, with explicit acquisition provenance and storage accounting;
- first-class velocity, mass-flow, and total-pressure creation controls for
  imported laminar projects, plus compact recovered flow-rate evidence;
- discoverable `case.py` factory parameters with defaults, current selections,
  JSON-scalar override contracts, canonical units, bounds, choices, and human
  descriptions in both status and solution plans;
- reusable, strict operating-point files shared by check, plan, mesh, and run,
  with command-line trial overrides and no shadow model definition;
- JSON Schemas for status, diagnosis, logs, storage, cleanup, and repairable
  errors.

### Next: eliminate repeated setup work

1. Extend the imported-volume slice with controlled STEP/IGES tessellation,
   prism layers, automatic y-plus correction, and thermal physics; promote
   RANS only after grid and physical evidence.
2. Extend the shipped parameterized circular-elbow project pattern to tee,
   manifold, valve/porous loss, fan/pump, heated pipe, and buoyant enclosure
   workflows, building on the released mass-flow-averaged total-pressure-loss
   report.

### Then: scale without scaling attention

1. Remote/container/HPC executor protocol with the same run state contract.
2. Catalyst extraction where native full-field retention dominates I/O. Direct
   compressed HDF5 and atomic in-run rolling checkpoint publication are already
   shipped.
3. Heat/steam and conjugate-transfer workflows, then reacting flow only after
   evidence gates are satisfied.
4. Agent policy layer that may propose edits but cannot conceal assumptions,
   bypass readiness gates, or promote an unaccepted result.

## Product gates

Each release should measure: minutes of user attention to first accepted run,
percentage of failures with a direct repair action, temporary-to-final storage
amplification, rerun setup time, and number of manual OpenFOAM-file edits. A
feature that adds provider breadth but increases those costs is not complete.
