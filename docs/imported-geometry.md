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

For each named region, the same single pass also reports triangle count,
surface area, area-weighted centroid, oriented area vector, mean unit normal,
and a 0--1 normal-coherence measure. SI values appear when units are declared;
the compact human view prints confirmed inlet/outlet area and direction.
Named flow regions with no positive-area faces fail before meshing, while
strongly diverging normals warn that an opening may be curved rather than a
well-defined cap. These metrics follow OpenFOAM's region-to-patch convention
and area-normal view of mesh faces; they are geometry evidence, not a substitute
for `surfaceCheck`, meshing, or post-mesh patch integration.

For a Cartesian velocity inlet, AgentCFD combines the consistently oriented
inlet normal with the sign of the enclosed volume to determine the outward
direction. A vector pointing out of the named inlet is rejected during project
creation and again whenever factory parameters are changed, before any mesh or
solver process starts. The versioned assessment is kept in model metadata. If
watertightness, orientation, or a single inlet-normal direction is unavailable,
the assessment says `indeterminate` and does not pretend that direction was
proved; OpenFOAM's post-mesh signed-flow acceptance gate still remains active.

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
external file cannot silently change the project. Exactly one inlet control is
required: `--inlet-velocity-m-s UX UY UZ`, `--inlet-mass-flow-kg-s KG_S`, or
`--inlet-total-gauge-pressure-pa PA`. The scalar controls currently target
constant-density laminar flow. SI mesh size, interior point, and the hard cell
limit are also required rather than guessed.

For automation, `agentcfd init DESTINATION --request REQUEST.json` accepts the
installed `project-creation-request.schema.json` contract. Its geometry path is
resolved relative to the request file, and the response includes the request
fingerprint. Geometry must contain exactly one of an explicit inline
`boundary_roles` map or
`"role_confirmation": "accept-name-suggestions"`. The JSON is only a
creation envelope; it generates the same readable `case.py` and does not become
a shadow source of model truth. Imported requests likewise require exactly one
of `inlet_velocity_m_s`, `inlet_mass_flow_kg_s`, or
`inlet_total_gauge_pressure_pa`; the installed JSON Schema rejects ambiguous or
missing control.

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
form below. Laminar cases can instead use
`boundaries.mass_flow_inlet(kg_per_s)`: AgentCFD divides by the declared
constant density, and OpenFOAM distributes the resulting volume flow normal to
the inlet patch. The recovered mass-flow target error is a mandatory gate.
AgentCFD refuses to infer a Cartesian direction or turbulence assumptions from
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

For a laminar flow-rate-controlled operating point, either create the project
with `--inlet-mass-flow-kg-s` or leave turbulence absent and set only the SI
mass flow on an existing generated project:

```bash
agentcfd run . --param mass_flow_rate=0.25
```

Combining `mass_flow_rate` with imported RANS currently fails before meshing;
that path needs a released turbulence-aware flow-rate inlet rather than an
invented reference velocity.

For pressure-driven laminar equipment, AgentCFD uses OpenFOAM's documented
[total-pressure inlet and static-pressure outlet combination](https://doc.openfoam.com/2606/tools/processing/boundary-conditions/common-combinations/).
The velocity boundary derives flux-normal motion and tolerates local return
flow. Results keep the requested total-to-static pressure difference separate
from the recovered area-averaged static pressure drop, and always expose the
recovered inlet and outlet mass/volume flow. Acceptance also requires signed
flux to agree with the confirmed inlet/outlet roles, so a reversed patch or
boundary setup cannot pass merely because absolute flow rates balance:

```bash
agentcfd run . --param inlet_total_gauge_pressure=250
agentcfd result . \
  --quantity reference.flow.total_to_static_pressure_difference \
  --quantity flow.pressure_drop \
  --quantity flow.inlet_mass_flow_rate \
  --quantity flow.outlet_mass_flow_rate
```

The OpenCFD v2606 pressure-driven workflow evidence used a deliberately small
0.001 Pa total-to-static request to remain laminar on the coarse example duct.
It converged in 326 SIMPLE iterations with a relative mass imbalance of
1.71e-9, recovered 0.0862 kg/s, and published a verified 190,014-byte XDMF/H5
bundle. This is workflow and numerical-gate evidence, not physical validation;
the compact record is `openfoam-v2606-imported-duct-pressure-driven.json`.

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
