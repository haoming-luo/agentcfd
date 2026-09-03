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

Case generation is deterministic and does not require OpenFOAM. The manifest
stores the public model SHA-256, every generated file SHA-256, the combined case
identity, provider name, process boundary, and license posture.
Its machine contract is `schemas/openfoam-case.schema.json`.

## Safety and failure behavior

The provider validates the public model before lowering and rejects energy,
compressibility, turbulence, reaction, wall roughness, invalid OpenFOAM patch
names, and multiple wall patches. It refuses to write into a non-empty
directory. Unsupported physics is never silently simplified.

Execution requires both `blockMesh` and `simpleFoam` on `PATH`. Commands are
passed as argument lists without a shell. Their combined output is retained as
`log.blockMesh` and `log.simpleFoam`.

The initial execution result is deliberately not accepted. Process success and
the solver end marker are useful evidence, but mesh-field mass balance and
pressure-loss recovery are still mandatory. The analytical pressure drop is
reported only under `reference.flow.pressure_drop`, never as an OpenFOAM field
result.

An OpenCFD v2606 container execution has been completed on Linux/arm64. Its
machine-readable evidence is retained in `docs/openfoam-v2606-validation.json`.
This proves the generated case can be meshed, checked, solved, and manually
post-processed; it does not promote the provider beyond experimental status.

## Promotion gate

Before this provider advances beyond experimental maturity it must add:

1. OpenFOAM Foundation and OpenCFD dialect/version detection;
2. mesh checks from an actual `blockMesh` run;
3. patch-integrated inlet/outlet mass-flow recovery;
4. area-averaged pressure loss in physical Pa;
5. comparison with the independent Hagen–Poiseuille reference;
6. mesh-convergence evidence and a documented entrance-length policy;
7. installed-runtime tests on Linux and macOS, plus Windows through WSL2;
8. restart, failure taxonomy, and bounded log/result artifacts.
