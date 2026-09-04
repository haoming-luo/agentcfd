# Numerical strategy for industrial internal flow

AgentCFD treats boundary conditions, numerical resolution, and acceptance as
parts of the scientific model. A faster run is useful only when its intended
decision and uncertainty are explicit.

## Inlet strategy

Use the least ambiguous inlet that represents the physical problem:

- **Laminar circular-pipe verification:** use the analytical
  Hagen--Poiseuille profile and normalize it by the discrete OpenFOAM patch
  integral. The profile is known exactly, so running another solver to discover
  it would add cost and uncertainty. Discrete normalization makes the requested
  physical volume flow exact even when the circular boundary is polygonal.
- **General prescribed industrial flow:** use OpenFOAM's
  [`flowRateInletVelocity`](https://doc.openfoam.com/2306/tools/processing/boundary-conditions/rtm/derived/inlet/flowRateInletVelocity/)
  when mass or volume flow is the measured input. Its default plug profile and
  optional extrapolated-profile behavior must remain explicit in the AgentCFD
  model because they describe different entrance physics.
- **Developed turbulent duct or pipe flow:** use
  [`boundaryFoam`](https://doc.openfoam.com/2606/tools/processing/solvers/rtm/incompressible/boundaryFoam/)
  to generate a compatible developed mean velocity and turbulence state when
  no trusted analytical profile exists. Store the precursor case identity,
  target bulk flow, turbulence model, wall treatment, and mapped fields as
  scientific inputs.

The existing analytical expression uses OpenFOAM patch `weightSum` support,
documented in the
[`patchExpr` syntax](https://doc.openfoam.com/2606/fundamentals/input-types/expression-syntax/),
to impose the target physical flow on the generated faces.

## Resolution and performance tiers

The validated OpenCFD v2606 pipe family supports three distinct uses. These
numbers apply only to the frozen 0.5 m by 0.1 m laminar benchmark; they are not
universal mesh recommendations.

| Tier | Cells | Measured pressure error | Runtime | Intended use |
|---|---:|---:|---:|---|
| screening | 12,800 | 6.8126% | 5.1 s | workflow and sensitivity checks; not accepted |
| engineering | 102,400 | 1.5606% | 28.3 s | routine accepted result for this benchmark |
| verification | 819,200 | 0.2873% | 390.1 s | release evidence and grid certificate |

The formal 8/16/32 study achieved observed order 2.0443 and fine-grid GCI
0.5079%. AgentCFD therefore defaults formal GCI work to the complete family,
while normal repeated engineering runs should prefer the middle grid after a
problem-specific mesh-sensitivity check. The fine grid costs roughly fourteen
times the middle-grid wall time here and should not be the default for every
campaign member.

The GCI calculation follows NASA's
[`three-grid spatial-convergence procedure`](https://www.grc.nasa.gov/www/wind/valid/tutorial/spatconv.html)
and uses the recommended three grids and 1.25 safety factor. GCI estimates
discretization uncertainty; agreement with an analytical or experimental
reference remains a separate validation question.

## Turbulent internal-flow promotion sequence

The incompressible provider slice is deliberately staged:

1. **implemented:** add explicit RANS study, turbulence-inlet, field, history,
   capability, and result contracts;
2. **implemented diagnostically:** lower `kOmegaSST` with blended wall
   functions and report wall `y+`, residuals, friction, conservation, runtime,
   and native turbulence fields;
3. verify developed turbulent pipe friction over documented Reynolds-number
   and roughness ranges, including grid and iterative sensitivity;
4. validate a public separated-flow case before promoting bends and tees;
5. validate pressure loss and flow split for bends, tees, and manifolds against
   experimental data, retaining geometry and tap-location uncertainty.

No laminar convergence exception carries into this sequence. All velocity and
turbulence equations, mass balance, stable engineering observables, wall
treatment, and output recovery must pass together.

The first 38,400-cell diagnostic at Re 99,621 completed 300 iterations in
14.86 s. Its mean y-plus was 86.55, mass imbalance was `5.27e-7`, and final
relevant outer residual was `2.56e-4`. The flow-rate inlet reduced setup
ambiguity and exactly met bulk flow, but the 10.94% Colebrook friction
difference cannot yet be divided into entrance, grid, and model-form effects.
It therefore proves the integration and evidence pipeline, not turbulent
accuracy. The next performance target is a reusable `boundaryFoam` precursor
whose developed velocity, `k`, `omega`, flow rate, mesh, and provider identity
are content-addressed and reused across a three-grid family.

The first exploratory 4,800 / 38,400 / 307,200-cell family moved smooth-pipe
friction error from 14.46% to 10.94% to 6.20%, but failed the GCI monotonic-
increment requirement because the pressure-drop increments grew with
refinement. Its y-plus range also moved substantially (mean 168.06 to 86.55 to
43.92), confirming that a wall-function grid family must control near-wall
strategy rather than treating indiscriminate refinement as one unchanged
numerical problem.

## Steam and compressible-flow promotion sequence

The first steam slice will use steady `rhoSimpleFoam`-class compressible
pressure/velocity/temperature equations with optional CoolProp `IF97::Water`
input states. AgentCFD will freeze the thermodynamic state and property-provider
identity before case generation, reject two-phase or out-of-range states, and
record mass and energy balances rather than accepting on residuals alone.

Promotion proceeds from an adiabatic compressible pipe to a heated pipe and a
nozzle benchmark. Only after those gates pass should the product expose steam
networks, throttling equipment, heat exchangers, or conjugate AgentFEM coupling.
Phase change remains out of scope for this single-phase milestone.
