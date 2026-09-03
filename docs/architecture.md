# Architecture

AgentCFD separates four responsibilities:

1. **Engineering language** — fluid studies, domains, materials, boundaries,
   sources, procedures, outputs, and observations.
2. **Scientific contracts** — validation issues, capability records, canonical
   field semantics, provenance, and acceptance checks.
3. **Provider lowering** — translation of a resolved model into a numerical
   engine without leaking engine-specific files into the public model.
4. **Deterministic execution** — assembly, nonlinear iteration, time advancement,
   parallel execution, restart, and field recovery performed by the selected engine.

An AI agent is not a mandatory runtime dependency. Any agent may construct and
inspect the same model that a person writes in Python. This keeps rapid progress
in AI models independent from numerical solver evolution.

## Provider strategy

The core remains Apache-2.0 and communicates with numerical providers through a
small protocol. The initial executable provider is an in-process analytical
reference. Planned numerical providers are admitted only when they have:

- a precise capability matrix;
- deterministic lowering from public model objects;
- executable golden cases;
- residual and conservation recovery;
- version and runtime fingerprints;
- explicit unsupported-case errors.

OpenFOAM is the primary industrial provider because its finite-volume,
thermophysical, heat-transfer, reacting-flow, and parallel capabilities match
the product scope. It remains an external GPL program connected by files and
subprocesses. The first experimental lowering creates a content-addressed
`simpleFoam` circular-pipe case and never overwrites an existing case directory.
Kratos remains a permissively licensed candidate for an optional native
Python-accessible provider; SU2 is a later candidate for compressible flow and
optimization. Provider choice is evidence-driven, not ideological.

## No premature common-core extraction

AgentCFD and AgentFEM will share concepts, but neither repository will depend on
the other. A common package will be extracted only after both products exercise
the same stable code. Until then, interoperability lives in small, versioned,
backend-neutral records. This prevents a speculative abstraction from slowing
both products.
