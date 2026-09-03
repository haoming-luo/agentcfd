# AgentCFD Concepts

This file defines the vocabulary that people, agents, CLIs, future GUIs, and
providers use. Backend terms such as an OpenFOAM dictionary name do not replace
these public concepts.

## Study

A physical declaration: internal or external flow, steady or transient,
compressible or incompressible, isothermal or energy-coupled, reacting or
non-reacting, laminar or turbulent. A Study says what is being modeled; it does
not select an OpenFOAM application or numerical algorithm.

## Solution Procedure

The numerical route for a Study. It owns steady/transient advancement,
tolerances, iteration policy, discretization intent, and convergence controls.
It must not silently change the physics declared by the Study.

## Domain and Region

The Domain is the physical flow volume and its geometry or mesh identity. A
Region is a named volume, surface, inlet, outlet, wall, interface, or observation
location. Regions are the stable public target for fluids, porous zones,
sources, boundaries, outputs, and coupling. Integer mesh tags and OpenFOAM patch
names are provider details derived from regions.

## Fluid and Thermodynamic Model

A Fluid record carries identity, properties, units, provenance, and applicable
state range. A constitutive or thermodynamic model defines how density,
viscosity, heat capacity, conductivity, enthalpy, and species behavior are
evaluated. A constant Newtonian fluid is one model, not the definition of all
fluids. Steam tables and reacting mixtures enter through explicit optional
property providers.

## Boundary, Source, and Zone Model

A Boundary states physical data on a named surface: velocity, mass flow,
pressure, wall, symmetry, heat flux, temperature, or another supported model.
A Source acts in a volume or equation, such as momentum resistance, heat
release, or species production. A Zone Model represents porous loss, rotating
frames, fans, or other localized physics. These remain separate so a provider
cannot confuse a boundary value with a volume force.

## Model

A readable registry of the Study, Domain, Regions, Fluids, Boundaries, Sources,
Zone Models, and metadata. It supports inspection, validation, and a stable
fingerprint. It must expose engineering intent rather than hide a generated
backend case. Fingerprints are emitted only after boundary cardinality and
metadata stability checks; unknown boundary objects, non-finite metadata, and
non-JSON context fail closed instead of entering an ambiguous hash.

## Analysis Step

A Step combines a validated Model, one Solution Procedure, requested outputs,
and execution policy. The Step is where provider capability is checked and the
public model is lowered. A Step may generate a case without running it.

## Provider

A deterministic implementation that lowers a supported Step to a numerical or
analytical engine. Every provider publishes a capability and maturity record,
rejects unsupported cases, preserves its version and license boundary, and
returns structured evidence. OpenFOAM is an external process provider; the
Hagen–Poiseuille solution is an in-process reference provider.

## Simulation Result

The scientific record of one attempt: status, convergence, canonical quantities
and fields, conservation and applicability checks, messages, artifacts, model
identity, provider identity, and provenance. OpenFOAM directories, VTK files,
plots, and logs are artifacts attached to the result rather than the result
abstraction itself.

## Completion, Convergence, and Acceptance

These are distinct states:

- completion means the provider returned;
- convergence means the declared numerical criteria were met;
- acceptance additionally requires every mandatory applicability,
  conservation, output, and validation check.

A zero process exit code is not scientific acceptance. A converged result can
remain unaccepted when field recovery, conservation, or applicability evidence
is missing.

## Trust Level

Trust is an evidence ladder independent of execution status: `not_computed`,
`computed`, `converged`, `verified`, and `validated`. Runtime checks can establish
that a solver ran and converged; verification checks test numerical or analytical
consistency; validation checks compare against trusted physical evidence. AI and
release automation must request an explicit minimum level instead of inferring
trust from a process exit code.

## Capability and Evidence Maturity

A capability name identifies a precise supported scope. Maturity advances from
planned to experimental, verified, and released only through formula checks,
manufactured or local verification, global integration, engineering benchmarks,
convergence evidence, and installed-artifact/platform gates. A familiar model
name such as `k-omega SST` never implies that all of those levels exist.

## Scientific Dataset and Learned Model

A dataset contains accepted simulation or experimental observations together
with units, layout, parameter schema, model/provider identity, applicability,
and provenance. A surrogate or neural operator is not complete until it carries
independent validation, an applicability guard, uncertainty semantics, and a
defined high-fidelity fallback. AI never silently repairs or replaces physics.
