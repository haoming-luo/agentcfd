# Zero-dimensional hydraulic models

AgentCFD's `zero_d` module solves a fluid system as named nodes connected by
passive branches. It answers system questions such as required supply pressure,
pipe flow, gravity head, and parallel flow split in milliseconds and without a
mesh. It is a complement to resolved CFD, not a lower-resolution CFD mesh.

## First model

All public inputs and outputs use SI units:

```python
from agentcfd import fluids, zero_d

water = fluids.newtonian(
    "water",
    density=998.2,
    dynamic_viscosity=1.002e-3,
)

system = zero_d.network("cooling-loop", fluid=water)
system.node("supply", pressure=250_000.0, elevation=2.0)
system.node("load", volume_flow_source=-0.0015, elevation=8.0)
system.pipe(
    "riser",
    "supply",
    "load",
    length=20.0,
    diameter=0.05,
    roughness=1.0e-5,
    loss_coefficient=2.5,
)

result = system.solve().require_accepted()
print(result.pressure("load"))          # Pa
print(result.volume_flow_rate("riser")) # m^3/s
result.write_json("results/zero_d_result.json")
```

`volume_flow_source` is positive when fluid enters the network and negative for
a withdrawal. `mass_flow_source` is an alternative in kg/s and is converted
using the declared constant density; specifying both fails closed. Branch flow
is positive from `start_node` to `end_node`. Node pressure is static gauge
pressure; elevation head is included explicitly as `rho g (z_start - z_end)`.

Use `system.resistance(...)` for a known passive component law
`delta_p = R1 Q + R2 |Q| Q`. Use `system.pipe(...)` for Darcy--Weisbach pipe
loss plus a lumped minor-loss coefficient. Circular pipes use the named
Churchill 1977 all-regime correlation so a network iteration can cross between
laminar and turbulent states without an artificial solver discontinuity. A
reported transitional solution is still only a screening estimate.

## Trust and output

`solve()` returns a `ZeroDResult`, not an unstructured dictionary. Acceptance
requires both free-node volume conservation and branch pressure closure. The
JSON output conforms to `zero-d-result.schema.json` and includes model identity,
units, sign-stable flows, Reynolds numbers, checks, and limitations. Model and
result files overwrite the selected path by default, matching the normal
single-project workflow; campaigns should choose distinct run directories.

Use the result to screen operating points or to derive explicit pressure and
mass-flow boundary conditions for a later 3D AgentCFD model. Do not present a 0D
pressure loss as evidence of separation, mixing, recirculation, local wall
loads, or other spatial flow structure.

## Current boundary

The initial capability is deliberately limited to steady, single-phase,
incompressible, constant-property, passive networks. Pumps, valve curves,
active controls, compressibility, heat transfer, storage, and transients raise
outside the current API rather than being silently approximated. These are
future capabilities and will receive their own equations, contracts, and
verification evidence.

Run the complete example:

```bash
python examples/zero_d_pipe_network/case.py
```
