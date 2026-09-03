# Validation policy

Completion is not acceptance. Every `SimulationResult` distinguishes:

- whether the provider executed;
- whether its numerical procedure converged;
- whether required outputs exist;
- whether conservation and applicability checks passed;
- which exact model and provider produced the data.

Provider-specific acceptance limits are inputs, not hidden constants. The
OpenFOAM pipe provider records an explicit `OpenFOAMValidationPolicy` alongside
the mesh controls, model, procedure, output request, and lowered-case identity.

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
is retained as a diagnostic difference, not mislabeled as discretization error:
this case prescribes a uniform inlet profile and includes the developing
entrance region. Its pressure-reference applicability check fails closed. The
fully developed inlet plus grid convergence is the validation path.

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

`agentcfd prepare openfoam-pipe-grid` creates the matching input side of this
workflow. It uses one fully developed public model for all three cases, scales
cross-section and axial counts by the same ratio, records each case hash and
expected cell count, and refuses a non-empty destination. The default bounded
benchmark is 0.5 m long and 0.1 m in diameter so the 4/8/16 by 20/40/80 family
contains 1,600, 12,800, and 102,400 cells without extreme axial aspect ratios.
`agentcfd run openfoam-pipe-grid` closes the loop: it verifies every prepared
case against the plan, executes the three fresh cases, writes a structured
result beside each case, and emits `agentcfd-grid-convergence.json`. A case
containing prior mesh, time, log, or post-processing output is rejected so two
executions cannot be silently mixed into one evidence record.

The emitted evidence applies an explicit `GridConvergencePolicy`: by default,
fine-grid relative GCI must be at most 2% and the GCI asymptotic ratio must lie
within 10% of unity. These are promotion gates rather than universal physical
constants. A completed study that misses them is still written for diagnosis,
but the CLI returns the completed-unaccepted exit status `3`.

`agentcfd.verification.assess_validation_point` provides a dependency-free
single-observable screening calculation for later experimental benchmarks. It
combines declared numerical, input, and experimental standard uncertainties by
root-sum-square, applies an explicit coverage factor, and reports both absolute
and normalized discrepancy. Correlation and model-form uncertainty remain
study-specific; the helper does not claim full ASME V&V 20 conformity.
The same calculation is available as `agentcfd verify validation-point`; its
versioned contract is installed with the package and an outside-uncertainty
comparison returns the completed-but-unaccepted exit status `3`.

For the pipe benchmark, a uniform inlet and a fully developed analytical inlet
are different scientific problems. Total inlet-to-outlet static pressure from
the uniform case includes an entrance contribution and is not promoted as a
Hagen--Poiseuille validation observable. The declared fully developed profile
is the canonical path for isolating spatial discretization error.
