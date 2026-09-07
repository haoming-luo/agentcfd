# Imported geometry: inspect before meshing

Imported geometry is a high-risk automation boundary: STL and OBJ do not carry
a dependable physical unit, CAD tessellation can erase or merge named faces,
and one open or non-manifold edge can change what “inside” means. AgentCFD
therefore begins with a read-only contract instead of silently repairing or
meshing an ambiguous file.

```bash
agentcfd geometry-check valve-fluid.stl --unit mm
agentcfd geometry-check intentional-open-plate.obj --unit m --allow-open
agentcfd geometry-check large.stl --unit mm --max-topology-triangles 2000000 --json
```

Name-based roles are suggestions only. Confirm every discovered region through
a small versioned file and add `--internal-flow` when inlet and outlet are
mandatory:

```json
{
  "schema": "agentcfd.boundary-role-map/0.1",
  "regions": {
    "inlet_main": "inlet",
    "outlet_main": "outlet",
    "housing": "wall"
  }
}
```

```bash
agentcfd geometry-check duct.obj --unit mm --roles boundary-roles.json \
  --internal-flow --output geometry/inspection.json
```

The map must cover the exact discovered names with no stale extras. Supported
roles are inlet, outlet, wall, symmetry, periodic, interface, farfield,
opening, and empty. AgentCFD will suggest roles from recognizable tokens but
sets `requires_confirmation: true`; it never converts a filename guess into a
physical boundary condition.

For well-named surfaces, the confirmation can be made directly while creating
the project:

```bash
agentcfd init my-duct --template imported-internal-flow \
  --geometry duct.obj --unit mm --accept-name-roles \
  --interior-point-m 0.15 0.03 0.03 \
  --inlet-velocity-m-s 1 0 0 --base-size-m 0.005 \
  --maximum-cells 500000
```

This is acceptance, not automatic inference. It succeeds only when every exact
surface name has one recognized role token. Any ambiguous name causes a
fail-closed error before the destination directory is written, at which point
the explicit versioned role map is the repair path.

The released inspector supports binary/ASCII STL and OBJ. It hashes the source,
converts bounds to SI, discovers surface region names, counts triangles and
unique vertices, detects zero-area triangles, boundary and non-manifold edges,
checks shared-edge orientation, estimates enclosed volume, and reports the
available `surfaceCheck`, `snappyHexMesh`, and `gmsh` tools. Edge topology has
an explicit triangle limit; when it is exceeded, bounds still complete but the
report refuses to invent watertightness.
Exact vertex matching is the default. If a tessellator emitted numerically
near-coincident vertices, `--merge-tolerance` applies an explicit tolerance in
source units to topology keys only. The report records it and warns that it
must remain smaller than any physical gap that should stay open.

The inspection can now become content-addressed public model intent. Keep the
surface and report inside the project; the bridge deliberately discards the
machine-specific absolute source path and retains the project-relative asset,
source hash, SI bounds, unit conversion, enclosed volume, merge policy, and
confirmed roles:

```python
import json
from pathlib import Path

from agentcfd import Model, boundaries, fluids, geometry, meshing, studies


def build():
    root = Path(__file__).parent
    inspection = json.loads((root / "geometry/inspection.json").read_text())
    domain = geometry.imported_surface_from_inspection(
        inspection,
        asset="geometry/duct.obj",
        interior_point_m=(0.15, 0.03, 0.03),
    )
    return Model(
        name="imported-duct",
        study=studies.internal_flow(),
        domain=domain,
        fluid=fluids.newtonian(
            "water", density=998.2, dynamic_viscosity=1.002e-3
        ),
    ).boundaries(
        inlet_main=boundaries.velocity_inlet((1.0, 0.0, 0.0)),
        outlet_main=boundaries.pressure_outlet(),
        housing=boundaries.no_slip_wall(),
    ).step(mesh=meshing.automatic(base_size=0.005, maximum_cells=2_000_000))
```

For the released single-inlet/single-outlet laminar slice, the CLI can perform
that bridge and create the complete owned project in one command:

```bash
agentcfd init my-duct --template imported-internal-flow \
  --geometry duct.obj --unit mm --roles boundary-roles.json \
  --interior-point-m 0.15 0.03 0.03 \
  --inlet-velocity-m-s 1 0 0 --base-size-m 0.005 \
  --maximum-cells 500000
```

Initialization runs the same bounded inspector before writing, rejects
unsupported roles or ambiguous inlet/outlet count, and copies the source into
`geometry/`. It writes a portable `inspection.json`, normalized
`boundary-roles.json`, and a readable `case.py`; a later change to the original
external file cannot silently change the project. Direction, SI mesh size,
interior point, and the hard cell limit are required rather than guessed.

For automation, `agentcfd init DESTINATION --request REQUEST.json` accepts the
installed `project-creation-request.schema.json` contract. Its geometry path is
resolved relative to the request file, and the response includes the request
fingerprint. Geometry must contain exactly one of an explicit inline
`boundary_roles` map or
`"role_confirmation": "accept-name-suggestions"`. The JSON is only a
creation envelope; it generates the same readable `case.py` and does not become
a shadow source of model truth.

`agentcfd plan .` verifies the asset still exists and still matches the
inspected SHA-256 before any provider action. Missing or changed geometry has
its own `input_assets_ready: false` state. This is separate from provider
compatibility so an agent can distinguish “repair the input” from “the released
OpenFOAM flow slice does not support this physics or boundary intent.” Volume CFD intent rejects
an intentionally open surface even when inspection was run with `--allow-open`.

Plan, prepare, or execute the mesh without starting a flow solver:

```bash
agentcfd mesh . --plan-only
agentcfd mesh . --output mesh-case --prepare-only
agentcfd mesh . --output mesh-case
```

The execution path runs `blockMesh`, native `snappyHexMesh -checkGeometry
-dry-run`, `snappyHexMesh -overwrite`, and `checkMesh -allGeometry
-allTopology`. It accepts the mesh only when every command succeeds, checkMesh
reports `Mesh OK`, and observed cell count, non-orthogonality, skewness, and
aspect ratio satisfy public intent. `-overwrite` avoids retaining one full mesh
for every snappy stage. The first slice deliberately rejects prism layers,
non-OpenFOAM region names, unsupported boundary roles, missing interior points,
and background grids already over budget.

The checked-in `examples/imported_duct_mesh` vertical slice produced an
accepted 6,400-cell OpenCFD v2606 mesh from a 2,688-cell background in about
1.65 seconds of container utility time. Its native gates measured 15.74°
maximum non-orthogonality, 0.104 maximum skewness, and 1.383 maximum aspect
ratio. This is workflow/mesh evidence, not flow-physics validation; the compact
record is `docs/openfoam-v2606-imported-duct-mesh.json`.

The same project now runs end to end with `agentcfd run .` for steady,
incompressible, isothermal laminar or k-omega SST flow. Arbitrary laminar
geometry uses `boundaries.velocity_inlet((ux, uy, uz))`; RANS uses the explicit
form below. AgentCFD refuses to infer direction or turbulence assumptions from
a scalar:

```python
study = studies.internal_flow(
    turbulence="k-omega-sst",
    wall_treatment="blended-wall-functions",
)
inlet = boundaries.turbulent_velocity_inlet(
    (5.0, 0.0, 0.0),
    intensity=0.05,
    length_scale=0.01,
)
output = outputs.turbulent_internal_flow()
```

Generated imported projects expose the same transition without rewriting the
model. Keep the three turbulence values absent for laminar flow, or provide all
three explicitly:

```bash
agentcfd run . \
  --param turbulence_model='"k-omega-sst"' \
  --param turbulence_intensity=0.05 \
  --param turbulence_length_scale=0.01
```

The RANS slice writes `k`, `omega`, and `nut`, and always computes compact
minimum, maximum, and average wall y-plus histories. OpenFOAM's blended
`omegaWallFunction`/`nutUBlendedWallFunction` treatment is used with no-slip
walls. The full wall y-plus range must stay between 30 and 300 or acceptance is
blocked. This is workflow evidence only: prism-layer automation, automatic
near-wall correction, grid sensitivity, and physical validation remain open gates. Heat,
compressibility, reactions, roughness, k-epsilon, and vector surface
reductions still fail closed. Point velocity/pressure probes, scalar pressure
surface reductions, and wall-force reports remain compact histories in both
laminar and RANS runs.

The checked OpenCFD v2606 run converged by SIMPLE residual control in 344
iterations with `1.8e-10` relative mass imbalance and 1.179 Pa pressure drop.
Its verified one-frame XDMF/H5 bundle contains 7,749 points and occupies about
190 KiB; the complete ordinary output is about 633 KiB. The source-linked
record is `docs/openfoam-v2606-imported-duct-flow.json`. These are numerical and
workflow gates, not an experimental validation claim.

The matching guarded RANS run used an explicit 0.5 m/s inlet, 5% intensity,
0.025 m turbulence length scale, k-omega SST, and blended wall functions. It
converged in 319 SIMPLE iterations with `4.2e-10` relative mass imbalance and
9.031 Pa pressure drop. Its complete wall y-plus range was 176.81--292.34, so
the run passed the public 30--300 wall-function gate; this is not a claim that
the coarse grid is physically validated. The verified five-field XDMF/H5
bundle occupies about 253 KiB and the source-linked record is
`docs/openfoam-v2606-imported-duct-rans.json`.

Project runs keep one content-addressed mesh cache under
`.agentcfd/mesh-cache/`. Its key covers the imported source bytes and complete
resolved mesh plan plus the configured OpenFOAM runtime identity, not the
velocity or turbulence operating point. Every hit
re-hashes all cached `polyMesh` files before reuse; a mismatch falls back to
native meshing and replaces the invalid entry. `agentcfd storage` accounts for
this persistent accelerator separately, while ordinary `agentcfd clean`
preserves it. Each result and run marker says whether its mesh was `generated`
or a verified `cache-hit`. Use `agentcfd clean . --include-cache` to preview the
reclaimable cache, then add `--apply` only when trading future meshing time for
disk space is intentional.

STEP/IGES are recognized but not silently tessellated. A future CAD adapter
must make tessellation tolerance, units, face-name retention, and source hash
explicit. Similarly, `geometry_ready: true` means that the released preflight
found no blocking defect; `ready_for_import_setup` means the surface can enter
the public `Model`. The inspection alone keeps `ready_to_mesh` false because the
interior seed and mesh budget live in `case.py`; `agentcfd mesh --plan-only`
resolves that second gate.

This boundary mirrors OpenFOAM's documented workflow: `snappyHexMesh` consumes
triangulated surfaces such as STL/OBJ/VTK, supports multiple surfaces and
regions, uses surface/region identities as final patches, and is designed for
batch operation. Its `-checkGeometry`/dry-run paths and the separate
`surfaceCheck` utility remain later provider gates, not claims made by this
standard-library scan. OpenFOAM also documents `maxGlobalCells` as a hard stop
during castellation, which will become an explicit AgentCFD meshing budget.

Primary references:

- [OpenFOAM v2606 snappyHexMesh overview](https://doc.openfoam.com/2606/tools/pre-processing/mesh/generation/snappyhexmesh/)
- [OpenFOAM triangulated geometry and surface regions](https://doc.openfoam.com/2212/tools/pre-processing/mesh/generation/snappyhexmesh/geometry/)
- [OpenFOAM castellation, refinement and maxGlobalCells](https://doc.openfoam.com/2606/tools/pre-processing/mesh/generation/snappyhexmesh/castellation/)
- [OpenFOAM v2606 turbulence models](https://doc.openfoam.com/2606/tools/processing/models/turbulence/)
- [OpenFOAM v2606 wall functions and y-plus requirements](https://doc.openfoam.com/2606/tools/processing/models/turbulence/ras/wall-functions/)
