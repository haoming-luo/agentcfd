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
expected cell count, and refuses a non-empty destination. The bounded benchmark
is 0.5 m long and 0.1 m in diameter. The evidence-backed default is the 8/16/32
by 40/80/160 family containing 12,800, 102,400, and 819,200 cells. Lower-cost
counts remain explicit CLI inputs but are not presented as meeting the default
GCI promotion gate.
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

The OpenCFD v2606 validation run in
`docs/openfoam-v2606-grid-validation.json` passed that policy. The observed
order was 2.0443, the fine-grid relative GCI was 0.5079%, and the asymptotic
ratio was effectively 1.0. The 102,400-cell case had 1.5606% pressure-drop
error and the 819,200-cell case had 0.2873% error against Hagen--Poiseuille.
All three inlet flows matched the declared physical circular-pipe flow within
approximately `3.25e-12` relatively after discrete face-area normalization.
The 12,800-cell result remains intentionally unaccepted at 6.8126% pressure
error; the grid-family certificate, not every member, is the promoted evidence.

At the finest grid, OpenFOAM's normalized residuals for the analytically zero
transverse velocity components stalled above `1e-8` while axial velocity and
pressure residuals met the target, pressure drop was constant over the final
window, and relative mass imbalance was approximately `1.15e-10`. The bounded
axis-aligned pipe convergence route therefore gates axial residual, observable
stability, conservation, process completion, and output recovery together.
Their final linear residuals still gate convergence; only the ill-conditioned
normalized outer initial values are treated as diagnostics. This exception is
not generalized to bends, tees, or other providers.

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

## Turbulent smooth-pipe diagnostic

The first OpenCFD v2606 k-omega SST run is retained in
`docs/openfoam-v2606-turbulent-pipe-diagnostic.json`. It is a diagnostic
integration milestone rather than a validated turbulent model. The 3 m by
0.1 m smooth pipe used water at 1 m/s, Re 99,621, a 5% inlet turbulence
intensity, and a 0.007 m length scale. Its 38,400-cell O-grid completed 300
iterations in 14.86 s and satisfied:

- relative inlet-flow error `3.25e-12` and mass imbalance `5.27e-7`;
- final relevant initial residual at most `2.56e-4`;
- final five-sample pressure-drop range `2.73e-4`, below the explicit RANS
  stability limit `5e-4`;
- wall y-plus minimum/average/maximum `66.77 / 86.55 / 94.42`, within the
  declared wall-function regime;
- immutable OpenCFD v2606 container and native mesh identities, all requested
  fields, and complete runtime evidence.

The recovered Darcy friction factor was `0.0160336`, 10.94% below the
smooth-pipe Colebrook reference `0.0180040`. The result is `converged` but not
`accepted`: the flow-rate inlet has not been proven fully developed, and no
turbulent three-grid study yet separates entrance, discretization, iterative,
and model-form effects. The explicit applicability failure is the desired
behavior for autonomous AI use.

An exploratory geometrically similar 4,800 / 38,400 / 307,200-cell sequence
reduced the Colebrook difference from 14.46% to 10.94% and then 6.20%; total
wall times were 2.51 s, 13.42 s, and 133.77 s. The pressure-drop increments
grew from 9.48 Pa to 12.80 Pa, so the sequence was not in the asymptotic range
and `grid_convergence_index` correctly rejected it. This negative result shows
that finer mesh improves agreement but cannot yet support a GCI certificate;
near-wall resolution and developed-inlet evidence must be redesigned together.

The first periodic `simpleFoam` plus `meanVelocityForce` precursor is recorded
in `docs/openfoam-v2606-periodic-precursor-validation.json`. On the same c8
cross-section at Re 99,621 it removed the developing-inlet ambiguity, completed
in 2.29 s on 320 cells, held y-plus between 78.27 and 93.59, and reduced the
smooth-Colebrook friction difference to 4.81%. All declared precursor checks
passed and the developed `U`, `p`, `k`, `omega`, and `nut` fields are content
addressed. Same-resolution downstream mapping is now separately recorded in
`docs/openfoam-v2606-precursor-mapping-validation.json`. The 38,400-cell target
passed every declared gate with 1.05% pressure-gradient transfer difference
and `trust_level="verified"`. Its 3.81% Colebrook difference remains diagnostic;
wall-strategy-controlled grid and Reynolds-range validation remain open.
