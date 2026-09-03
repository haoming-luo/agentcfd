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

## Gas screening

The ideal-gas density, calorically perfect-gas speed of sound, and Mach number
are available for preliminary regime screening. They use explicit pressure,
temperature, specific gas constant, and heat-capacity ratio inputs. Steam near
saturation and non-ideal gases require a property backend such as the optional
CoolProp IF97 provider; AgentCFD does not silently substitute the ideal-gas
relations.

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
