# OpenFOAM provider boundary

## Released truth

`agentcfd.providers.OpenFOAMProvider` currently lowers one bounded case:

- steady, incompressible, isothermal, Newtonian, laminar internal flow;
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
compressibility, turbulence, reaction, wall roughness, invalid OpenFOAM patch
names, and multiple wall patches. It refuses to write into a non-empty
directory. Unsupported physics is never silently simplified.

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
timeout fires, AgentCFD force-stops only that exact container and removes the
CID file, preventing an abandoned solver from consuming resources in the
background.
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
axis-aligned pipe only, analytically zero transverse-velocity residuals remain
diagnostics while axial velocity and pressure gate convergence. The final five
pressure-drop samples must also stay within a relative range of `1e-4` by
default, so algebraic convergence cannot hide a drifting engineering result.
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

Prepared-case manifests bind the model, procedure, and output request through
an analysis SHA-256 in addition to hashing every generated file. Reusing a case
with changed iteration tolerance or requested outputs therefore fails before
execution instead of attaching false scientific inputs to an old case.

An OpenCFD v2606 container execution has been completed on Linux/arm64. Its
machine-readable evidence is retained in `docs/openfoam-v2606-validation.json`.
This proves the generated case can be meshed, checked, solved, and manually
post-processed; it does not promote the provider beyond experimental status.

## Promotion gate

Before this provider advances beyond experimental maturity it must add:

1. OpenFOAM Foundation distribution detection and separately validated dialects;
2. repeat the accepted fully developed three-grid evidence on another supported
   platform and on any newly supported OpenFOAM dialect;
3. a documented uniform-inlet entrance-length policy;
4. installed-runtime tests on Linux and macOS, plus Windows through WSL2;
5. restart, failure taxonomy, and bounded log/result artifacts.
