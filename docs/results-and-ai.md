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

The authoritative machine schemas live in `schemas/simulation-result.schema.json`,
`schemas/result-exchange.schema.json`, and `schemas/scientific-sample.schema.json`.
Release wheels also install them under `share/agentcfd/schemas` in the active
Python environment so non-Python consumers can discover the same contracts.
