# Discovery and safe automation

AgentCFD exposes one dependency-free discovery record for people, scripts,
GUIs, and AI agents. A client should inspect this record instead of scraping
help text, importing provider internals, or guessing which operation is safe.

```bash
agentcfd capabilities --json
```

The validated `agentcfd.capabilities/0.2` response contains:

- the installed product version and recommended core workflow;
- progressively disclosed public Python modules and facade methods;
- the exact CLI inventory and preferred machine commands;
- project workflow stages;
- provider families, license/execution boundaries, variants, and accepted
  operational options;
- bounded product operations with side-effect and risk metadata;
- scientific capabilities with evidence, limitations, and maturity.

Python clients can use `agentcfd.public_api("core")`, `"advanced"`, or
`"expert"` for the same progressive module inventory. The core tier is the
normal user workflow; provider and contract internals remain available without
being placed in the first-use path.

## Provider contract

`provider_catalog` describes the analytical reference and external OpenFOAM
families. OpenFOAM remains a filesystem-and-subprocess boundary, so its GPL
runtime is not linked into the Apache-2.0 Python core. Its option contract names
each accepted key, value type, effect, and meaning. Unknown, mistyped, or
non-finite options fail before solver execution with stable issue codes.

Project checking, planning, running, and resuming call the same provider
resolver. A frontend must not implement a second provider-selection algorithm.
The family contract is static product discovery; runtime availability still
comes from `agentcfd doctor --json` and project compatibility still comes from
`agentcfd check PROJECT --json`.

## Bounded operation contract

`operation_catalog` is the small product-wide verb set suitable for an agent or
future MCP adapter. Each record declares:

- a stable name and exact AgentCFD command pattern;
- `read`, `write`, or `execute` effect;
- idempotence and destructive behavior;
- explicit automatic-retry and user-approval policy;
- whether it can start a solver;
- whether a preview exists;
- expected durable artifact roles;
- the structured output contract when the command directly returns one;
- conservative MCP-compatible annotations.

The catalog contains no arbitrary shell and no arbitrary Python execution.
Destructive cleanup has a separate read-only preview operation. Solver starts
are distinguishable from inspection, and a null `output_contract` means that a
command returns a documented CLI view rather than falsely claiming conformance
to a narrower stored-result schema.

The static operation catalog answers *what can this product do?* For one
existing project, use:

```bash
agentcfd actions PROJECT --json
```

That state-aware report answers *what is available now?* and adds current
availability, reasons, cost, side effects, and one recommended action. A GUI or
agent should combine static risk metadata with this dynamic project state, and
must still respect normal user approval policy for writes, solver execution,
and destructive application.

## Minimum machine journey

```text
capabilities -> templates -> init -> check -> plan
             -> run -> status/result -> verify
```

Every arrow has a declared machine command in the capability record. `check`
and `plan` do not start a solver. `run` is the explicit execution boundary.
`result` is a compact decision surface that does not load HDF5 arrays, while
`verify project` performs the more expensive identity and artifact-integrity
handoff check.

The contracts are shipped inside the wheel as
`capability-catalog.schema.json`, `provider-catalog.schema.json`, and
`operation-catalog.schema.json`. Installed-wheel tests verify that discovery
still works without NumPy or a local OpenFOAM runtime.
