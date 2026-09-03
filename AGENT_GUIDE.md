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

The laminar circular-pipe reference workflow is released. In 0.1.0a2, the
bounded OpenCFD v2606 fully developed pipe workflow has accepted 8/16/32
three-grid evidence, while the OpenFOAM provider remains experimental as a
general capability. Do not extend that evidence to uniform developing inlets,
other OpenFOAM dialects, turbulence, heat transfer, or general geometry. A
generated case or a zero process exit code is never a broader CFD claim.
