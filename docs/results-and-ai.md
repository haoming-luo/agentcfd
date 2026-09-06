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

`output.views` carries named, typed post-processing intent in the same analysis
fingerprint. After field publication, `output/postprocess/manifest.json` maps
canonical fields to their exported point/cell arrays and portable ParaView
scripts. An agent can list `status.postprocess.recipes`, select one by name, and
call `agentcfd view . --recipe NAME --batch` for headless CSV/image/state
generation or use `--launch` for an interactive GUI. It never needs to
synthesize a ParaView trace or duplicate the HDF5 payload.

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
| `manifest.json` | units, canonical names, source names, processing, axis semantics, and hashes |

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
`schemas/project-clean.schema.json`, `schemas/project-view.schema.json`, and
`schemas/error.schema.json`.
Release wheels also install them under `share/agentcfd/schemas` in the active
Python environment so non-Python consumers can discover the same contracts.
