# Imported duct mesh

This deliberately small closed duct exercises the imported-geometry contract,
not flow-solver accuracy. The editable project owns the STL, confirmed boundary
roles, portable inspection record, explicit interior point, sizing, and hard
cell budget.

```bash
agentcfd geometry-check geometry/fluid.stl --unit m \
  --roles geometry/boundary-roles.json --internal-flow
agentcfd mesh . --plan-only
agentcfd mesh . --output mesh-case
```

The final command uses the configured OpenCFD v2606 container and accepts only
an OpenFOAM-native geometry dry-run plus a budget- and quality-compliant
`checkMesh`. The generated `mesh-case/` is disposable provider detail and is
ignored by Git.
