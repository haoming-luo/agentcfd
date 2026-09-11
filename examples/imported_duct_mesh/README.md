# Imported duct mesh

This deliberately small closed duct exercises the imported-geometry contract
and first bounded laminar flow slice, not physical validation. The editable project owns the STL, confirmed boundary
roles, portable inspection record, explicit interior point, sizing, and hard
cell budget.

```bash
agentcfd init ../owned-duct --request project-request.json
# Equivalent explicit confirmation when all surface names are unambiguous:
agentcfd init ../owned-duct --template imported-internal-flow \
  --geometry geometry/fluid.stl --unit m --accept-name-roles \
  --interior-point-m 0.5 0.25 0.1 --inlet-velocity-m-s 0.5 0 0 \
  --base-size-m 0.05 --maximum-cells 200000
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
its only scientific source of truth. The request keeps the full role map as an
example of the most explicit path; `role_confirmation` is the shorter safe path
for already well-named surfaces.

The final command uses the configured OpenCFD v2606 container and accepts only
an OpenFOAM-native geometry dry-run plus a budget- and quality-compliant
`checkMesh`. A normal run also gates SIMPLE convergence and conservation, then
publishes standard XDMF/H5 fields plus the compact
`report.system-loss.loss_coefficient` and its dimensional total-pressure-loss
evidence. The generated `mesh-case/` is disposable provider detail and is
ignored by Git.
