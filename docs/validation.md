# Validation policy

Completion is not acceptance. Every `SimulationResult` distinguishes:

- whether the provider executed;
- whether its numerical procedure converged;
- whether required outputs exist;
- whether conservation and applicability checks passed;
- which exact model and provider produced the data.

The first executable workflow is deliberately narrow. It checks the laminar
Reynolds-number range, mass balance, and consistency between Hagen–Poiseuille
and Darcy–Weisbach relations. It is a reference solution, not a claim that
general mesh-based CFD is already implemented.

## OpenFOAM v2606 execution evidence

The first external-solver evidence run is recorded in
`docs/openfoam-v2606-validation.json`. An official OpenCFD v2606 Linux/arm64
container generated and checked a 128,000-cell circular-pipe mesh, then ran
`simpleFoam` for all 500 configured iterations. The process ended normally and
the final linear-solver residuals were below `1e-8`, but the stricter outer
residual-control condition did not stop the run early. It is therefore recorded
as completed numerical evidence, not as a strictly converged or scientifically
accepted result.

Patch-integrated inlet and outlet volume flow differed by approximately
`2.56e-10` relatively. The recovered pressure drop was `2.834396 Pa`, about
`10.50%` above the fully developed Hagen–Poiseuille reference. That difference
is not treated as validation failure or success yet: this case prescribes a
uniform inlet profile and includes the developing entrance region, while mesh
convergence and an entrance-length policy remain future promotion gates.

Numerical capabilities will add, as appropriate:

- method of manufactured solutions;
- benchmark measurements or trusted reference data;
- mesh, timestep, iterative, and model-form sensitivity;
- mass, momentum, species, and energy balances;
- y-plus and wall-treatment checks;
- installed-runtime and parallel reproducibility evidence;
- documented counterexamples and unsupported regimes.
