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

The laminar circular-pipe reference workflow is released. The bounded OpenCFD
v2606 fully developed laminar pipe has accepted 8/16/32 three-grid evidence.
Version 0.1.0a3 also exposes a diagnostic k-omega SST smooth-pipe slice with
explicit inlet turbulence inputs, wall functions, y-plus, friction, residual,
mass-balance, mesh, field, and runtime evidence. Its first real run converges
but is intentionally unaccepted because a developed inlet and turbulent grid
study have not passed. Do not extend either evidence record to another inlet,
OpenFOAM dialect, geometry, heat transfer, or rough wall. A generated case or a
zero process exit code is never a broader CFD claim.

The periodic circular-pipe precursor driven by OpenFOAM `meanVelocityForce`
can now be mapped into the downstream pipe through a content-addressed
`mapFields` contract. At the bounded c8 operating point, mapping transfer error
against the precursor pressure gradient is 1.05% and the result is verified.
The separate 3.81% Colebrook difference remains a model-form diagnostic; do
not extend this evidence to another Reynolds number, wall strategy, geometry,
or OpenFOAM dialect.
