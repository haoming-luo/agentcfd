# AgentCFD MCP

This package is the thin official MCP transport for AgentCFD. It exposes the
bounded operations already declared by `agentcfd.operations`; it does not
create a second simulation API and never accepts shell or Python source.

By default it can only read projects under the current directory. Configure
one or more roots with `AGENTCFD_MCP_ROOTS` (separated by the platform path
separator). Mutation and solver execution remain disabled unless their
specific environment switches are explicitly set:

- `AGENTCFD_MCP_ALLOW_WRITES=1`
- `AGENTCFD_MCP_ALLOW_EXECUTION=1`
- `AGENTCFD_MCP_ALLOW_DESTRUCTIVE=1`

Run the local stdio server with `agentcfd-mcp`. Numerical commands execute in a
separate Python process, keeping OpenFOAM and optional scientific runtimes out
of the MCP host process.
