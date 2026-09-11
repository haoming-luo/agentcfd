# Results, evidence, and AI exchange

AgentCFD treats a result as a scientific record, not a bag of solver arrays.
The native `agentcfd.simulation-result` contract and neutral
`agentcae.simulation-result` exchange record carry the same semantic layers:

- scalar quantities with canonical names, units, kinds, and descriptions;
- field records with location, components, representation, mesh identity, and
  an artifact reference;
- monotonic histories with explicit axis names and units;
- content-addressed artifacts for cases, logs, fields, tables, and plots;
- scientific inputs with a deterministic content fingerprint;
- runtime, verification, and validation claims;
- provider, model, and execution provenance.

## Acceptance and trust

`accepted` is a workflow gate: the provider completed, its numerical procedure
converged, and every mandatory check passed. `trust_level` is an evidence
classification:

| Level | Meaning |
| --- | --- |
| `not_computed` | No completed computation exists. |
| `computed` | A computation completed but did not meet convergence. |
| `converged` | Numerical convergence exists, without passed verification evidence. |
| `verified` | Verification claims passed. |
| `validated` | Validation claims against trusted physical evidence passed. |

These are intentionally separate. A result can be accepted for an exploratory
workflow while still being below a release policy's required trust level.

```python
result.require_accepted()
result.require_trust("verified")
```

Serialized results can be reopened with `read_result_record()`, or checked from
automation using `agentcfd verify result RESULT.json`. The reader recomputes
the accepted/trust state from the recorded checks and verifies every artifact's
size and SHA-256; edited evidence and hand-edited trust claims fail closed.
The parser also rejects duplicate JSON keys and non-standard `NaN`/`Infinity`
values so different downstream languages cannot interpret one record
differently.

Provider-run CLI JSON adds a non-persistent `decision` view containing only the
failed checks, their values and limits, and explicit guidance not to promote an
unaccepted result. This keeps the durable simulation-result schema stable while
giving an AI agent or GUI a small, actionable surface instead of requiring it
to infer failure from a long solver log. Human output prints the same failed
gate names after the trust state.

At project level, `agentcfd status PROJECT --json` is the control surface an
agent should poll. It exposes a finite lifecycle state, whether inputs changed,
the latest run, post-processing target, and one shell-safe next command. Expected
CLI failures requested with `--json` use `agentcfd.error/0.1` with a stable code,
plain message, repair guidance, and `safe_to_retry`; agents do not need to parse
human stderr. Active solver workspaces are protected from cleanup.

`agentcfd project PROJECT --json` is the broader one-call snapshot for an agent,
GUI, or notebook. It adds the compact result, published-file roles, standard
field-bundle paths, hidden provider-workspace state, and optional managed
storage without opening a field payload. Its bounded `next_action.operation`
separates observation, review, maintenance, and solver-starting execution; the
snapshot itself is always read-only. Python integrations use
`agentcfd.open_project(path).snapshot()` instead of importing CLI internals.

`agentcfd performance PROJECT --json` and `Project.performance()` expose a
bounded runtime-evidence record for scheduling and human expectations. The
calibration key includes provider/runtime version, capability, solver,
geometry, boundary types, Reynolds-number regime, mesh, procedure, and output
profile, but deliberately excludes field payloads. Active
`status.progress.estimated_remaining` reports how many comparable completed
runs support its range. Runtime calibration is advisory and cannot alter
`accepted`, `trust_level`, or solver settings.

At a handoff boundary, `agentcfd verify project PROJECT --json` performs the
expensive counterpart: it checks the run/result identity, hashes registered
artifacts, verifies the content-addressed solution plan, and verifies an
available field bundle internally. Its report keeps `verified`, `accepted`, and
`trust_level` distinct and records whether field payloads were opened. An agent
therefore cannot turn byte integrity into a scientific validation claim.

`agentcfd result PROJECT --json` is the matching lightweight result surface.
New runs materialize it as `output/summary.json`; the command normally reads
that bounded artifact rather than reparsing all scalar samples in
`result.json`. It returns canonical quantities,
failed checks, history metadata, field metadata, provenance, and available
names without opening HDF5 or hashing external artifacts. Repeat `--quantity`
to request only the scalar values needed by a decision. The response states
that artifact integrity is deferred and provides the exact `verify result`
command for workflows that require a full byte-level audit.
Project verification also compares every summary quantity, check, history/field
descriptor, provenance record, source byte count, and source SHA-256 with the
verified full result, so the convenient AI surface cannot silently diverge at
a handoff boundary.

Industrial component decisions should use compact semantic quantities instead
of reopening fields. An `outputs.pressure_loss("system-loss", ...)` report
publishes total-pressure loss, actual inlet bulk velocity and dynamic pressure,
and `report.system-loss.loss_coefficient`. These names are stable across the
supported OpenFOAM channel and imported-geometry providers, so an AI workflow
can rank valve, elbow, or manifold variants through `result` or campaign tables
without loading XDMF/H5. The value covers the declared inlet-to-outlet system;
AgentCFD does not silently subtract distributed straight-run friction.

Flow-distribution decisions use the same compact path. An
`outputs.flow_uniformity("outlet-quality", region="outlet")` report publishes
the vector-velocity uniformity index, signed area-normal mean velocity, and
actual surface area under stable `report.outlet-quality.*` names. It stores
the complete report cadence as small scalar histories but no additional volume
field or XDMF/H5 frame. AI workflows can therefore screen manifold branches,
heat-exchanger inlets, or combustor-entry distributions before promoting only
selected runs for spatial inspection. A high uniformity index is an observable,
not an automatic scientific acceptance claim.

For actual branch allocation, `outputs.flow_distribution(...)` publishes one
coherent `report.<name>.*` result family: inlet and outlet mass/volume flow,
per-outlet fractions, coefficient of variation, conservation error, and
optional target-fraction errors. An agent can place an explicit
`outputs.require(..., quantity="report.branch-split.maximum_fraction_error",
unit="1", maximum=...)` beside it and screen summary-only campaigns without
opening a field bundle. AgentCFD requires complete target coverage and verifies
the final signed direction at every declared port; it never takes absolute
values that could hide branch backflow.
For creation automation, the same target map and optional tolerance can arrive
through `--outlet-target` / `--maximum-fraction-error` or the strict
`project-creation-request` contract. Both routes generate ordinary Python API
calls, preserving one inspectable source of truth after the ephemeral request
has been consumed.

`outputs.require(...)` turns canonical quantities into explicit, versioned
design gates. Each criterion carries its own name, exact unit, and inclusive
minimum and/or maximum. All providers evaluate the same post-recovery logic;
missing values and unit mismatches fail closed. Criteria appear as
`kind="requirement"` checks: they participate in `accepted` but are excluded
from scientific trust classification. Consequently, a well-converged verified
run that exceeds a pressure-loss budget is `accepted=false` and still
`trust_level=verified`, accurately separating model credibility from product
fitness.

When a fitting-only value is required, solve a second straight-run baseline
whose distributed path length, section and measurement planes, wall treatment,
roughness, fluid, and operating point are genuinely equivalent, then make that
engineering assertion explicit:

```bash
agentcfd verify component-loss \
  campaigns/equipment/result.json \
  campaigns/equivalent-straight-run/result.json \
  --confirm-equivalent-baseline \
  --output output/local-loss.json
```

The verifier hashes and validates both native results, requires verified or
validated accepted sources, and checks provider/runtime capability, fluid,
study, numerical procedure, wall conditions, reference area, bulk velocity,
dynamic pressure, units, and each reported `K = Δp_t/q` identity. It reports
`K_local = K_candidate - K_baseline` and dimensionalizes it
with the candidate dynamic pressure. Candidate and baseline must be distinct
model identities. A mismatch or negative local K produces a
completed-but-unaccepted assessment; baseline equivalence is never inferred.

Without `--json`, the same scalar values are grouped as flow results, inputs,
mesh quality, verification, runtime, and other results. This presentation layer
does not rename or nest canonical quantities in the machine response; unit `1`
is shown as `[-]` so a dimensionless value is not mistaken for a missing unit.

`output.views` and `output.layouts` carry named, typed post-processing intent in
the same analysis fingerprint. After field publication,
`output/postprocess/manifest.json` maps canonical fields to their exported
point/cell arrays, portable ParaView scripts, and shared-data multi-view
layouts. An agent can list `status.postprocess.recipes` or `.layouts`, select
one by name, and call `agentcfd view . --recipe NAME --batch` or
`agentcfd view . --layout NAME --batch` for headless CSV/image/state generation;
`--launch` remains the interactive GUI path. It never needs to synthesize a
ParaView trace or duplicate the HDF5 payload.

Campaign runs copy their compact scalar quantity map into `run.json`.
`agentcfd campaigns . --json` can therefore compare accepted design points,
input fingerprints, providers, duration, and quantities without parsing solver
logs or opening `result.json` histories and HDF5 fields. The optional CSV export
flattens canonical quantity names and units for Pandas, spreadsheets, or
surrogate-model ingestion.

For the routine one-parameter decision, no plotting script or array dependency
is required:

```bash
agentcfd campaigns . \
  --plot-svg output/loss-map.svg \
  --x-parameter mean_velocity \
  --y-quantity report.system-loss.loss_coefficient
```

The matching Python API is `Project.campaign_operating_map()` for a versioned
machine record and `Project.export_campaign_operating_map()` for the portable
SVG. The horizontal axis must be a declared numeric project parameter, the
vertical axis must be a canonical scalar quantity, and units must agree across
all selected points. By default only scientifically accepted points enter the
curve. Historical points lacking an explicit x value, missing quantities, and
unaccepted results are listed under `exclusions`; none are guessed from current
factory defaults. The report's `observation_cost` proves that no result manifest
or volumetric field payload was opened. Consequently `artifact_integrity` stays
explicitly unverified; run full result verification on the selected design
before a high-consequence decision.

Design-point inputs are explicit factory keyword arguments, not text edits.
`check`, `plan`, and project `run` accept repeatable `--param NAME=VALUE`; the
normalized JSON scalars are recorded beside the content-addressed analysis.
This gives an agent a narrow mutation surface while Python type/model validation
continues to reject physically invalid values.

`agentcfd sweep` adds a versioned request/report boundary around this surface.
It preflights the complete set, reuses only accepted results with the exact
result-execution fingerprint, writes progress atomically after each point, and
continues after isolated runtime failure unless fail-fast was explicit. It does
not infer parameter names, weaken provider gates, or read field payloads to
decide reuse.

`sweep --plan-only --json` is the non-mutating agent decision surface. It also
distinguishes reuse from an existing campaign versus an identical point later
in the same request, so estimated solver starts are not inflated by duplicate
inputs.

For exploration, `sweep --summary-only` keeps the same quantities, checks,
histories, logs, and provenance while omitting permanent field payloads. Its
distinct result fingerprint prevents downstream agents from assuming that a
summary-only design point can satisfy a full-field request. This supports a
two-stage loop: screen many points cheaply, then rerun the small promoted set
with standard XDMF/HDF5 output for visual review, AgentFEM exchange, or learned
field workflows.

`agentcfd promote . <run-id> --json` is the explicit transition between those
stages. It is fail-closed on changed model intent, a rejected source, or missing
portable-I/O capability, and its versioned report states whether a full-field
identity was executed or reused and whether any solver process started.

`agentcfd compact . <run-id> --json` is the inverse storage transition and is
preview-only unless `--apply` is explicit. It removes regenerable spatial bulk
without reading HDF5, preserves compact engineering and derived evidence, and
updates result identity so an agent can no longer mistake the point for a
field-bearing sample.

## AgentFEM and AI continuity

There is no runtime dependency on AgentFEM. Instead, `to_sample()` emits numeric
`inputs` and `outputs` that map directly to AgentFEM's `datasets.Sample`, plus a
quantity schema retaining physical meaning:

```python
sample = result.to_sample(
    case_id="pipe-water-001",
    inputs={"diameter": 0.05, "mean_velocity": 0.02},
    outputs=("flow.pressure_drop", "flow.mass_flow_rate"),
)
```

The project-level path removes the remaining integration glue and enforces the
trust boundary before writing:

```bash
agentcfd export sample . dataset/pipe-water-001.json \
  --input diameter \
  --input mean_velocity \
  --output-quantity flow.pressure_drop \
  --output-quantity flow.mass_flow_rate \
  --case-id pipe-water-001
```

`Project.scientific_sample()` provides the same record in Python. Inputs are
numeric parameters declared by the readable `case.py` factory; if `--input` is
omitted, all numeric parameters are included. Outputs must be explicit stable
quantity names. The export runs full project verification first and refuses an
unaccepted result, integrity drift, unknown/non-numeric input, unknown output,
or an existing target file. Summary-only campaign points are therefore the
cheap natural source for large scalar datasets, while full-field runs remain
available when spatial learning is actually required.

An accepted campaign becomes one atomic, streamable dataset without a notebook:

```bash
agentcfd export dataset . datasets/pressure-map \
  --input diameter \
  --input mean_velocity \
  --output-quantity flow.pressure_drop
```

The directory contains `manifest.json` and `samples.jsonl`. Every JSON Line is
an independent `agentcae.scientific-sample/0.1.0`; the manifest fixes the common
input/output schema, maps line numbers to run and result hashes, records the
JSONL byte count and SHA-256, and preserves every excluded unaccepted point with
its reason. Each included run passes full project verification first. Output is
renamed into place only after every sample agrees semantically, so a failed
campaign point or interrupted export cannot leave a plausible partial dataset.
`agentcfd verify dataset DIRECTORY --json` reopens no solver or field payload;
it verifies the manifest, counts, JSONL byte length and SHA-256, every sample's
contract and shared input/output metadata, line-to-run identity, and uniqueness.

`open_scientific_dataset(DIRECTORY)` is the matching dependency-free Python
consumer. It fails closed unless the package verifies, exposes stable input and
output order, streams strict sample records, and returns raw-unit immutable
`X/Y` matrices. `to_numpy()` is an explicit adapter from the optional `arrays`
extra. `agentcfd dataset inspect DIRECTORY --preview 3` emits a compact,
versioned view of shapes, units, ranges, trust-level counts, payload identity,
and bounded rows; it never imports a solver or opens XDMF/HDF5 fields.

The same record can be ingested by campaign managers, tabular surrogate tools,
or user ML pipelines without importing PyTorch or JAX into the core package.
Field-learning workflows should consume `field_records` and their artifacts;
they must also retain mesh identity, tensor layout, masks, normalization, and
units. AgentCFD will not silently flatten a field and discard those semantics.

## Standard portable field bundle

AgentCFD uses one `agentcae.field-bundle/0.1.0` contract across visualization,
AgentFEM transfer, and field-learning pipelines. One directory contains:

| Artifact | Role |
| --- | --- |
| `fields.xdmf` | lightweight topology, field, association, and time-series index |
| `fields.h5` | binary geometry, topology, and field payloads referenced by XDMF |
| `fields.npz` | optional compressed, pickle-free mirror for NumPy/PyTorch/JAX ingestion |
| `manifest.json` | units, canonical names, source names, processing, axis semantics, source-mesh identity, and hashes |

OpenFOAM native cell values and its interpolated point values are both
retained and named separately. For example, `fluid.velocity.cell` is the native
finite-volume field while `fluid.velocity.point` is the visualization-oriented
cell-to-point representation. In incompressible OpenFOAM cases,
`fluid.kinematic_pressure` remains in `m^2/s^2`; `fluid.pressure` in Pa is added
only when a positive constant density is available and the multiplication is
recorded in the manifest.

The standard visualization bundle is only XDMF/H5. Request the stable NPZ
layout explicitly when an array or learning workflow needs it:

```bash
agentcfd export openfoam CASE fields --with-npz
```

The NPZ layout does not require object deserialization:

```python
import json
import numpy as np

with np.load("fields/fields.npz", allow_pickle=False) as data:
    metadata = json.loads(str(data["metadata_json"]))
    axis = data["axis"]
    points = data["points"]
    cells = data["cells__0__hexahedron"]
    velocity = data["cell__fluid_velocity_cell__0"]
```

For a single field/frame, AgentCFD can emit the exact four-key NPZ shape used
by AgentFEM's dependency-free `FEMFieldSample` reader:

```bash
agentcfd export field-sample fields velocity-final.npz \
  --field fluid.velocity --association point --frame -1
```

The output keys are `coordinates`, `values`, `encoding_json`, and
`metadata_json`. Point fields retain mesh vertices; cell fields use explicit
cell-centre coordinates. Units, component names, interpolation history,
source identity, selected axis coordinate, and the parent bundle hash remain
attached rather than being inferred by a training script.

The manifest says whether the axis is physical time, a steady-solver iteration,
or an unclassified provider coordinate. Consumers must not infer seconds from
an XDMF `Time` element alone. `agentcfd verify field-bundle` reopens XDMF/H5 and
NPZ, compares their axes and geometry, and verifies all artifact hashes.
Accepted OpenFOAM project exports also bind every portable `FieldRecord` and the
HDF5 metadata to the exact source `polyMesh` SHA-256. A default cleaned project
records the provider case as intentionally unretained instead of publishing a
dangling absolute workspace path.

### Output profiles

AgentCFD separates three different reasons for storing a field instead of
presenting cell and point copies as peers to every user:

| Profile | Association | Normal use |
| --- | --- | --- |
| `visualization` | interpolated point fields | ParaView, reports, ordinary inspection |
| `native` | OpenFOAM cell fields | finite-volume audit, coupling, training data |
| `both` | point and cell fields | explicit expert interchange and debugging |

The CLI defaults to `visualization`. A project takes its canonical field list
and profile from `OutputRequest`; `outputs.standard()` therefore publishes only
point velocity and physical pressure when density is known. For the baffled
channel workflow, native solver restart state is repacked into the
content-addressed `evidence/restart.zip` independently of the portable profile.
The disposable OpenFOAM workspace can therefore be removed without discarding
the declared rolling continuation state.

Use repeated canonical selectors when a workflow needs a smaller set:

```bash
agentcfd export openfoam CASE fields --profile visualization \
  --field fluid.velocity --field fluid.vorticity
```

Transient conversion reads physical coordinates from OpenFOAM's
`case.vtm.series`. Adaptive-time-step file sequence numbers are never treated
as seconds. Solver time step and portable write interval remain separate:
smaller write intervals improve animation detail without changing field
association or forcing an NPZ copy.

### Retention, compression, and budgets

Full-field frames are separate from solver timesteps, scalar histories, and
native restart checkpoints. `outputs.animation(...)` declares physical frame
cadence and a maximum count; `outputs.checkpoints(...)` declares sparse rolling
restart intent; `outputs.storage(...)` declares a fail-closed byte budget and
HDF5 compression. `agentcfd plan` reports the resolved channels and estimated
temporary peak before execution.

Two completed, otherwise matched transient results can be screened with
`verification.time_step_sensitivity(...)` or
`verification.time_step_sensitivity_from_result_records(...)`. The returned
record deliberately calls itself pairwise sensitivity: two time-step levels
can measure change, but cannot establish observed temporal order or numerical
uncertainty.

For agents and CI, the same contract is available without importing Python:

```bash
agentcfd verify time-step-sensitivity coarse/result.json fine/result.json \
  --quantity report.baffle-drag --maximum-relative-change 0.02 \
  --output time-step-sensitivity.json --json
```

Exit status is `0` inside the declared limit and `3` for a completed but
unaccepted comparison. The evidence file includes both source paths and hashes.

Numeric HDF5 datasets use chunked gzip compression by default. The field-bundle
manifest records the conservative preflight estimate, uncompressed bytes per
frame, selected compression, budget, and actual portable bytes. See
[output architecture](output-architecture.md) for the complete contract.

The authoritative machine schemas live in `schemas/simulation-result.schema.json`,
`schemas/result-exchange.schema.json`, `schemas/scientific-sample.schema.json`,
`schemas/field-bundle.schema.json`, and
`schemas/time-step-sensitivity.schema.json`. Project automation additionally
uses `schemas/project-status.schema.json`, `schemas/project-storage.schema.json`,
`schemas/project-clean.schema.json`, `schemas/project-view.schema.json`,
`schemas/project-snapshot.schema.json`, `schemas/project-verification.schema.json`,
`schemas/project-performance.schema.json`,
`schemas/performance-history.schema.json`, and `schemas/error.schema.json`.
Release wheels also install them under `share/agentcfd/schemas` in the active
Python environment so non-Python consumers can discover the same contracts.
