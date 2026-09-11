# Common CFD workflow API

AgentCFD's public language describes engineering intent independently from an
OpenFOAM dictionary, Fluent settings tree, or another solver. One readable
`case.py` owns the simulation; `Project.plan()` resolves it against a provider
before any expensive solve.

`Step.to_dict()` emits `agentcfd.analysis-request/0.1`, and
`Step.fingerprint()` identifies the complete model, procedure, initialization,
mesh intent, and output request. The installed `analysis-request.schema.json`
is discoverable through `agentcfd contracts --json` for agents and GUIs.

## Object layout

```text
Study
└── Model
    ├── Domain ── stable Regions
    ├── Fluid
    ├── Boundary conditions keyed by Region name
    └── reusable internal measurement Sections
        └── Step
            ├── Procedure
            ├── Initialization
            ├── MeshIntent
            └── OutputRequest
                ├── full-field Frames
                ├── rolling Checkpoints
                └── compact Reports / probes
```

This follows mature CFD workflow boundaries without copying a backend API:

- boundaries target stable named surfaces and distinguish velocity, mass-flow,
  pressure, no-slip, slip, and symmetry intent;
- initialization is explicit and can be uniform, potential-flow based, or a
  trusted previous result;
- mesh intent separates global size, local refinement, wall layers, and quality
  gates;
- probes, surface/force reports, total-pressure-loss, flow-uniformity, and
  multi-outlet flow-distribution reports are compact
  histories, independent from the much more expensive full-field frame cadence;
- `outputs.line_profile(...)` samples one declared field on the final portable
  frame and publishes only distance plus that value to CSV; vector fields
  require an explicit Cartesian component or magnitude;
- a provider validates the entire `Step` before lowering it. Unsupported intent
  is rejected instead of ignored.

The first demonstrator is
[`examples/channel_baffle_project`](../examples/channel_baffle_project). It
is now the first executable non-pipe slice: a deterministic structured
five-block mesh, low-Re transient flow, compact reports, and standard field
output. A separate bounded imported-volume slice now owns checked STL/OBJ,
explicit region roles, an interior seed, a hard mesh budget, and steady laminar
or explicitly parameterized k-omega SST OpenFOAM execution. `init --request`
exposes creation as strict JSON for agents and GUIs without replacing the
generated Python source of truth. The imported provider consumes the same
point, scalar pressure-surface, and wall-force report objects as the channel
provider. It also accepts a pressure-loss report that converts mass-flow-
averaged total pressure and actual inlet flow into a directly usable loss
coefficient. All of these produce small solver-iteration histories rather than
more full-field frames. Cartesian RANS inlets carry velocity, turbulence
intensity, and length scale in one typed boundary object. Laminar imported
volumes also lower a typed SI mass-flow inlet into a patch-normal constant-
density volume flow and verify the recovered target.

## Output and result ergonomics

Use full fields only when spatial data are necessary. Prefer reports for design
decisions and convergence monitoring:

```python
reports = (
    outputs.probe("wake", at=(0.5, 0.05, 0.05)),
    outputs.surface_report(
        "outlet-pressure",
        region="outlet",
        field="fluid.pressure",
        operation="area-average",
    ),
    outputs.pressure_loss(
        "system-loss",
        inlet="inlet",
        outlet="outlet",
    ),
    outputs.flow_uniformity("outlet-quality", region="outlet"),
)
```

Boundary patches are not the only useful reporting locations. Bends, valves,
tees, manifolds, and diffusers often need the same upstream/downstream planes
for pressure loss, uniformity, and scalar reductions. Declare those planes once
in SI coordinates and reuse their stable names:

```python
from agentcfd import regions

model.sections(
    regions.plane(
        "upstream",
        origin=(0.20, 0.05, 0.05),
        normal=(1.0, 0.0, 0.0),
    ),
    regions.plane(
        "downstream",
        origin=(0.80, 0.05, 0.05),
        normal=(1.0, 0.0, 0.0),
    ),
)

output = outputs.standard(
    reports=(
        outputs.pressure_loss(
            "valve-loss", inlet="upstream", outlet="downstream"
        ),
        outputs.flow_uniformity("downstream-quality", region="downstream"),
    )
)
```

A section is an infinite plane clipped by the fluid mesh, not a new boundary
and not a duplicated field export. The OpenFOAM imported-volume and baffled-
channel providers lower it to a `cuttingPlane` sampled surface and retain only
the requested scalar histories. Its origin must be strictly inside the domain
bounds so an obviously empty request fails before execution. Physical port
balance and wall forces still require actual boundary surfaces; an internal
plane cannot be passed to flow-distribution or force reports.

For constant-density pressure loss, boundary patches retain flux-field
weighting while internal planes use velocity projected onto the plane normal.
Both are mass-flow averages; this distinction avoids asking a general sampled
surface to consume OpenFOAM's face-only `phi` field. Plane normals should point
consistently from upstream to downstream. The generated OpenCFD v2606 syntax is
covered by an actual solver dry-run, not only string tests.

People can read the declarations above directly. Agents and future GUIs can
inspect the same relationships without parsing Python or touching field data:

```python
catalog = step.observation_catalog()
```

The versioned `agentcfd.observation-catalog/0.1` record lists each physical
surface, internal section, and inline probe point once, records every report
that reuses it, and states cadence and retention. It separately describes the
full-field frame request. `Project.plan()` embeds this catalog under
`decisions.output_plan.observation_catalog`, and the installed
`observation-catalog.schema.json` contract is available through
`agentcfd contracts --json`. Catalog construction opens no XDMF/H5 payload and
starts no solver.

`pressure_loss` returns `report.system-loss.total_pressure_loss` in Pa and
`report.system-loss.loss_coefficient` as a dimensionless scalar, using the
actual inlet bulk velocity:

`K_loss = (p_t,in - p_t,out) / (0.5 * rho * (Q_in / A_ref)^2)`

It is the complete loss between the selected planes, including distributed
wall loss. A fitting-only K value requires a declared straight-run baseline
rather than a hidden correction.

`flow_uniformity` publishes three directly usable quantities without adding a
field frame:

- `report.outlet-quality.velocity_uniformity_index` in `[0, 1]`, where one is
  perfectly uniform;
- `report.outlet-quality.area_normal_velocity` in m/s, signed along the
  surface outward normal;
- `report.outlet-quality.area` in m².

The uniformity definition is
`1 - integral(|U - mean(U)| dA) / (2 |mean(U)| A)`, clamped to `[0, 1]`.
It deliberately evaluates the complete velocity vector, so swirl and
cross-flow reduce the score rather than disappearing behind an axial-only
average. The normal velocity provides the accompanying direction and scale.

Manifolds and split ducts declare their ports once instead of assembling a
spreadsheet from unrelated patch reports:

```python
split = outputs.flow_distribution(
    "branch-split",
    inlet="inlet",
    outlets=("branch_a", "branch_b", "branch_c"),
    targets={"branch_a": 0.25, "branch_b": 0.25, "branch_c": 0.50},
)
```

The result contains positive role-directed volume and constant-density mass
flow for every port, each outlet fraction, total outlet flow, relative
inlet/outlet imbalance, outlet-flow coefficient of variation, signed
actual-minus-target errors, and `report.branch-split.maximum_fraction_error`.
Target fractions are optional but, when present, must cover every outlet and
sum to one. A steady final state with inlet or branch reversal fails report
recovery instead of turning a signed flux into a plausible-looking fraction.
These are scalar histories only; no extra XDMF/H5 field frame is produced.

Design limits belong in the same readable request rather than in a hidden
spreadsheet or agent prompt:

```python
output = outputs.standard(
    reports=(
        outputs.pressure_loss("system-loss", inlet="inlet", outlet="outlet"),
        outputs.flow_uniformity("outlet-quality", region="outlet"),
    ),
    criteria=(
        outputs.require(
            "loss-budget",
            quantity="report.system-loss.loss_coefficient",
            unit="1",
            maximum=0.20,
        ),
        outputs.require(
            "uniform-enough",
            quantity="report.outlet-quality.velocity_uniformity_index",
            unit="1",
            minimum=0.95,
        ),
    ),
)
```

Bounds are inclusive and evaluated only after canonical scalar recovery. A
missing quantity, exact-unit mismatch, or violated bound becomes a visible
`requirement.*` check and makes the run unaccepted. Requirement failure does
not rewrite numerical trust: a verified calculation can be trustworthy while
still missing its design target.

After a run, inspect names without loading heavy files, then request a typed
record:

```python
print(result.available_data())
pressure_drop = result.quantity("flow.pressure_drop")
probe_history = result.history("probe.wake.fluid.velocity.x")
velocity_field = result.field("fluid.velocity", location="point")
```

These records preserve unit, association, artifact identity, and trust instead
of returning an ambiguous array.

## Current support boundary

The API object is not a capability claim. At this checkpoint:

- circular-pipe steady laminar and bounded RANS lowering is available through
  the existing OpenFOAM provider;
- one bottom-attached rectangular-channel baffle is executable for transient,
  incompressible, constant-property flow with hydraulic Re below 2300;
- arbitrary channels, thermal imported geometry, vector surface reductions,
  crash-safe in-run checkpoint publication, prism-layer automation, and
  arbitrary vector surface reductions and turbulent baffle flow remain pending
  and fail closed. Imported k-omega SST is
  an experimental workflow slice with y-plus evidence, not a physical-validation
  claim. Completed baffled-channel runs publish a verified rolling ZIP
  and can resume from `initialization.previous_result(...)`.

This separation lets the product language grow coherently while every numerical
claim remains tied to implemented lowering and evidence.

## Design references

The vocabulary was cross-checked against primary documentation rather than one
solver's file syntax:

- [Ansys Fluent boundary conditions](https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/flu_ug/flu_ug_bcs_sec_bound_cond.html)
  for boundary families and named zones;
- [Ansys Fluent initialization](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/flu_ug/flu_ug_sec_solve_initialize.html)
  for standard, hybrid, and patched initialization responsibilities;
- [PyFluent meshing workflows](https://fluent.docs.pyansys.com/version/stable/user_guide/meshing/new_meshing_workflows.html)
  for staged geometry, sizing, boundary layer, region, and volume-mesh intent;
- [OpenFOAM boundary-condition categories](https://doc.openfoam.com/2606/tools/processing/boundary-conditions/)
  and [mesh-quality controls](https://doc.openfoam.com/2306/tools/pre-processing/mesh/generation/snappyhexmesh/meshquality/)
  for provider-side lowering and quality gates;
- [OpenFOAM RANS turbulence models](https://doc.openfoam.com/2606/tools/processing/models/turbulence/)
  and [wall functions](https://doc.openfoam.com/2606/tools/processing/models/turbulence/ras/wall-functions/)
  for explicit k-omega SST and y-plus review requirements;
- [OpenFOAM pressure-velocity boundary combinations](https://doc.openfoam.com/2606/tools/processing/boundary-conditions/common-combinations/)
  for the volume-flow inlet and static-pressure outlet pairing;
- [OpenFOAM pressure](https://doc.openfoam.com/2306/tools/post-processing/function-objects/field/pressure/)
  and [surfaceFieldValue](https://doc.openfoam.com/2312/tools/post-processing/function-objects/field/surfaceFieldValue/)
  for physical total pressure, mass-flow weighting, area recovery, velocity
  uniformity, normal velocity, and compact component-loss reports;
  [cuttingPlane](https://doc.openfoam.com/2306/tools/post-processing/function-objects/sampling/surfaces/cuttingPlane/)
  defines the provider lowering for reusable internal sections;
  [SU2 custom output](https://su2code.github.io/docs_v7/Custom-Output/)
  informed the solver-neutral report vocabulary;
- [PyFluent solver settings](https://fluent.docs.pyansys.com/version/stable/user_guide/solver_settings/solver_settings_contents.html)
  for discoverable group and named-object behavior.
