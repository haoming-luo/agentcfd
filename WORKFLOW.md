# AgentCFD Workflow

Application code should keep the engineering sequence visible:

```text
Study -> Domain/Mesh -> Regions -> Fluid/Thermodynamics
      -> Boundaries/Sources/Zones -> Procedure -> Step
      -> Provider -> SimulationResult -> Acceptance
```

## Standard sequence

At project level, the recommended lifecycle is:

```text
init -> edit case.py -> status -> run -> view
```

`status` collapses check, plan freshness, runtime readiness, run phase,
acceptance, post-processing target, and recovery into one decision surface.
The detailed `check`, `plan`, `inspect`, `storage`, verification, and export
commands remain available through progressive disclosure.

1. Declare the physical Study and modeling assumptions.
2. Select a numerical Procedure only after the physics is fixed.
3. Define or import the Domain and inspect its dimensions and identity.
4. Create named volume, boundary, interface, and observation Regions.
5. Require expected region coverage and mesh-quality criteria.
6. Define Fluid records and the transport or thermodynamic model with units and
   provenance.
7. Apply boundary conditions to named Regions.
8. Add volume Sources and Zone Models separately from boundaries.
9. Declare canonical fields, section quantities, wall quantities, histories,
   and artifacts required from the analysis.
10. Construct the Model and inspect its summary, capability requirements, and
    stable fingerprint.
11. Create the Step with Procedure, solver policy, output policy, progress, and
    checkpoint intent.
12. Validate the complete Step before generating backend files.
13. Ask the selected Provider whether it supports the exact resolved request.
14. Lower deterministically and retain a content-addressed provider manifest.
15. Execute without a shell, retain bounded logs, and classify addressable
    failures.
16. Recover canonical fields and quantities without leaking backend names into
    public output.
17. Evaluate numerical convergence, mass/momentum/energy/species conservation,
    applicability, required outputs, reference or benchmark claims, and
    explicitly declared quantity requirements.
18. Return one SimulationResult whose acceptance state and provenance travel
    with every artifact.
19. Publish volumetric results through the standard XDMF/H5/NPZ field bundle;
    keep native cell and interpolated point associations explicit. For broad
    campaigns, screen summary-only first and promote selected accepted points
    to full fields instead of permanently storing every volume solution.
20. Run mesh, timestep, iterative, and model-form sensitivity appropriate to
    the decision before claiming engineering verification.
21. Admit observations to campaigns or learning datasets only under a named
    quality policy.

## Progressive disclosure

The core workflow is Study, Domain/Regions, Fluid, Boundaries/Sources, Model,
Step, Result, and Acceptance. Campaigns, coupling, combustion, multiphase flow,
inverse problems, and learned computation are advanced layers. Backend
dictionaries, command lines, mesh tags, and field-file parsing are provider
implementation details but remain available for expert inspection.

## OpenFOAM route

For OpenFOAM providers, validation precedes deterministic lowering into a
disposable project workspace. Parametric and imported geometry both receive a
content-addressed case and mesh manifest; imported setup can start with
`init --template imported-internal-flow` so units, roles, interior seed,
velocity direction, mesh size, and cell budget are explicit before execution.
A standard circular elbow starts directly with `init --template
industrial-elbow`: the project owns an editable generator specification and
content-addressed STL/inspection derivations. A changed specification blocks
execution until preview-first `geometry-sync --apply` refreshes them; it never
changes an active run or starts a solver.
If an ASCII STL or OBJ carries names that cannot become stable OpenFOAM region
identifiers, `geometry-normalize` first publishes a deterministic preview and
can then write a new name-only copy. The original is never overwritten and the
copy must pass the ordinary geometry and role preflight; normalization is not
a mesh-readiness claim.
AgentCFD invokes the declared local/container runtime, recovers conservation,
mesh, convergence, and output evidence, and publishes XDMF/H5 only through the
same acceptance workflow. A zero solver exit never becomes acceptance by
itself.

## Reliability principle

An agent may translate intent, inspect capabilities, prepare cases, diagnose
failures, and propose repairs. Deterministic solvers compute the fields. Named
checks decide acceptance. Humans can inspect the same public model and evidence;
no essential scientific decision may exist only inside an AI conversation.
