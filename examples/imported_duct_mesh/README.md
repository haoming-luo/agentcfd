# Imported duct mesh

This deliberately small closed duct exercises the imported-geometry contract
and first bounded laminar flow slice, not physical validation. The editable project owns the STL, confirmed boundary
roles, portable inspection record, explicit interior point, sizing, and hard
cell budget.

```bash
agentcfd init ../owned-duct --request project-request.json
# Or inspect and operate this already-materialized example directly:
agentcfd geometry-check geometry/fluid.stl --unit m \
  --roles geometry/boundary-roles.json --internal-flow
agentcfd mesh . --plan-only
agentcfd mesh . --output mesh-case
agentcfd run .
agentcfd view .
```

The creation request is relative to this directory and is intended for agents,
GUIs, or repeatable scaffolding. The generated project still uses `case.py` as
its only scientific source of truth.

The final command uses the configured OpenCFD v2606 container and accepts only
an OpenFOAM-native geometry dry-run plus a budget- and quality-compliant
`checkMesh`. A normal run also gates SIMPLE convergence and conservation, then
publishes standard XDMF/H5 fields. The generated `mesh-case/` is disposable provider detail and is
ignored by Git.
