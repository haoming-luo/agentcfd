# OpenFOAM provider boundary

## Released truth

`agentcfd.providers.OpenFOAMProvider` currently lowers two bounded slices of
one geometry:

- steady, incompressible, isothermal, Newtonian internal flow;
- one smooth circular pipe;
- one velocity or mass-flow inlet, one pressure outlet, and one no-slip wall;
- a full three-dimensional five-block O-grid generated with `blockMesh`;
- explicit circular arcs on the eight outer end-face edges, avoiding the
  inscribed-square geometry produced when projected vertices are used alone;
- `simpleFoam` dictionaries with kinematic pressure and SI properties.
- uniform mean-velocity, mass-flow-derived mean-velocity, or an explicitly
  declared fully developed laminar profile inlet;
- in-run `surfaceFieldValue` histories for both patch flows and both
  area-averaged patch pressures.

The experimental RANS slice additionally requires a public
`turbulence="k-omega-sst"` study and `turbulent_mean_velocity_inlet`. The inlet
stores mean velocity, turbulence intensity as a fraction, and turbulence length
scale in metres. The provider lowers these to an exact physical volume-flow
constraint, `kOmegaSST`, explicit inlet `k` and `omega`, `kqRWallFunction`,
`omegaWallFunction` with binomial blending, and `nutUBlendedWallFunction`.
It requests and recovers native `k`, `omega`, and `nut` fields plus wall y-plus.

Case generation is deterministic and does not require OpenFOAM. The manifest
stores the public model SHA-256, every generated file SHA-256, the combined case
identity, provider name, process boundary, and license posture.
Its machine contract is `schemas/openfoam-case.schema.json`.

The CLI exposes `--cross-section-cells` and `--axial-cells` on both `prepare`
and `run`. The exact controls are retained in the result's scientific inputs;
changing resolution therefore changes the case identity and remains auditable.
For formal refinement work, `prepare openfoam-pipe-grid` creates three
same-model cases and a versioned `agentcfd-grid-study.json` plan; it prevents
manual boundary edits from being mistaken for a valid GCI family.
`run openfoam-pipe-grid` verifies each plan/case identity, performs all three
runs, and feeds their serialized pressure-drop quantities into the same
result-based GCI implementation used by `verify grid-convergence`.

## Safety and failure behavior

The provider validates the public model before lowering and rejects energy,
compressibility, reaction, wall roughness, turbulence models other than the
bounded k-omega SST slice, invalid OpenFOAM patch names, and multiple wall
patches. Laminar cases reject turbulent outputs and inlets; turbulent cases
require the explicit turbulent inlet and Reynolds number at least 4000. It
refuses to write into a non-empty directory. Unsupported physics is never
silently simplified.

Execution requires `blockMesh`, `checkMesh`, and `simpleFoam` on `PATH`. Commands are
passed as argument lists without a shell. Their combined output is retained as
`log.blockMesh`, `log.checkMesh`, and `log.simpleFoam`.

An existing generated case can be executed with `run openfoam-pipe --prepared`.
Before launching any command, AgentCFD checks the model SHA, every generated
file SHA, the combined case SHA, and that all manifest paths remain inside the
case directory. Changed, missing, mismatched, or path-escaping cases fail with
`CaseIntegrityError`; AgentCFD never silently blesses a hand-edited case.
Case and grid-study manifests use strict JSON parsing that rejects duplicate
keys, non-finite numbers, and non-object roots before identity checks.
The executed result records mesh controls recovered from the verified
`system/blockMeshDict`, rather than trusting callers to repeat the original
mesh options correctly. Unrecorded files, directories, and symbolic links are
also rejected because inputs such as an added `fvOptions` can change the
equations without changing any recorded file.

Native commands and an explicit Docker image are both supported. Container
mode mounts only the selected case at `/case`; it does not infer, download, or
silently switch images. This makes the same provider usable from Linux,
macOS, CI, and future remote workers while preserving runtime provenance.
After execution, AgentCFD inspects the local Docker image and records its
immutable image SHA-256, repository digests, operating system, and architecture.
A container run without a verifiable immutable image identity is not accepted.
Each container command also uses a dedicated Docker CID file. If the client
timeout or keyboard interruption fires, AgentCFD force-stops only that exact
container and removes the CID file, preventing an abandoned solver from
consuming resources in the background.
The CLI exposes this per-command limit as `--timeout-seconds` for both single
cases and three-grid studies; invalid non-positive values fail before execution.

Scientific acceptance is currently version-gated to the tested OpenCFD `2606`
dialect. An unknown or different runtime can still produce diagnostic evidence,
but the result is not promoted as accepted until that distribution and version
has its own validation evidence.

Result recovery reads the four independent patch histories, converts OpenFOAM
kinematic pressure to Pa, checks relative mass imbalance, compares pressure
drop with Hagen--Poiseuille at the recovered flow rate, and attaches the final
native `U` and `p` fields. `checkMesh` observables are also structured. A normal
`End` proves completion only. Numerical convergence requires relevant equation
residuals, pressure-drop stability, and conservation; an explicit SIMPLE marker
is retained when present but is not sufficient on its own. For the bounded
axis-aligned pipe only, the normalized outer initial residuals for analytically
zero transverse velocity are replaced by their final linear residuals; those
components still gate convergence together with axial velocity and pressure.
The final five
pressure-drop samples must also stay within a relative range of `1e-4` for
laminar validation and `5e-4` for the RANS slice by default. These are separate
observable-stability limits; neither changes the 2% laminar reference-accuracy
gate.
Command-level return codes and monotonic wall-clock durations are also retained,
supporting timeout diagnosis and performance regression tracking without
treating performance as scientific convergence.

The provider records the resolved axial and cross-section counts recovered
from the generated case, verifies `checkMesh` returned the corresponding cell
count, and writes a content-addressed `agentcfd-mesh.json` over every native
`polyMesh` file. Final `U` and `p` records carry that mesh SHA-256, preventing a
field from being silently paired with another mesh in downstream AI or FEM
workflows.
In addition to OpenFOAM's `Mesh OK`, default explicit promotion limits require
maximum non-orthogonality at most 65 degrees, skewness at most 4, and aspect
ratio at most 50. Missing observables fail closed and all limits are recorded
with the scientific inputs.

`OpenFOAMValidationPolicy` makes the scientific thresholds auditable. Its
defaults require relative mass imbalance no greater than `1e-6` (or ten times
the requested iterative tolerance, whichever is larger) and recovered-flow
pressure-drop error no greater than 2%. It also requires the recovered inlet
flow to match the public mean-velocity or mass-flow request within 1%, exposing
coarse-face integration error instead of hiding it by renormalizing only the
pressure reference. The exact policy is retained in every result's scientific
inputs. A project may declare different thresholds, but it cannot change them
after the run without changing the recorded evidence.

The provider validates requested output names before case generation and checks
their recovery after execution. Missing final velocity or pressure fields, or
missing requested histories, therefore make the result unaccepted even when the
solver process exits normally.

`mean_velocity_inlet` remains uniform and therefore includes entrance effects.
Uniform velocity and mass-flow-derived inlets also report Reynolds number, the
`0.05 Re D` hydrodynamic entrance-length estimate, and pipe/entrance-length
ratio as diagnostics. These quantities explain the physical mismatch without
making the fully developed pressure law an acceptance shortcut.
For this boundary, the Hagen--Poiseuille difference is tagged diagnostic and a
separate reference-applicability check fails; it is not presented as pure CFD
error. The pressure-error validation threshold applies only to the declared
fully developed profile.
The separate `fully_developed_velocity_inlet` uses OpenCFD's non-compiling
expression parser to prescribe the radial Hagen--Poiseuille profile. The
profile is normalized by its discrete patch-area integral so its volume flow
equals the public `mean velocity * physical circular area` request even on a
coarse polygonal patch. It is explicit in the model fingerprint and is never
selected by backend guesswork.

For the RANS slice, the provider computes inlet turbulence state using
`k = 3/2 (I U)^2` and `omega = sqrt(k)/(Cmu^0.25 L)`, matching OpenFOAM's
documented k-omega SST initialization convention. The public output preset
`turbulent_internal_flow()` requires velocity, pressure, `k`, `omega`, `nut`,
mass balance, pressure drop, and wall y-plus. Mean wall y-plus must fall in the
declared wall-function range `[30, 300]`; all samples remain available as
histories. The derived Darcy friction factor is compared with the smooth-pipe
Colebrook relation under a separate 15% diagnostic gate.

That friction comparison is not yet a validation claim. A flow-rate inlet
develops inside the pipe, while the correlation is a fully developed bulk-flow
reference. `pressure-reference-applicability` therefore fails until a
developed turbulent inlet and grid evidence are supplied, even if execution,
convergence, y-plus, conservation, and the diagnostic friction threshold pass.
The developed circular-pipe precursor instead uses a periodic one-layer O-grid
with `simpleFoam` and `meanVelocityForce`. Its case, analysis, mesh, and
container identities, target flow, pressure gradient, wall treatment,
turbulence model, and final `U`, `p`, `k`, `omega`, and `nut` fields are retained.
OpenCFD v2606 `boundaryFoam` was tested and rejected for this geometry because
its source requires at most two mutually parallel wall faces; silently using
it for a circular perimeter would be operationally invalid.

The first c8 precursor run completed in 2.29 s with 320 cells. It met the
target bulk velocity, residual, pressure-gradient stability, mesh, field,
container, and high-Re wall-function checks. Its y-plus range was 78.27 to
93.59 and its Darcy friction factor was 0.0171387, 4.81% below smooth
Colebrook, versus 10.94% for the earlier developing-pipe run on the same
cross-section. This verifies the experimental precursor workflow but does not
replace a turbulent grid or Reynolds-range validation certificate.

An accepted precursor can be supplied to the turbulent pipe provider through
`--precursor-case`. AgentCFD verifies the source result, model, resolution,
runtime, mesh, and SHA-256 identities of `U`, `p`, `k`, `omega`, and `nut`, then
freezes them into `agentcfd-precursor-map.json`. Execution adds `mapFields`
between mesh checking and `simpleFoam`; source identity is checked both before
and after mapping. The target retains an exact flow-rate velocity boundary,
uses zero-gradient `k` and `omega` to extend the mapped internal state, and
solves velocity to its absolute linear tolerance.

The final 38,400-cell mapped target completed in 12.73 s. Its pressure loss was
259.31 Pa versus 256.62 Pa obtained by integrating the precursor gradient, a
1.05% transfer difference below the 2% gate. The maximum gated final residual
was `9.38e-5`, y-plus was 78.32 / 87.68 / 94.33, and every declared mapping
check passed. Its 3.81% difference from smooth Colebrook is retained separately
as model-form diagnostic evidence; this is verification of deterministic field
transfer, not Reynolds-range physical validation.

Prepared-case manifests bind the model, procedure, and output request through
an analysis SHA-256 in addition to hashing every generated file. Reusing a case
with changed iteration tolerance or requested outputs therefore fails before
execution instead of attaching false scientific inputs to an old case.

An OpenCFD v2606 container execution has been completed on Linux/arm64. Its
machine-readable evidence is retained in `docs/openfoam-v2606-validation.json`.
This proves the generated case can be meshed, checked, solved, and manually
post-processed; it does not promote the provider beyond experimental status.

The first k-omega SST execution is recorded in
`docs/openfoam-v2606-turbulent-pipe-diagnostic.json`. It completed 300
`simpleFoam` iterations on 38,400 cells in 14.86 s total wall time, passed mesh,
mass-balance, requested-output, RANS-residual, pressure-stability, and y-plus
checks, and reached `trust_level="converged"`. It remains scientifically
unaccepted because the developed-inlet applicability gate has not passed. Its
10.94% friction difference is diagnostic, not a discretization-error claim.

## Promotion gate

Before this provider advances beyond experimental maturity it must add:

1. OpenFOAM Foundation distribution detection and separately validated dialects;
2. repeat the accepted fully developed three-grid evidence on another supported
   platform and on any newly supported OpenFOAM dialect;
3. a documented uniform-inlet entrance-length policy;
4. installed-runtime tests on Linux and macOS, plus Windows through WSL2;
5. restart, failure taxonomy, and bounded log/result artifacts.
6. a wall-strategy-controlled turbulent three-grid study and public smooth-pipe
   friction benchmark across a declared Reynolds-number range.
