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
- **Developed turbulent circular-pipe flow:** use a periodic one-layer O-grid
  with OpenFOAM's
  [`meanVelocityForce`](https://doc.openfoam.com/2212/tools/processing/numerics/fvoptions/sources/rtm/meanVelocityForce/)
  to drive `simpleFoam` to the declared bulk velocity. Store the precursor case,
  mesh, container, target flow, turbulence model, wall treatment, pressure
  gradient, and developed fields as scientific inputs. OpenCFD v2606
  `boundaryFoam` is not used for this geometry: its implementation is
  one-dimensional and rejects more than two mutually parallel wall faces, so
  it is appropriate for planar channels rather than circular cross-sections.

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
accuracy. The implemented periodic precursor now content-addresses developed
velocity, pressure, `k`, `omega`, `nut`, target flow, pressure gradient, mesh, runtime,
and provider identity. The next product gate is deterministic mapping of those
fields to the main pipe inlet, followed by a wall-strategy-controlled grid
family.

The first exploratory 4,800 / 38,400 / 307,200-cell family moved smooth-pipe
friction error from 14.46% to 10.94% to 6.20%, but failed the GCI monotonic-
increment requirement because the pressure-drop increments grew with
refinement. Its y-plus range also moved substantially (mean 168.06 to 86.55 to
43.92), confirming that a wall-function grid family must control near-wall
strategy rather than treating indiscriminate refinement as one unchanged
numerical problem.

The first periodic precursor closes the entrance-condition part of that
redesign. At Re 99,621, the c8 one-layer O-grid used only 320 cells and
completed in 2.29 s. It recovered y-plus 78.27 / 87.07 / 93.59 and a Darcy
friction factor of 0.0171387, 4.81% below the smooth Colebrook value. This is
materially better than the 10.94% difference in the developing 3 m pipe while
preserving the same c8 cross-section and wall treatment. A
c8/12/16/20/24/32 scan was non-monotonic and the finer members crossed below
the declared y-plus 30 high-Re limit; it is diagnostic evidence, not a GCI
family.

The redesigned mesh exposes `nominal_wall_cell_fraction`. AgentCFD treats this
as the first wall-adjacent cell width divided by the nominal outer O-grid
radial-edge length and solves OpenFOAM's end/start `simpleGrading` ratio from
the complete geometric series. This follows the official
[OpenFOAM blockMesh grading definition](https://doc.openfoam.com/2606/tools/pre-processing/mesh/generation/blockMesh/),
where expansion is end-cell width divided by start-cell width rather than a
per-cell growth factor. Holding the fraction at 0.0625 fixed the design
height at 1.65186 mm for c8/c16/c32. Real v2606 runs kept average y-plus within
43.63--44.39 (1.72% relative spread) and the full wall range within
38.46--47.86. The c16-to-c32 pressure-gradient change was 0.630%.

This is a wall-strategy and resolution-plateau certificate, not a formal GCI.
Early five-sample tail checks falsely accepted slow drift on the finer grids.
The production gate now requires 50 samples; after 1000/4000/6000 iterations,
the pressure gradients were 84.7583, 85.0938, and 85.6336 Pa/m and converged
monotonically. Fixed physical wall height nevertheless requires different
radial grading at each resolution, so the meshes are not
geometrically similar. `verify turbulent-wall-study` records both successful
wall control and failed uncertainty promotion rather than applying Richardson
extrapolation outside its assumptions. The remaining numerical gate is a
separate geometrically similar family that stays within one valid wall model,
or a declared wall-resolved SST family with its own verification policy.

A complementary uniform c8/c12/c18 family tests formal GCI eligibility while
remaining in the same high-Re wall-function range. The ratio is exactly 1.5,
the combined y-plus range is 34.34--93.59, and all three sources pass the new
50-sample stability gate. For this one-axial-layer periodic problem, AgentCFD
uses cross-section characteristic size `h/D = 1/N`; total cell count to the
`-1/3` power would incorrectly treat the extruded 2-D refinement as 3-D.
Measured pressure gradients were 85.5393, 85.0416, and 85.2219 Pa/m. The fine
pair differs by only 0.212%, but the sequence is oscillatory, so
`verify turbulent-precursor-grid-study` rejects Richardson extrapolation and
uncertainty promotion. This negative result is retained in
`openfoam-v2606-turbulent-gci-candidate.json` rather than converted into a
misleading GCI.

The next controlled variable is the OpenFOAM momentum wall function. On one
identical c16 mesh, the existing `nutUBlendedWallFunction`,
`nutUSpaldingWallFunction`, and `nutkWallFunction` produced smooth-Colebrook
differences of 5.634%, 1.851%, and 6.185%. A tighter-solver fixed-wall-height
Spalding c8/c16/c32 follow-up produced 1.746%, 1.851%, and 1.858% differences,
with only a 0.00689% fine-pair pressure-gradient change. This clean plateau
shows that further interior refinement does not remove the remaining model/
reference difference. Spalding is therefore the current smooth-pipe candidate,
not yet the global default: c16 checks at
Re 199,242 and 498,104 retain 3.07--3.20% correlation differences, and no
experimental uncertainty budget has been applied. AgentCFD exposes the choice,
hashes it into each case, and keeps default promotion false until the Reynolds
range and independent reference data are adequate.

The next model-form screen holds that c16 mesh and every non-model input fixed.
With the inner linear solves tightened independently of the outer SIMPLE
target, SST plus `nutUSpaldingWallFunction` differs from smooth Colebrook by
1.851%, while standard k-epsilon plus `nutkWallFunction` differs by 3.289%.
Both runs are accepted and remain in the high-Re wall-function range; k-epsilon
is modestly faster in repeated local runs. This supports SST/Spalding as the
candidate for the present smooth-pipe benchmark, not as a general-purpose
default. The study contract requires identical native mesh SHA-256, physical
inputs, numerical procedure, iteration budget, stability window, and y-plus
regime before comparison.

The c8 precursor is now mapped through a deterministic `mapFields` contract
into the 3 m target. The target pressure loss differs by 1.05% from the source
pressure gradient integrated over the target length, and all mapping,
convergence, conservation, wall, mesh, field, runtime, and identity gates pass.
This comparison isolates transfer consistency. The independent 3.81% smooth
Colebrook difference remains a model-form diagnostic and cannot be relabeled as
discretization or experimental validation.

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
