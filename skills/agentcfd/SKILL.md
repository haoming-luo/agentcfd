---
name: agentcfd
description: Build, run, inspect, and verify AgentCFD computational-fluid-dynamics workflows.
---

# AgentCFD

Use this skill when a user asks an AI agent to create or operate an AgentCFD
model.

## Workflow

1. Run `agentcfd doctor --json` and `agentcfd capabilities --json`.
2. State the intended physics, geometry, properties, boundaries, procedure,
   outputs, and acceptance checks before execution.
3. Use the public `Study -> Model -> Step -> SimulationResult` API.
4. Reject a provider that does not advertise the required capability.
5. Run the model and preserve structured results and provenance.
6. Report convergence, conservation, applicability, and failed checks.
7. Export learning data only from accepted results unless the user explicitly
   records another scientific disposition.

## Initial release

The only executable scientific path in 0.1.0a1 is the steady incompressible
Newtonian laminar circular-pipe reference workflow. Do not describe the planned
OpenFOAM boundary as an implemented mesh-based solver.
