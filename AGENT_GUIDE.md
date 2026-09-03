# Guide for AI agents

AgentCFD is operated through the same public Python workflow used by people.
Do not generate backend dictionaries until the resolved AgentCFD model passes
validation and the selected provider advertises the required capability.

## First run

```bash
python -m pip install -e .
agentcfd doctor --json
agentcfd capabilities --json
agentcfd demo pipe --output pipe-result.json
agentcfd prepare openfoam-pipe openfoam-pipe --json
python -m pytest -q
```

## Required behavior

1. State geometry, fluid properties, boundaries, procedure, and requested results.
2. Use SI units unless an API explicitly accepts another unit system.
3. Check provider capability before execution.
4. Preserve warnings, failed checks, provenance, and model fingerprints.
5. Never use a completed process as evidence that the physics is correct.
6. Do not train on a result unless `result.accepted` is true or a human explicitly
   records why a failed check is acceptable for that dataset.
7. Explain unsupported physics instead of silently changing the model.

## Current boundary

Only the laminar circular-pipe reference workflow is scientifically accepted in
0.1.0a1. Deterministic OpenFOAM `simpleFoam` case lowering for the same bounded
physics is experimental. External execution still returns an unaccepted result
until mesh-field pressure loss and conservation are recovered. Do not turn a
generated case or a zero process exit code into a broader CFD claim.
