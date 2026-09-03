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

Numerical capabilities will add, as appropriate:

- method of manufactured solutions;
- benchmark measurements or trusted reference data;
- mesh, timestep, iterative, and model-form sensitivity;
- mass, momentum, species, and energy balances;
- y-plus and wall-treatment checks;
- installed-runtime and parallel reproducibility evidence;
- documented counterexamples and unsupported regimes.
