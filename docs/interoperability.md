# AgentFEM, CFD, and AI interoperability

AgentCFD and AgentFEM are independent products with a planned shared scientific
boundary. The boundary is data and semantics, not direct access to each other's
solver objects.

## Canonical fields

Backend names such as `U`, `p`, or `T` are aliases. Exchange records use
unambiguous names:

- `fluid.velocity`, `fluid.pressure`, `fluid.density`, `fluid.temperature`;
- `thermal.temperature`, `thermal.heat_flux`;
- `solid.displacement`, `solid.velocity`, `solid.traction`.

Every transferred field records its unit, mesh location, direction, coordinate
frame, conservation requirement, mesh identity, model identity, and time
coordinate.

## Coupling sequence

The first useful coupling is one-way and deterministic:

```text
AgentCFD pressure / wall traction / temperature / heat flux
                         |
                         v
                versioned interface record
                         |
                         v
             AgentFEM structural or thermal load
```

The return path adds AgentFEM displacement or velocity for mesh motion. A later
partitioned driver will control time windows, interpolation, relaxation,
convergence, rollback, and restart. Conservative mapping must be explicit;
nearest-neighbour transfer is not an acceptable hidden default for loads or
heat flux.

## Learning continuity

`SimulationResult.to_sample()` emits `agentcae.scientific-sample/0.1`, a small
framework-neutral record. It can feed a future common dataset layer without
requiring PyTorch, JAX, PINNs, or neural operators in the core CFD package.

The intended sequence is:

```text
CFD/FEM/experiment -> accepted observations -> common dataset
                   -> user model or supplied template
                   -> applicability guard -> high-fidelity fallback
```

Field-learning workflows will additionally require mesh or observation-grid
identity, tensor layout, normalization, masks, boundary channels, and physical
units. Those contracts will be fixed before a neural-operator convenience API.
