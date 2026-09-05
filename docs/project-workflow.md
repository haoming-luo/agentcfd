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
│   ├── fields/
│   │   ├── fields.xdmf
│   │   ├── fields.h5
│   │   └── manifest.json
│   └── evidence/                  small auditable provider records
├── campaigns/<run-id>/            only with --campaign
└── .agentcfd/work/<run-id>/        generated and disposable
```

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
