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

`SimulationResult.to_sample()` emits `agentcae.scientific-sample` version
`0.1.0`. Its numeric `inputs` and `outputs` map directly onto AgentFEM's
`datasets.Sample` contract, while `quantity_schema` retains names, units,
shapes, and semantic kinds. `SimulationResult.to_exchange()` preserves the
larger result as `agentcae.simulation-result` for coupling and audit tools.

For normal project workflows, `agentcfd export sample PROJECT OUTPUT.json
--output-quantity NAME` resolves numeric `case.py` parameters, verifies the
published project, and writes that same solver-neutral sample atomically.
`Project.scientific_sample()` and `Project.export_scientific_sample()` expose
the matching Python APIs. This is the preferred scalar bridge into AgentFEM or
tabular learning because it does not require users to reconstruct inputs,
units, acceptance, or source hashes by hand.

For campaigns, `agentcfd export dataset PROJECT DIRECTORY
--output-quantity NAME` writes a content-hashed `samples.jsonl` plus
`manifest.json`. Included lines retain the same AgentCAE sample identity;
unaccepted points remain explicit in the manifest, and mixed parameter or
quantity schemas fail before the target directory appears. AgentFEM and other
consumers can therefore stream samples without importing AgentCFD or opening
solver fields. `agentcfd verify dataset` performs a dependency-free package
audit before a copied dataset is admitted downstream.

The products share semantics, not Python imports. AgentCFD therefore remains
installable without AgentFEM, and either product can evolve its solver stack
behind the stable `agentcae.*` records.

Spatial fields use `agentcae.field-bundle/0.1.0`: XDMF/HDF5 for mesh-aware
exchange and a mirror NPZ for array consumers. `agentcfd export field-sample`
also emits the exact `coordinates`, `values`, `encoding_json`, and
`metadata_json` keys read by `agentfem.datasets.FEMFieldSample`. This is a
direct file-level bridge, not a Python dependency between the products.

Installed tools can discover these contracts through
`agentcfd.contracts.available()`, `path()`, and `load()`, or with
`agentcfd contracts --json`. This avoids repository-relative paths and does not
require a JSON-schema validation library at runtime.
The catalog includes `openfoam-mesh.schema.json`, which fixes the identity
contract used to bind native CFD fields to their exact mesh.

AgentCAE remains optional. Installing `agentcfd[interop]` adds its lightweight
catalog without adding either numerical solver. `agentcfd contracts
--check-agentcae --json` derives AgentCFD's four produced identities from the
shipped JSON schemas and compares them with the installed AgentCAE catalog.
Missing, duplicate, unexpected, or version-drifted identities fail the explicit
audit with a machine-readable repair action; ordinary AgentCFD workflows remain
dependency-free when no cross-product audit is requested.

The intended sequence is:

```text
CFD/FEM/experiment -> accepted observations -> common dataset
                   -> user model or supplied template
                   -> applicability guard -> high-fidelity fallback
```

Field-learning workflows will additionally require mesh or observation-grid
identity, tensor layout, normalization, masks, boundary channels, and physical
units. Those contracts will be fixed before a neural-operator convenience API.
