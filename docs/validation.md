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

## Grid convergence

`agentcfd.verification.grid_convergence_index` implements a solver-neutral
three-grid Richardson extrapolation and Grid Convergence Index workflow. It
sorts grids by characteristic size, supports unequal refinement ratios,
reports the observed order and asymptotic ratio, and rejects oscillatory or
non-converging triples. This is numerical solution verification, not validation
against physical data. Geometric similarity and membership in the asymptotic
range remain explicit engineering responsibilities.

`agentcfd verify grid-convergence` accepts three serialized AgentCFD results.
It fails closed unless they are completed, numerically converged, share the
same model SHA-256, and expose a finite target quantity plus distinct positive
cell counts. Its versioned JSON evidence includes the observed order,
extrapolated value, fine/medium GCI, asymptotic ratio, and source-file hashes.

For the pipe benchmark, a uniform inlet and a fully developed analytical inlet
are different scientific problems. Total inlet-to-outlet static pressure from
the uniform case includes an entrance contribution and is not promoted as a
Hagen--Poiseuille validation observable. The declared fully developed profile
is the canonical path for isolating spatial discretization error.
