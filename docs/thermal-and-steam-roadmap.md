# Thermal flow and steam: bounded lowering before real-fluid breadth

AgentCFD has a solver-neutral thermal internal-flow contract and one deliberately
narrow OpenFOAM lowering. The released experimental slice is constant-property,
steady, incompressible, laminar circular-pipe flow with a non-zero prescribed
wall heat flux. It is not a compressible steam solver.

```python
from agentcfd import Model, boundaries, fluids, geometry, outputs, studies

water = fluids.newtonian(
    "water-at-operating-point",
    density=971.8,
    dynamic_viscosity=3.55e-4,
    specific_heat=4195.0,
    thermal_conductivity=0.67,
)

model = Model(
    name="heated-pipe",
    study=studies.internal_flow(energy=True),
    domain=geometry.circular_pipe(length=2.0, diameter=0.05),
    fluid=water,
).boundaries(
    inlet=boundaries.mean_velocity_inlet(0.5, temperature=353.15),
    outlet=boundaries.pressure_outlet(),
    wall=boundaries.no_slip_wall(
        thermal=boundaries.heat_flux_into_fluid(10_000.0)
    ),
)

step = model.step(output=outputs.thermal_internal_flow())
```

The heat-flux sign is stated in engineering language: positive means heat into
the fluid. Provider adapters own the conversion to each backend's outward-normal
gradient convention. `fixed_temperature(K)` and explicit `adiabatic()` are the
other released wall intents. All velocity, mass-flow, fully developed, turbulent,
and total-pressure inlets can carry absolute temperature in kelvin.

An energy model fails before provider work unless it has:

- positive specific heat and thermal conductivity;
- absolute temperature on every inlet;
- an explicit thermal condition on every no-slip or slip wall;
- the canonical `thermal.temperature` output field.

Conversely, temperature or wall-heat inputs on an isothermal study are rejected
instead of being ignored. Existing isothermal models serialize exactly as before
when the new optional values are absent.

## Property bridge

`CoolPropPropertyProvider.at_pressure_temperature(...)` already returns a
versioned, phase-labelled SI state. Its
`as_constant_property_fluid()` method now produces the exact density, viscosity,
specific heat, and conductivity required by the thermal model. Keep the original
state record in project evidence: converting one point to constant properties
does not make those properties valid over an arbitrary pressure-temperature
range and does not turn a single-phase model into a phase-change model.

## First OpenFOAM lowering

The pipe provider runs `simpleFoam` for pressure and velocity and OpenFOAM's
`scalarTransport` function object for a passive absolute-temperature field. It
sets `alpha = k/(rho cp)`, maps positive heat into the fluid to OpenFOAM's
outward-normal temperature gradient, and reports mass-flow-weighted inlet and
outlet temperatures. Area averaging is intentionally not used for the outlet:
in developed flow, near-wall temperature and velocity are correlated, so it
does not represent mixed-mean enthalpy.

Acceptance includes the existing mesh, convergence, mass, and pressure gates
plus `|mdot cp (Tout-Tin) - Qwall| / |Qwall|`. This one-way coupling is valid
only when temperature does not materially change momentum or properties;
buoyancy, viscous heating, phase change, radiation, and conjugate walls are
excluded. `fixed_temperature` and `adiabatic` remain public intent but are not
yet lowered by this first slice.

## OpenFOAM lowering sequence

The staged provider work is deliberately narrower than the public intent:

1. Complete a grid-identified constant-property heated-pipe validation record;
   then add fixed-temperature walls without inventing a heat-transfer coefficient.
2. Add steady compressible `rhoSimpleFoam` gas flow with absolute pressure,
   temperature, thermophysical identity, low-Mach/transonic screening, and
   mandatory mass/energy conservation.
3. Add a bounded superheated-steam operating envelope. IF97/CoolProp supplies
   independent reference properties and phase checks; no provider may cross the
   saturation boundary silently or present a one-point constant-property fit as
   a real-fluid model.
4. Add buoyancy only with gravity and hydrostatic-pressure intent; add conjugate
   heat transfer only after solid materials and interface conservation exist.
5. Treat combustion as a separate compressible, multi-species, chemistry and
   heat-release capability with its own validation matrix.

This sequence follows the OpenFOAM solver boundary: `rhoSimpleFoam` requires
pressure, velocity, temperature, turbulence and thermophysical models;
`buoyantSimpleFoam` additionally introduces hydrostatic pressure; documented
wall heat-transfer combinations distinguish adiabatic, fixed temperature and
fixed heat flux; and `reactingFoam` adds transient compressible multi-species
chemistry. AgentCFD will not collapse those into one misleading “thermal” switch.

Authoritative OpenCFD references:

- <https://doc.openfoam.com/2306/tools/processing/solvers/rtm/compressible/rhoSimpleFoam/>
- <https://doc.openfoam.com/2306/tools/processing/solvers/rtm/heat-transfer/buoyantSimpleFoam/>
- <https://doc.openfoam.com/2606/tools/processing/boundary-conditions/common-combinations/>
- <https://doc.openfoam.com/2606/tools/post-processing/function-objects/solvers/scalarTransport/>
- <https://api.openfoam.com/2606/classFoam_1_1functionObjects_1_1fieldValues_1_1surfaceFieldValue.html>
- <https://doc.openfoam.com/2606/tools/processing/solvers/rtm/combustion/>
