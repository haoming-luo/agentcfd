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
│   ├── result.json
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
It never starts the desktop GUI. See
[post-processing recipes](postprocessing-recipes.md).

The CLI and `Project.discover()` resolve the nearest `agentcfd.toml` upward
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
