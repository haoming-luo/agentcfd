# Project workflow and storage policy

An AgentCFD project represents one readable engineering simulation, not one
OpenFOAM filesystem. The ordinary user edits `case.py`, runs the project, and
opens `output/fields/fields.xdmf` or `output/result.json`.

```text
my-flow/
├── case.py
├── agentcfd.toml
├── input/                         optional user-owned inputs
├── output/                        current published run
│   ├── plan.json
│   ├── summary.json               small default decision/result surface
│   ├── result.json                full scalar histories and evidence index
│   ├── run.json
│   ├── README.md                  human start-here guide
│   ├── fields/
│   │   ├── fields.xdmf
│   │   ├── fields.h5
│   │   └── manifest.json
│   └── evidence/                  small auditable provider records
├── campaigns/<run-id>/            only with --campaign
└── .agentcfd/work/<run-id>/        generated and disposable
```

## One decision surface

The primary interaction is deliberately shorter than the underlying CFD
pipeline:

```bash
agentcfd status .
agentcfd run .
agentcfd view .
```

Before a project exists, `agentcfd templates` is the discovery surface. It
states provider compatibility, generated versus imported geometry, physics,
default outputs, required user decisions, scientific limitations, and a
copyable `init` command. `agentcfd templates --json` carries the same versioned
catalog to agents and frontends; `init` derives its accepted names and default
provider from that catalog rather than maintaining a second runtime list. Each
template links the installed creation-request schema and declares its required,
optional, and exactly-one fields; CI checks the unavoidable static JSON Schema
enumerations against the runtime catalog.

`agentcfd project . --json` is the unified integration view over the same
lifecycle. It returns the status, compact current result, named published
files, XDMF/H5 entry points, provider-workspace retention, and a typed
`next_action.operation` in one bounded read. Add `--storage` only when recursive
byte accounting is needed, or `--no-result` for the smallest control-plane
poll. The Python equivalent is:

```python
from agentcfd import open_project

snapshot = open_project(".").snapshot(include_storage=False)
```

Snapshots do not execute the returned action, open field payloads, launch a
viewer, or expose an ordinary user to generated OpenFOAM dictionaries.

Every completed run materializes the same bounded result view as
`output/summary.json`. Normal `result` and `project` reads use that small file
and leave the much larger complete history in `result.json` closed. The summary
records the source result's byte count and SHA-256, but honestly marks external
artifact integrity as unverified; `verify` remains the explicit expensive trust
boundary. Older projects without `summary.json` continue to work by deriving
the view from `result.json`.

Use `agentcfd verify project .` at an explicit trust boundary such as handoff,
archive, coupling, or training-data admission. It verifies the atomic run
record and content-addressed `plan.json` against `result.json`, proves that
`summary.json` exactly matches that full record, hashes every registered result
artifact, and—if present—opens XDMF/H5 to compare hashes, schema identity, mesh
identity, and frame axes. A legacy 0.1.0a3 OpenFOAM
analysis identity is accepted only when it can be recomputed from that verified
plan. Verification reports integrity separately from `accepted` and
`trust_level`; it never upgrades the scientific evidence.

Use `agentcfd doctor .` when a full project/runtime/resource audit is worth a
recursive storage scan. It checks model/provider/runtime readiness, output
budget, filesystem headroom, latest run health, acceptance, and recovery, then
returns the same single next action. Its cell-update proxy supports relative
cost comparison; it explicitly reports energy as unmeasured unless an executor
provides hardware telemetry.

`status` reports `blocked`, `ready`, `running`, `interrupted`, `modified`,
`complete`, `review`, or `failed`, then gives exactly one recommended command.
Use `--json` for the versioned machine contract and `--storage` when a recursive
space scan is worth the extra latency. `check`, `plan`, and `inspect` remain
available as drill-down tools rather than required ceremony.

While OpenFOAM runs, provider output is streamed to its workspace log instead
of accumulated in process memory. `status` reads at most 256 KiB from the active
log plus the latest rows of compact function-object tables. It reports the
current command, physical time or steady iteration, latest residuals, Courant
number, live mass imbalance and pressure drop, elapsed time, and a deliberately
wide ETA range for transient time advancement. It never opens a volume field.
Workspace byte accounting remains opt-in through `status --storage` because a
recursive scan itself can become expensive on very large cases.

Every completed managed run also contributes one tiny record to
`.agentcfd/performance.json`. The file is capped at 50 samples and never stores
solver fields. `agentcfd performance .` groups only comparable provider,
solver, geometry, mesh, procedure, and output profiles; `status` and `watch`
prefer that evidence over linear extrapolation when available. A missing,
damaged, or unwritable calibration file never blocks a simulation or weakens a
scientific acceptance gate.

`agentcfd watch .` follows this contract at a two-second default cadence and
exits automatically when the project leaves `running`. Human output is one
compact append-only line per sample; `watch --json` emits JSON Lines for an AI
agent, service, or log collector. The active state's recommended next action is
therefore `watch`, so waiting does not require repeated manual commands.

When a provider reports failure, AgentCFD preserves that run's generated
workspace automatically. `status` recommends `agentcfd diagnose .` whenever a
log exists. Diagnosis opens no field payload and scans at most 256 KiB from up
to eight newest phase logs. Known storage, memory, missing-file, boundary,
dictionary, mesh, numerical, MPI, timeout, and process failures become stable
codes with an evidence line, confidence, conservative repair, and one next
command. It never edits `case.py` or generated provider files automatically.

`agentcfd logs .` is the raw drill-down. It reads at most 1 MiB and returns only
the requested final lines (80 by default), so a person or agent can inspect the
decisive evidence without loading a multi-gigabyte case or guessing a backend
path. Use `logs --command pimpleFoam`, for example, when several phase logs
exist; `logs --json` carries the available commands, source, truncation state,
and one safe retry action. Successful runs still remove disposable native bulk
after copying small logs to `output/evidence/`.

Campaign diagnosis is run-scoped rather than “latest-run” scoped. Every failed
sweep row carries its immutable `run_id`, result directory, and a ready-to-copy
`diagnose_command`. Use `agentcfd diagnose . --run-id <id>` or
`agentcfd logs . --run-id <id>` after later points have completed; selecting a
missing id fails explicitly instead of silently inspecting another run.

## Checkpoint recovery

Transient full-field frames are for interpretation; checkpoints are sparse
native solver state for recovery. The default baffle template keeps two rolling
states every `0.5 s`. If a run fails or its owner process disappears,
`status.recovery` reports whether the latest complete `U` and `p` state is
usable. Diagnosis keeps its immediate repair as the one next action and exposes
`resume_after_repair` separately.

`agentcfd resume .` is fail-closed. It currently supports the transient
OpenFOAM channel capability in replace mode and requires the current analysis
fingerprint and execution fingerprint to equal the source run. AgentCFD checks
the generated case identity, checkpoint metadata and every restored archive
member, stages the checkpoint before replacing `output/`, changes only
`startFrom` to `latestTime`, and skips repeated potential-flow initialization.
The new result records source run ID, checkpoint time, and archive hash.
Compact function-object histories travel in the restart archive as well, so a
terminal checkpoint can still satisfy conservation and requested-report gates
without re-reading discarded native field frames. An exact end-time checkpoint
is valid for termination/publication recovery even when the interrupted log
did not have time to print its final `End` marker.

Execution timeout, workspace retention, and portable-export enablement are
publication/operational controls, not solver state. They may change before
resume (for example, increasing `timeout_seconds` after a diagnosed timeout);
container identity, mesh controls, analysis inputs, and all other
solver-affecting settings must remain identical.

Cleanup cannot remove a workspace that contains the only complete recovery
checkpoint. After successful resumed publication, the superseded source
workspace is removed automatically; failed resume attempts retain both sources
for diagnosis. This trades a small, explicit checkpoint cost for avoiding a
full recomputation.

The recovery path was exercised against OpenCFD v2606 with a deliberately
one-second-limited 3,020-cell channel. The first run published
`PROCESS_TIMED_OUT` plus 0.45/0.5 s checkpoints; resuming the complete 0.5 s
state performed only OpenFOAM termination confirmation and produced a verified,
accepted result with source-run provenance.

`view` reads only the small field manifest before opening anything. It reports
frame count, physical/iteration axis range, portable size, canonical variables,
and whether each variable is a visualization point field or native cell field.
`view --launch` opens the XDMF in ParaView and can discover a macOS ParaView App
even when `paraview` is absent from `PATH`.

Named view recipes in `case.py` publish beneath `output/postprocess/` and share
the same XDMF/HDF5 payload. `agentcfd view . --recipe NAME --launch` starts the
generated ParaView script for an interactive visual pipeline; no derived copy
of the volume fields is stored. For agents, CI, and remote machines,
`agentcfd view . --recipe NAME --batch` runs the same text recipe through
`pvbatch`, waits for completion, and reports the output paths it actually found.
It never starts the desktop GUI. Several named render recipes can be composed
with `outputs.render_layout()` and selected through
`agentcfd view . --layout NAME --launch|--batch`; the layout opens the portable
field source once and reports its generated PNG/PVSM without copying HDF5. See
[post-processing recipes](postprocessing-recipes.md).

The CLI, `Project.discover()`, and public `open_project()` resolve the nearest
`agentcfd.toml` upward
from a file or directory. A user inspecting `output/fields/` therefore does not
need to remember the project root before checking status, rerunning, cleaning,
or opening the result; generated next actions remain `agentcfd … .` while the
working directory stays anywhere inside that project.

## Ordinary replace mode

`agentcfd run .` publishes to the stable `output/` path. A later run replaces
that directory only when its existing `run.json` proves AgentCFD owns it. An
unmarked non-empty directory fails closed, so a mistaken configuration cannot
erase user files.

The solver workspace is generated from the public model. After successful
field conversion, AgentCFD copies provider logs, case identity, mesh identity,
and compact report evidence into `output/evidence/`, then removes the generated
OpenFOAM time directories and temporary VTK conversion. XDMF/HDF5 is the
ordinary portable field product; NPZ remains explicit opt-in.

The output request in `case.py` also owns an explicit storage policy. Before
execution, `agentcfd plan` resolves full-field frame count and estimates both
the final portable bundle and the temporary provider peak. Oversized requests
fail before solver work starts. See [output architecture](output-architecture.md)
for animation, checkpoint, compression, and budget examples.

`agentcfd storage .` accounts separately for the replaceable current result,
intentional campaigns, and hidden temporary workspaces. `agentcfd clean .` is a
non-destructive preview. `agentcfd clean . --apply` can remove only hidden
temporary solver workspaces; it always preserves `output/` and `campaigns/`.
If a failed workspace holds the only complete recovery checkpoint, it is also
protected and excluded from reclaimable bytes.

Workspaces retained deliberately by `--keep-workspace` or
`[openfoam].keep_workspace = true` are a separate protected class. Normal
cleanup never deletes them. Preview their release with
`agentcfd clean . --include-retained`, then repeat with `--apply` only when the
listed paths are no longer needed. Active workspaces and sole recovery
checkpoints remain protected even in this expanded scope.

The OpenFOAM-to-XDMF adapter passes selected native times and field names to
`foamToVTK`, so a sparse public animation no longer requires staging every
restart time or unused solver field as temporary VTK data.

## Campaign mode

`agentcfd run . --campaign` publishes to `campaigns/<run-id>/` and never
replaces the current `output/`. This mode is for parameter studies, validation
matrices, training-data generation, and design histories—not for every edit-run
cycle.

`agentcfd campaigns .` turns those immutable runs into a compact design-point
index. It reads run markers and small plan summaries only, exposes acceptance,
trust, input identity, duration, and canonical scalar quantities, and records
that it opened zero result manifests and field payloads. Add `--storage` only
when per-run recursive size is worth the I/O. Use
`--export-csv design-points.csv` for a flat table whose quantity headers retain
units; no solver or HDF5 reader is invoked.

For a direct operating curve, add `--plot-svg`, `--x-parameter`, and
`--y-quantity`. AgentCFD reads the same compact markers, labels both axes with
declared units, and connects only accepted points with distinct x values.
Unaccepted points are omitted by default; `--include-unaccepted` shows them as
separate warning markers without promoting them into the accepted curve. Runs
that did not explicitly record the selected x parameter are reported as
excluded rather than filled from today's `case.py` defaults.

Projects may expose design variables directly in the readable factory:

```python
def build(*, mean_velocity=0.5, baffle_height=0.12):
    # These ordinary Python values construct the same typed model as before.
    ...
```

Then `agentcfd plan . --param mean_velocity=0.8` previews that exact design
point and `agentcfd run . --campaign --param mean_velocity=0.8` preserves it.
Each repeatable `--param NAME=JSON_SCALAR` is passed as a named factory
argument, included in the plan fingerprint and run marker, and flattened into
the campaign CSV. Misspelled or unsupported names fail through the factory
signature before meshing. This keeps one language and one model source of truth;
AgentCFD does not patch arbitrary Python source or maintain a shadow YAML model.

When an operating point is worth naming or handing to another person or agent,
put the same scalar overrides in a portable file:

```json
{
  "schema": "agentcfd.parameter-set/0.1",
  "parameters": {"mean_velocity": 0.8, "baffle_height": 0.1}
}
```

Pass it to `check`, `plan`, `mesh`, or `run` with `--param-file`. Command-line
`--param` values take precedence, so a baseline file remains reusable during a
small trial. The selected merged values enter the same plan and run identities;
the parameter file never becomes a second model definition.

To avoid authoring that envelope by hand, use
`agentcfd params . --param mean_velocity=0.8 --output v08.json`. The command
executes the project factory validation, fills every other editable input from
its current Python default, validates the installed parameter-set contract, and
refuses to overwrite an existing snapshot. `Project.parameter_set()` provides
the same dependency-free Python API.

For repeatable studies, a request contains names plus only those factory
parameters:

```json
{
  "schema": "agentcfd.campaign-request/0.1",
  "points": [
    {"name": "v05", "parameters": {"mean_velocity": 0.5}},
    {"name": "v07", "parameters": {"mean_velocity": 0.7}}
  ]
}
```

`agentcfd sweep . sweep.json` plans every point before executing any. A bad
name, invalid model, unsupported physics, missing runtime, or exceeded output
budget aborts the whole preflight without a partial campaign. During execution,
an already accepted identical result fingerprint is reused, including duplicate
points inside one request. Runtime failures are recorded and later points
continue by default; `--fail-fast` changes only that execution policy. The
small atomic `campaigns/last-sweep.json` makes interruption and automation
observable. Version 0.1 executes serially so a campaign cannot oversubscribe
memory or temporary storage before a bounded resource scheduler exists.
Solver-level failed results count as `failed`; a completed result that did not
meet acceptance checks counts as `review`, so automation does not confuse a
runtime failure with an engineering decision gate.

Use `agentcfd sweep . sweep.json --plan-only` as the approval boundary. It
reports how many points are ready, already reusable, duplicated within the
request, and would actually start a solver; the contract records
`solver_processes_started: 0`. Reuse requires the exact accepted result identity
and a still-present compact `result.json`; a stale success marker beside deleted
results never suppresses recomputation.

For unattended or agent-triggered work, add `--max-runs N`. AgentCFD computes
the number of unique new solver executions after accepted-cache reuse and
aborts the complete request before starting a process if the explicit limit is
exceeded. The final report records the limit, planned new runs, and actual
processes started. Equivalent points inside one request never execute twice,
including when the first execution fails or reaches review rather than
acceptance; they share the same immutable evidence while retaining their own
design-point names in the sweep table.

Use `--summary-only` for broad operating maps where scalar quantities,
acceptance checks, logs, and provenance are sufficient. OpenFOAM still writes
the native states required during the solve, but AgentCFD skips VTK conversion,
XDMF/HDF5 publication, view recipes, and permanent provider-native fields, then
removes disposable bulk after publishing compact evidence. The plan reports a
lower temporary-storage estimate and does not require optional portable-I/O
packages. Summary-only and full-field runs have different result fingerprints,
so an accepted lightweight point cannot masquerade as an animation-ready one;
promote only a selected candidate with `agentcfd promote . <run-id>`. Promotion
requires an accepted immutable summary result, verifies that current `case.py`
still has the same analysis fingerprint, forces standard portable fields, and
records the source run in the new result provenance. An already accepted exact
full-field identity is reused without starting another solver.

Existing accepted full-field campaign points can be slimmed with
`agentcfd compact . <run-id>`. The default is a non-mutating inventory of exact
managed targets, bytes, and files; `--apply` is required to rewrite the point
as summary-only and remove reproducible volume fields plus recipes that would
otherwise reference them. Compact quantities, checks, histories, run metadata,
logs/evidence, and independently generated CSV/PNG/MP4 products are preserved.
Compaction never opens the HDF5 payload and fails closed if the current model or
OpenFOAM result settings cannot reproduce the source identity. Because the
rewritten point has the canonical summary fingerprint, `promote` can later
regenerate a provenance-linked full-field result.

## Expert workspace retention

`agentcfd run . --keep-workspace` retains the generated backend below
`.agentcfd/work/<run-id>/openfoam`. The same behavior can be selected in
`agentcfd.toml` with `keep_workspace = true` under `[openfoam]`. This is a
debugging and provider-development surface; changing it does not change the
declared scientific intent in `case.py`.

Failure retention is automatic and does not require this option. After the
cause is repaired and a fresh or resumed replace-mode run completes, stale
failed workspaces become reclaimable through the normal preview-first `clean`
flow.

Explicit retention persists as user intent across later commands. It does not
silently become ordinary reclaimable cache; use the dedicated
`--include-retained` cleanup scope to revoke that intent.
