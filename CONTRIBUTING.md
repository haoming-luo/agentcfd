# Contributing

AgentCFD welcomes focused contributions that deepen a supported workflow.

Every scientific capability should include:

- governing equations and assumptions;
- typed public inputs and explicit units;
- provider capability and unsupported-case behavior;
- unit, integration, and regression tests;
- a representative example;
- conservation or invariant checks;
- external benchmark evidence appropriate to its maturity;
- documentation of failure modes and limits.

Avoid backend-specific options in the top-level engineering language unless the
concept has no stable physical or numerical meaning outside that backend.
