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

`view` reads only the small field manifest before opening anything. It reports
frame count, physical/iteration axis range, portable size, canonical variables,
and whether each variable is a visualization point field or native cell field.
`view --launch` opens the XDMF in ParaView and can discover a macOS ParaView App
even when `paraview` is absent from `PATH`.

Named view recipes in `case.py` publish beneath `output/postprocess/` and share
the same XDMF/HDF5 payload. `agentcfd view . --recipe NAME --launch` starts the
generated ParaView script for a slice, contour, or streamline pipeline; no
derived copy of the volume fields is stored. See
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

The OpenFOAM-to-XDMF adapter passes selected native times and field names to
`foamToVTK`, so a sparse public animation no longer requires staging every
restart time or unused solver field as temporary VTK data.

## Campaign mode

`agentcfd run . --campaign` publishes to `campaigns/<run-id>/` and never
replaces the current `output/`. This mode is for parameter studies, validation
matrices, training-data generation, and design histories—not for every edit-run
cycle.

## Expert workspace retention

`agentcfd run . --keep-workspace` retains the generated backend below
`.agentcfd/work/<run-id>/openfoam`. The same behavior can be selected in
`agentcfd.toml` with `keep_workspace = true` under `[openfoam]`. This is a
debugging and provider-development surface; changing it does not change the
declared scientific intent in `case.py`.

Failure retention is automatic and does not require this option. After the
cause is repaired and a new replace-mode run completes, stale failed
workspaces become reclaimable through the normal preview-first `clean` flow.
