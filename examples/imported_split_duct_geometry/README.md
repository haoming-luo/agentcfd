# Split-outlet imported geometry

This small watertight rectangular duct divides one coplanar outlet into two
named patches. It is an integration fixture for the multi-outlet project and
compact flow-distribution workflow, not a physical manifold benchmark.

Create a readable project without authoring OpenFOAM dictionaries:

```bash
agentcfd init split-duct \
  --template imported-internal-flow \
  --geometry examples/imported_split_duct_geometry/fluid.stl \
  --unit m \
  --roles examples/imported_split_duct_geometry/boundary-roles.json \
  --interior-point-m 0.5 0.25 0.1 \
  --inlet-velocity-m-s 0.5 0 0 \
  --base-size-m 0.05 \
  --maximum-cells 200000 \
  --outlet-target branch_a=0.5 \
  --outlet-target branch_b=0.5 \
  --maximum-fraction-error 0.001
```

Agents and GUIs can create the identical project through the checked strict
request, whose relative geometry path resolves beside the JSON file:

```bash
agentcfd init split-duct \
  --request examples/imported_split_duct_geometry/project-request.json
```

The generated `case.py` contains one `outputs.flow_distribution("flow-split",
...)` and the explicit design criterion shown below. Per-outlet loss and
uniformity reports remain opt-in to avoid scaling irrelevant storage with port
count. Omitting all three target options produces the same report without
inventing a target or acceptance threshold:

```python
outputs.flow_distribution(
    "flow-split",
    inlet="inlet",
    outlets=("branch_a", "branch_b"),
    targets={"branch_a": 0.5, "branch_b": 0.5},
)
outputs.require(
    "flow-split-target",
    quantity="report.flow-split.maximum_fraction_error",
    unit="1",
    maximum=0.001,
)
```

The corresponding OpenCFD v2606 runtime record is
[`docs/openfoam-v2606-flow-distribution.json`](../../docs/openfoam-v2606-flow-distribution.json).
