# AgentCFD Workflow

Application code should keep the engineering sequence visible:

```text
Study -> Domain/Mesh -> Regions -> Fluid/Thermodynamics
      -> Boundaries/Sources/Zones -> Procedure -> Step
      -> Provider -> SimulationResult -> Acceptance
```

## Standard sequence

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
    applicability, required outputs, and reference or benchmark claims.
18. Return one SimulationResult whose acceptance state and provenance travel
    with every artifact.
19. Run mesh, timestep, iterative, and model-form sensitivity appropriate to
    the decision before claiming engineering verification.
20. Admit observations to campaigns or learning datasets only under a named
    quality policy.

## Progressive disclosure

The core workflow is Study, Domain/Regions, Fluid, Boundaries/Sources, Model,
Step, Result, and Acceptance. Campaigns, coupling, combustion, multiphase flow,
inverse problems, and learned computation are advanced layers. Backend
dictionaries, command lines, mesh tags, and field-file parsing are provider
implementation details but remain available for expert inspection.

## OpenFOAM route

For the experimental first provider, validation precedes
`OpenFOAMProvider.prepare(...)`. Preparation writes a new case directory and a
content-addressed manifest. It never overwrites an existing case. Execution
requires user-managed `blockMesh` and `simpleFoam` commands. Until actual
mesh-field mass balance and pressure loss are recovered, a successful process
must still produce an unaccepted SimulationResult.

## Reliability principle

An agent may translate intent, inspect capabilities, prepare cases, diagnose
failures, and propose repairs. Deterministic solvers compute the fields. Named
checks decide acceptance. Humans can inspect the same public model and evidence;
no essential scientific decision may exist only inside an AI conversation.
