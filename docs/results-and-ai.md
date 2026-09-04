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
| `fields.npz` | compressed, pickle-free arrays for NumPy/PyTorch/JAX ingestion |
| `manifest.json` | units, canonical names, source names, processing, axis semantics, and hashes |

OpenFOAM native cell values and its interpolated point values are both
retained and named separately. For example, `fluid.velocity.cell` is the native
finite-volume field while `fluid.velocity.point` is the visualization-oriented
cell-to-point representation. In incompressible OpenFOAM cases,
`fluid.kinematic_pressure` remains in `m^2/s^2`; `fluid.pressure` in Pa is added
only when a positive constant density is available and the multiplication is
recorded in the manifest.

The NPZ layout is stable and does not require object deserialization:

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
point velocity and physical pressure when density is known. Native solver
restart files remain in the OpenFOAM case regardless of the portable profile,
so reducing presentation noise does not discard the authoritative solution.

Use repeated canonical selectors when a workflow needs a smaller set:

```bash
agentcfd export openfoam CASE fields --profile visualization \
  --field fluid.velocity --field fluid.vorticity
```

Transient conversion reads physical coordinates from OpenFOAM's
`case.vtm.series`. Adaptive-time-step file sequence numbers are never treated
as seconds.

The authoritative machine schemas live in `schemas/simulation-result.schema.json`,
`schemas/result-exchange.schema.json`, `schemas/scientific-sample.schema.json`,
and `schemas/field-bundle.schema.json`.
Release wheels also install them under `share/agentcfd/schemas` in the active
Python environment so non-Python consumers can discover the same contracts.
