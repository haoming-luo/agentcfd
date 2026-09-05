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
    └── Boundary conditions keyed by Region name
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
- probes and surface/force reports are compact histories, independent from the
  much more expensive full-field frame cadence;
- a provider validates the entire `Step` before lowering it. Unsupported intent
  is rejected instead of ignored.

The first demonstrator is
[`examples/channel_baffle_project`](../examples/channel_baffle_project). It
is now the first executable non-pipe slice: a deterministic structured
five-block mesh, low-Re transient flow, compact reports, and standard field
output. Higher-Re turbulence and general imported geometry remain separate
capabilities rather than silent extensions of this example.

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
)
```

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
- arbitrary channels, imported geometry, vector surface reductions, crash-safe
  in-run checkpoint publication, and turbulent baffle flow remain pending and
  fail closed. Completed baffled-channel runs publish a verified rolling ZIP
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
- [OpenFOAM surfaceFieldValue](https://doc.openfoam.com/2312/tools/post-processing/function-objects/field/surfaceFieldValue/)
  and [SU2 custom output](https://su2code.github.io/docs_v7/Custom-Output/)
  for area, mass-flow, integral, uniformity, and probe-style reports;
- [PyFluent solver settings](https://fluent.docs.pyansys.com/version/stable/user_guide/solver_settings/solver_settings_contents.html)
  for discoverable group and named-object behavior.
