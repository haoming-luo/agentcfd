# Engineering correlations

These functions are setup checks and low-order engineering references. They do
not replace a CFD model and are not silently substituted for one.

## Pipe loss

`agentcfd.engineering` provides:

- hydraulic diameter, `D_h = 4 A / P_w`;
- bulk Reynolds number, `Re = rho U D_h / mu`;
- the laminar Darcy factor, `f = 64 / Re`;
- a bracketed solution of the implicit Colebrook--White relation for
  `Re >= 4000`;
- Darcy--Weisbach straight-run loss, `Delta p = f (L/D_h) rho U^2 / 2`;
- local loss, `Delta p = K rho U^2 / 2`.

`pipe_pressure_loss` combines these steps into one auditable record containing
the Reynolds number, declared regime, relative roughness, Darcy factor, major
loss, minor loss, and total pressure loss. Individual functions remain public
so an agent never has to reverse-engineer a bundled calculation.

`circular_pipe_operating_point` solves the inverse industrial question: given
available pressure loss, what mean velocity and volume flow can the circular
line carry? The laminar branch uses the exact linear-plus-local-quadratic form;
the turbulent branch brackets the Colebrook-dependent nonlinear loss. The
caller must declare `laminar` or `turbulent`, and a solution that falls into a
different or transitional regime is rejected.

For a uniform laminar inlet,
`laminar_hydrodynamic_entrance_length` exposes the screening estimate
`L_e = C Re D_h` with an explicit default `C = 0.05`. Published definitions
commonly put the coefficient around 0.05--0.06; the
[Cambridge engineering reference](https://www-mdp.eng.cam.ac.uk/web/library/enginfo/aerothermal_dvd_only/aero/fprops/pipeflow/node9.html)
uses approximately 0.06. The estimate diagnoses whether entrance effects are
likely important; it is not used as a substitute for a developing-flow
validation solution.

The interval `2300 <= Re < 4000` is rejected. AgentCFD will not turn a regime
choice into an undocumented interpolation. Density and viscosity are treated
as constant, and all returned pressure losses are positive magnitudes.

The Darcy--Weisbach form and a worked CFD comparison are documented by
[NASA NTRS](https://ntrs.nasa.gov/api/citations/20050209950/downloads/20050209950.pdf).
The implicit Colebrook--White residual is solved directly rather than replaced
by an unlabelled approximation. These correlations remain empirical
engineering evidence; their domain of applicability must travel with any
decision that uses them.

## Near-wall mesh screening

`friction_velocity`, `y_plus`, and `wall_distance_for_y_plus` expose the
standard relations `u_tau = sqrt(tau_w/rho)` and `y+ = y u_tau/nu`.
`turbulent_pipe_wall_resolution` combines them with the bulk Darcy factor to
estimate first-cell-centre distance and nominal first-cell thickness for a
target `y+`. The estimate is deliberately restricted to `Re >= 4000` and does
not replace post-processing the achieved local wall coordinate.

OpenFOAM's [wall-function guidance](https://doc.openfoam.com/2606/tools/processing/models/turbulence/ras/wall-functions/)
identifies approximately `y+ <= 1` for wall-resolved operation and `30 <= y+
<= 300` for high-Re wall functions, with the buffer layer between them being a
common source of modelling inconsistency. Those ranges are guidance, not
hard-coded acceptance thresholds; the selected wall treatment and the solved
`y+` distribution must be recorded together.

`turbulence_inlet_from_intensity` derives the common `k`, `omega`, and
`epsilon` inlet estimates from an explicit mean speed, fractional turbulence
intensity, and length scale. OpenFOAM documents
`k = 1.5 (I |U|)^2` and `omega = sqrt(k)/(C_mu^0.25 L)` for
[k-omega SST initialization](https://doc.openfoam.com/2606/tools/processing/models/turbulence/ras/linear-evm/rtm/kOmegaSST/).
The function does not guess an intensity or length scale and does not imply
that the experimental RANS provider has been promoted.

## Gas screening

The ideal-gas density, calorically perfect-gas speed of sound, Mach number, and
`screen_incompressible_flow` are available for preliminary regime screening.
The screen records both the calculated Mach number and the explicit policy
threshold; its default is Mach 0.3, below which NASA notes that compressibility
effects are very small in ordinary aerodynamic flow. This is a model-selection
warning rather than proof: large temperature or composition changes can still
make density variation important at low Mach number. See NASA's
[Mach-number guidance](https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/role-of-the-mach-number/)
and the supporting
[low-speed assessment](https://ntrs.nasa.gov/api/citations/19960045736/downloads/19960045736.pdf).

All gas functions use explicit pressure, temperature, specific gas constant,
and heat-capacity-ratio inputs. Steam near saturation and non-ideal gases
require a property backend such as the optional CoolProp IF97 provider;
AgentCFD does not silently substitute ideal-gas relations.

## Numerical verification

`agentcfd.verification.grid_convergence_index` implements the monotonic
three-grid Richardson/GCI workflow described in NASA's
[spatial-convergence tutorial](https://www.grc.nasa.gov/www/wind/valid/tutorial/spatconv.html?force_isolation=true).
ASME V&V 20 describes the broader CFD and heat-transfer accuracy framework;
the standard's current scope is summarized by
[ASME](https://www.asme.org/codes-standards/find-codes-standards/standard-for-verification-and-validation-in-computational-fluid-dynamics-and-heat-transfer/2009).

GCI estimates ordered discretization uncertainty only. It does not cover
iterative error, uncertain inputs, geometry error, model-form error, or
experimental uncertainty.
