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

`agentcfd.operations.argv()` renders one catalog operation directly to an argv
tuple. It requires every declared placeholder, rejects undeclared arguments,
and never invokes a shell. This is the preferred boundary for a GUI, agent
adapter, or test harness that needs to execute an approved operation.

## Optional extensions

```bash
agentcfd extensions --json
```

Extension discovery reads Python distribution and entry-point metadata only.
It does not import the referenced module. The supported API-v1 groups are:

- `agentcfd.providers.v1`
- `agentcfd.exporters.v1`
- `agentcfd.properties.v1`
- `agentcfd.learning.v1`

An extension package declares a normal entry point, for example:

```toml
[project.entry-points."agentcfd.learning.v1"]
physicsnemo = "agentcfd_learning_physicsnemo:extension"
```

Versioning the group makes compatibility visible without custom package
metadata or importing code. Unversioned legacy groups are reported as
incompatible. Duplicate group/name registrations also fail closed. Only an
explicit `agentcfd.extensions.load(kind, name)` imports code; the loaded object
must then return this minimal descriptor from a callable `descriptor()`:

```python
{
    "schema": "agentcfd.extension/1",
    "kind": "learning",
    "name": "physicsnemo",
    "capabilities": ["learning.physicsnemo"],
}
```

The core establishes discovery and compatibility, not automatic installation
or privileged execution. The reference `extensions/agentcfd-learning` package
is the first real implementation: it prepares leakage-safe, framework-neutral
batches from verified scalar campaign datasets. Provider/exporter/property/learning
integration protocols can evolve behind these separate namespaces without
making their runtimes mandatory core dependencies.

## Project compatibility

```bash
agentcfd compatibility PROJECT --json
```

This read-only command parses `agentcfd.toml` without importing `case.py`. It
distinguishes current, legacy, newer, foreign, malformed, and missing schemas,
reports whether the project can be opened, and never rewrites a manifest. A
migration is advertised only after an explicit reviewed migrator exists.

## MCP manifest

```bash
agentcfd mcp-manifest --json
```

The dependency-free manifest derives catalog resources, project resource
templates, typed tool input schemas, annotations, approval/retry policy, and
expected artifacts from the existing operation catalog. It deliberately
declares `transport: null` and does not start a server or execute a tool. A thin
MCP transport package can consume this manifest; it must enforce host approval
and dispatch only through the bounded argv renderer. The independent
`extensions/agentcfd-mcp` reference package does this over local stdio: paths
remain under configured roots, mutation and execution are disabled by default,
and operations run in separate processes. Arbitrary shell and arbitrary Python
remain absent.

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
`operation-catalog.schema.json`, together with `extension-catalog.schema.json`
and `mcp-manifest.schema.json`. Installed-wheel tests verify that discovery
still works without NumPy, an MCP dependency, or a local OpenFOAM runtime.
