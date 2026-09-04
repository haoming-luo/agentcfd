# Guide for AI agents

AgentCFD is operated through the same public Python workflow used by people.
Do not generate backend dictionaries until the resolved AgentCFD model passes
validation and the selected provider advertises the required capability.

## First run

```bash
python -m pip install -e .
agentcfd doctor --json
agentcfd capabilities --json
agentcfd demo pipe --output pipe-result.json
agentcfd prepare openfoam-pipe openfoam-pipe --json
python -m pytest -q
```

## Required behavior

1. State geometry, fluid properties, boundaries, procedure, and requested results.
2. Use SI units unless an API explicitly accepts another unit system.
3. Check provider capability before execution.
4. Preserve warnings, failed checks, provenance, and model fingerprints.
5. Never use a completed process as evidence that the physics is correct.
6. Do not train on a result unless `result.accepted` is true or a human explicitly
   records why a failed check is acceptable for that dataset.
7. Explain unsupported physics instead of silently changing the model.

## Current boundary

The laminar circular-pipe reference workflow is released. The bounded OpenCFD
v2606 fully developed laminar pipe has accepted 8/16/32 three-grid evidence.
Version 0.1.0a3 also exposes a diagnostic k-omega SST smooth-pipe slice with
explicit inlet turbulence inputs, wall functions, y-plus, friction, residual,
mass-balance, mesh, field, and runtime evidence. Its first real run converges
but is intentionally unaccepted because a developed inlet and turbulent grid
study have not passed. Do not extend either evidence record to another inlet,
OpenFOAM dialect, geometry, heat transfer, or rough wall. A generated case or a
zero process exit code is never a broader CFD claim.

The periodic circular-pipe precursor driven by OpenFOAM `meanVelocityForce`
can now be mapped into the downstream pipe through a content-addressed
`mapFields` contract. At the bounded c8 operating point, mapping transfer error
against the precursor pressure gradient is 1.05% and the result is verified.
The separate 3.81% Colebrook difference remains a model-form diagnostic; do
not extend this evidence to another Reynolds number, wall strategy, geometry,
or OpenFOAM dialect.

The fixed-wall-cell precursor workflow accepts an explicit
`nominal_wall_cell_fraction`. At Re 99,621, the 0.0625 c8/c16/c32 family kept
mean y-plus within 43.63--44.39 and its fine-pair pressure-gradient change was
0.630% after 50-sample stability checks. Use `verify turbulent-wall-study` for
this evidence. Never pass this family to Richardson/GCI: holding wall height
fixed changes radial grading, even though the final pressure-gradient sequence
is monotonic.

For a geometrically similar periodic precursor candidate, use
`verify turbulent-precursor-grid-study`; its characteristic size is
`h/D = 1/cross_section_cells`, not total cells to the `-1/3` power. The first
uniform c8/c12/c18 candidate retained wall y-plus above 30 but was oscillatory,
so its 0.212% fine-pair plateau is not a GCI or uncertainty certificate.

Use `openfoam-turbulent-wall-function-study` to isolate the supported SST
momentum wall functions on one mesh. At Re 99,621, Spalding reduces the c16
smooth-Colebrook difference to 1.851%. Its tighter-solver fixed-wall c16/c32
pressure-gradient change is only 0.00689%, with 1.851% and 1.858% correlation
differences. This is a plateau, not evidence that refinement removes model-form
difference.

The periodic precursor also supports standard k-epsilon paired with
`epsilonWallFunction` and `nutkWallFunction`. Use
`openfoam-turbulent-model-study` to hold mesh and non-model inputs fixed; its
first c16 screen finds 1.851% for SST/Spalding and 3.289% for k-epsilon/nutk.
Both are accepted at this point, but the assessment intentionally refuses
general default promotion. Downstream mapping remains SST-only.

For a Reynolds sweep, pass `--target-y-plus` to each
`prepare openfoam-turbulent-model-study` call. The resulting plan contains a
prediction-only wall-resolution screen; never replace the solved y-plus gate
with that estimate. Aggregate at least three point assessments with `verify
turbulent-model-sweep`. Cross-Re meshes may adapt wall spacing, but each point's
two model cases must retain one identical native mesh and the same non-model
inputs. The current four-point evidence changes winner between Re 99,621 and
199,242. Treat `diagnostic-ranking-only`, `model_selection_required=true`, or a
false range gate as an instruction to run sensitivity/validation—not as
permission to choose the aggregate ranking.
