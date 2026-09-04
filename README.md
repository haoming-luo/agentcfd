<p align="center">
  <strong>AgentCFD</strong><br>
  <em>AI-native computational fluid dynamics for humans and agents.</em>
</p>

# AgentCFD

AgentCFD is an open-source platform for readable, verifiable, and learning-ready
computational fluid dynamics. Its first engineering focus is industrial flow:
pipes and ducts, pressure loss, fluid and steam transport, heat transfer, and
later reacting flow and combustion.

AgentCFD was initiated by Haoming Luo in September 2026. It follows the product
principles established through AgentFEM, but it is a separate codebase with its
own fluid-mechanics language, providers, validation evidence, and release cycle.

> **Project status:** pre-alpha. The public workflow and circular-pipe reference
> solution are executable. Deterministic OpenFOAM laminar, k-omega SST, and
> bounded k-epsilon precursor generation, execution, mesh checks, result recovery, and
> pressure-loss evidence are experimental capabilities; every run must still
> earn acceptance.

## Why AgentCFD

- **AI-native CFD** — people, scripts, GUIs, and agents operate the same explicit
  engineering model instead of hiding scientific intent in generated files.
- **Industrial flow first** — internal flow, heat transfer, steam and reacting
  systems come before a broad but shallow solver catalog.
- **Results you can check** — applicability, conservation checks, provider
  identity, model fingerprints, and failure evidence travel with the result.
- **One run or thousands** — the same workflow is designed for a single analysis,
  parameter campaigns, reproducible datasets, surrogates, and neural operators.
- **Open provider boundary** — numerical engines can evolve without changing the
  public model; license and runtime boundaries remain visible.
- **FEM–CFD continuity** — versioned exchange records prepare pressure, traction,
  temperature, heat flux, and mesh motion for future AgentFEM coupling.

## First executable workflow

```python
from agentcfd import Model, boundaries, fluids, geometry, outputs, procedures, studies

model = Model(
    name="water-pipe",
    study=studies.internal_flow(),
    domain=geometry.circular_pipe(length=10.0, diameter=0.05),
    fluid=fluids.newtonian(
        "water",
        density=998.2,
        dynamic_viscosity=1.002e-3,
    ),
).boundaries(
    inlet=boundaries.mean_velocity_inlet(0.02),
    outlet=boundaries.pressure_outlet(),
    wall=boundaries.no_slip_wall(),
)

result = model.step(
    procedure=procedures.steady(),
    output=outputs.standard(),
).run(provider="reference")

result.require_accepted()
print(result.quantities["flow.pressure_drop"])
```

The reference provider implements the Hagen–Poiseuille solution, rejects flow
outside its declared laminar range, and records mass balance and an independent
Darcy–Weisbach identity check.

## Install

Install the published alpha from PyPI with Python 3.11 or newer:

```bash
python -m pip install agentcfd==0.1.0a3
agentcfd doctor
agentcfd demo pipe
```

## One project lifecycle

The current development version exposes the same readable project to people,
agents, CI, and future GUIs:

```bash
agentcfd init --template industrial-pipe my-flow
cd my-flow
agentcfd check --json
agentcfd plan --json
agentcfd run . --json
agentcfd inspect --json
```

`case.py` is the modeling source of truth. `agentcfd.toml` contains only
operational settings such as the default provider, run directory, container,
and mesh controls. Every execution publishes an immutable run directory with
the resolved plan, result, provider artifacts, and trust state. Validation is
attached evidence; it does not replace the engineering workflow.

Common pipe-loss screening is available without a CFD runtime:

```bash
agentcfd calculate pipe-loss --density 998.2 --viscosity 0.001002 \
  --length 10 --diameter 0.05 --velocity 0.02 --json
agentcfd calculate pipe-flow --density 998.2 --viscosity 0.001002 \
  --length 10 --diameter 0.05 --pressure-loss 2.56512 \
  --regime laminar --json
```

Gas-model screening and optional CoolProp/IF97 states use equally explicit
commands:

```bash
agentcfd calculate compressibility \
  --velocity 100 --speed-of-sound 400 --json
python -m pip install "agentcfd[properties]"
agentcfd properties state \
  --fluid IF97::Water --pressure 101325 --temperature 500 --json
```

Both return structured records rather than presentation-only text. Installed
AgentCFD/AgentCAE schemas can be discovered with `agentcfd contracts --json`.

For editable development from the repository:

```bash
git clone https://github.com/haoming-luo/agentcfd.git
cd agentcfd
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
agentcfd doctor
agentcfd demo pipe
```

## Prepare the first OpenFOAM case

OpenFOAM is the primary industrial solver direction. AgentCFD keeps it behind a
filesystem-and-subprocess provider boundary: the Apache-2.0 Python core writes
an ordinary case, while an OpenFOAM installation already managed by the user
does the GPL-licensed numerical work.

Generate a full three-dimensional O-grid pipe case without requiring OpenFOAM:

```bash
agentcfd prepare openfoam-pipe openfoam-pipe --json
```

Or use the public provider from Python:

```python
from agentcfd.providers import OpenFOAMProvider

step = model.step(procedure=procedures.steady(), output=outputs.standard())
case = OpenFOAMProvider().prepare(step, "openfoam-pipe")
print(case.case_sha256)
```

The generated manifest binds every case file to its content hash and the public
model fingerprint. Execution currently targets installations that provide
`blockMesh`, `checkMesh`, and `simpleFoam`. AgentCFD recovers inlet/outlet flow,
area-averaged pressure, pressure drop, mass imbalance, mesh-quality metrics,
convergence evidence, and final native fields. Process completion alone never
implies scientific acceptance.

## Portable fields: XDMF/H5 and NPZ

Install the permissively licensed optional I/O stack, then export every saved
OpenFOAM field frame into one versioned bundle:

```bash
python -m pip install "agentcfd[io]"
agentcfd export openfoam OPENFOAM_CASE fields \
  --container-image opencfd/openfoam-run:2606 \
  --profile visualization \
  --field fluid.velocity --field fluid.pressure --json
agentcfd verify field-bundle fields --json
agentcfd export field-sample fields velocity-final.npz \
  --field fluid.velocity --association point --frame -1
```

`fields.xdmf` plus `fields.h5` is the standard mesh-and-field route for
ParaView, AgentFEM exchange, and other scientific tools. `fields.npz` mirrors
the same geometry, topology, axis, point fields, and native cell fields without
pickles for NumPy, PyTorch, JAX, and dataset pipelines. `manifest.json` retains
canonical field names, units, association, interpolation semantics, source
identity, and artifact hashes. Incompressible OpenFOAM `p` remains available
as kinematic pressure; physical pressure in Pa is a separate density-derived
field rather than a silent reinterpretation.
The optional `field-sample` command extracts one frame into AgentFEM's
`coordinates`, `values`, `encoding_json`, and `metadata_json` NPZ layout. It
opens directly with `agentfem.datasets.FEMFieldSample.read(...)`, or with
`numpy.load(..., allow_pickle=False)`, without adding AgentFEM as a dependency.

Portable output is intentionally profiled instead of dumping every array:
`visualization` writes selected interpolated point fields, `native` writes
selected OpenFOAM cell fields for verification/training, and `both` is the
explicit expert interchange mode. The CLI defaults to `visualization`; project
output follows `OutputRequest.portable_profile` and canonical `fields`.

For the canonical fully developed validation case, declare the physical inlet
profile instead of silently changing a mean-velocity boundary:

```python
model.boundaries(
    inlet=boundaries.fully_developed_velocity_inlet(0.02),
    outlet=boundaries.pressure_outlet(),
    wall=boundaries.no_slip_wall(),
)
```

Or run the bundled case end to end against an installed runtime:

```bash
agentcfd run openfoam-pipe openfoam-pipe --fully-developed --json
```

On macOS or another host without native OpenFOAM commands, use the official
OpenCFD container directly (Docker remains externally managed):

```bash
agentcfd run openfoam-pipe openfoam-pipe \
  --fully-developed \
  --cross-section-cells 16 \
  --axial-cells 400 \
  --container-image opencfd/openfoam-run:2606 \
  --json
```

Three-grid studies can use `agentcfd.verification.grid_convergence_index` or
consume three serialized results directly:

```bash
agentcfd prepare openfoam-pipe-grid pipe-grid \
  --cross-section-cells 8 16 32 \
  --base-axial-cells 40 \
  --json

agentcfd run openfoam-pipe-grid pipe-grid \
  --container-image opencfd/openfoam-run:2606 \
  --json

agentcfd verify grid-convergence coarse.json medium.json fine.json \
  --quantity flow.pressure_drop \
  --json
```

The preparation workflow creates a 0.5 m by 0.1 m benchmark with one declared
fully developed inlet model, isotropic refinement ratios, per-case hashes, and
an explicit plan. The grid runner verifies and executes every fresh case and
writes the GCI evidence automatically. The result workflow checks that all runs
completed, converged, share model and analysis identities, use the same quantity
unit, and contain distinct positive dimensionless mesh cell counts before it records
Richardson extrapolation, observed order, GCI, the asymptotic ratio, and hashes
of all three source files.

The first RANS workflow is explicit about both turbulence and evidence:

```python
turbulent = Model(
    name="water-pipe-rans",
    study=studies.internal_flow(
        turbulence="k-omega-sst",
        wall_treatment="blended-wall-functions",
    ),
    domain=geometry.circular_pipe(length=3.0, diameter=0.1),
    fluid=fluids.newtonian(
        "water", density=998.2, dynamic_viscosity=1.002e-3
    ),
).boundaries(
    inlet=boundaries.turbulent_mean_velocity_inlet(
        1.0, intensity=0.05, length_scale=0.007
    ),
    outlet=boundaries.pressure_outlet(),
    wall=boundaries.no_slip_wall(),
)
```

Prepare or execute the same model from the CLI:

```bash
agentcfd prepare openfoam-turbulent-pipe turbulent-pipe --json
agentcfd run openfoam-turbulent-pipe turbulent-pipe \
  --container-image opencfd/openfoam-run:2606 --json
```

Generate or run the experimental fully developed circular-pipe precursor:

```bash
agentcfd prepare openfoam-turbulent-precursor precursor --json
agentcfd run openfoam-turbulent-precursor precursor \
  --container-image opencfd/openfoam-run:2606 --json
```

The precursor supports explicit `--turbulence-model k-omega-sst` and
`--turbulence-model k-epsilon` selections. AgentCFD pairs k-epsilon with
`epsilonWallFunction` and `nutkWallFunction`, recovers `epsilon` rather than
`omega`, and keeps this capability precursor-only until downstream mapping is
independently verified.

Keep the nominal wall-adjacent cell height fixed while changing the O-grid
interior resolution, then assess the resulting wall-function study separately
from formal GCI:

```bash
agentcfd prepare openfoam-turbulent-wall-study wall-study
agentcfd run openfoam-turbulent-wall-study wall-study \
  --container-image opencfd/openfoam-run:2606
```

The evidence-backed defaults are c8/c16/c32, a 0.0625 nominal wall-cell
fraction, and 1000/4000/6000 iterations with a 50-sample stability window.
Existing accepted precursor results can also be assessed directly with
`agentcfd verify turbulent-wall-study`.

Screen the three supported k-omega SST momentum wall functions on one
content-identical mesh, or select one explicitly for the fixed-wall family:

```bash
agentcfd prepare openfoam-turbulent-wall-function-study wall-functions
agentcfd run openfoam-turbulent-wall-function-study wall-functions \
  --container-image opencfd/openfoam-run:2606
agentcfd prepare openfoam-turbulent-wall-study spalding-grid \
  --nut-wall-function nutUSpaldingWallFunction
```

The comparison is fail-closed: it can nominate a benchmark-specific candidate,
but one Reynolds point and one correlation cannot promote a general default.

Compare the evidence-backed SST/Spalding and k-epsilon/nutk model pairs with
all non-model inputs and the native mesh held fixed:

```bash
agentcfd prepare openfoam-turbulent-model-study model-study
agentcfd run openfoam-turbulent-model-study model-study \
  --container-image opencfd/openfoam-run:2606 --json
```

For Reynolds sweeps, ask AgentCFD to derive the near-wall spacing instead of
reusing one physical cell height blindly:

```bash
agentcfd prepare openfoam-turbulent-model-study re-low \
  --velocity 0.5 --target-y-plus 40 --json
agentcfd calculate wall-resolution --density 998.2 --viscosity 0.001002 \
  --velocity 0.5 --diameter 0.1 --target-y-plus 40 --json
agentcfd verify turbulent-model-sweep study-1.json study-2.json study-3.json
```

Or prepare and execute the complete content-addressed campaign:

```bash
agentcfd prepare openfoam-turbulent-model-sweep campaign \
  --velocities 0.5 1 2 5 --target-y-plus 40 --json
agentcfd run openfoam-turbulent-model-sweep campaign \
  --container-image opencfd/openfoam-run:2606 --json
```

The runner writes point-granular progress, verifies each nested plan hash, and
resumes only from complete point assessments whose native result artifacts can
be reopened and reverified.

The prepared plan records the correlation-based wall-spacing prediction and
recommended fraction, but runtime y-plus remains mandatory evidence. Across a
sweep each model pair must share an identical mesh; different Reynolds points
may use different wall-cell fractions to preserve one declared y-plus policy.

The same assessment can be recreated from two accepted results with
`agentcfd verify turbulent-model-study`. The certificate ranks accuracy and
runtime but always keeps general default promotion false at a single Reynolds
number. The current four-point OpenCFD v2606 matrix finds SST/Spalding best at
Re 49,810 and 99,621, then k-epsilon/nutk best at Re 199,242 and 498,104. It
therefore accepts the evidence matrix but rejects a single range-wide default.

A uniform, geometrically similar candidate can be checked separately with
`agentcfd verify turbulent-precursor-grid-study`. It uses the periodic
cross-section size `h/D = 1/N`, verifies that all three wall-y-plus ranges stay
in one wall-model regime, and refuses oscillatory Richardson sequences.

The fraction is relative to the nominal radial edge of the outer O-grid block.
AgentCFD solves the required OpenFOAM end/start grading ratio and records both
the fraction and physical design height. Fixed-wall-cell families test whether
the chosen wall-function regime remains consistent; because their interior
grading changes with resolution, they are not automatically valid Richardson/
GCI families. Pathological cumulative grading is rejected before OpenFOAM runs
when its estimated axial-to-smallest-radial cell ratio already exceeds the
declared mesh-aspect limit.

Map that accepted, content-addressed developed field into a downstream pipe:

```bash
agentcfd run openfoam-turbulent-pipe mapped-pipe \
  --precursor-case precursor \
  --cross-section-cells 8 --axial-cells 120 \
  --container-image opencfd/openfoam-run:2606 --json
```

AgentCFD lowers this to `flowRateInletVelocity`, `kOmegaSST`, explicit `k` and
`omega` inputs, blended wall functions, and in-run `yPlus` recovery. A completed
and converged run remains unaccepted until its inlet/reference applicability and
grid evidence pass; the trust state is intended to be safe for unattended AI
workflows.
The mapping route rejects unaccepted, incompatible, incomplete, or modified
precursors before execution. It records source result, case, mesh, runtime, and
field identities in `agentcfd-precursor-map.json`; `mapFields` initializes the
downstream internal field while target boundary semantics remain explicit.
The rationale for analytical, flow-rate, and periodic developed inlets, measured
resolution/runtime tiers, and the staged turbulence and steam plan is recorded
in [the numerical strategy](docs/numerical-strategy.md).
Common pipe checks are available under `agentcfd.engineering`: hydraulic
diameter, Reynolds number, laminar or iterated Colebrook--White Darcy friction,
straight-run pressure loss, local-loss pressure drop, and one auditable
`pipe_pressure_loss` record combining them. The transitional Reynolds range is
deliberately rejected rather than silently interpolated.

## Architecture

```text
Human / AI agent / script / future GUI
                    |
                    v
Study -> Model -> Domain/Regions -> Fluids -> Boundaries/Sources
                    |
                    v
             Solution Step + Output
                    |
                    v
      Provider lowering and deterministic execution
                    |
                    v
 SimulationResult -> verification -> datasets -> learning
                    |
                    v
      AgentFEM / experiments / NN / PINN / neural operators
```

The core package has no mandatory third-party runtime dependency or LLM and
does not treat successful execution as scientific acceptance. NumPy support is
available through the optional `arrays` extra. AI is a first-class operator of
the workflow; fluid mechanics and deterministic numerical computation remain
authoritative.

## Documentation

- [Concepts](CONCEPTS.md)
- [Workflow](WORKFLOW.md)
- [Changelog](CHANGELOG.md)
- [Architecture](docs/architecture.md)
- [Roadmap](ROADMAP.md)
- [Product and market strategy](docs/product-strategy.md)
- [AgentFEM, CFD, and AI interoperability](docs/interoperability.md)
- [Results, evidence, and AI exchange](docs/results-and-ai.md)
- [Thermophysical properties and IF97](docs/properties.md)
- [Engineering correlations and model screening](docs/engineering-correlations.md)
- [Installation and solver runtime](docs/installation.md)
- [Dependency and license policy](docs/licensing.md)
- [Thermophysical properties](docs/properties.md)
- [OpenFOAM provider boundary](docs/openfoam-provider.md)
- [Numerical strategy and performance tiers](docs/numerical-strategy.md)
- [Publishing and PyPI name status](docs/publishing.md)
- [Validation policy](docs/validation.md)
- [Engineering correlations](docs/engineering-correlations.md)
- [Benchmark catalog](docs/benchmark-catalog.md)
- [OpenFOAM v2606 execution evidence](docs/openfoam-v2606-validation.json)
- [OpenFOAM v2606 grid-validation evidence](docs/openfoam-v2606-grid-validation.json)
- [OpenFOAM v2606 turbulent-pipe diagnostic evidence](docs/openfoam-v2606-turbulent-pipe-diagnostic.json)
- [OpenFOAM v2606 periodic precursor evidence](docs/openfoam-v2606-periodic-precursor-validation.json)
- [OpenFOAM v2606 precursor-mapping evidence](docs/openfoam-v2606-precursor-mapping-validation.json)
- [OpenFOAM v2606 fixed-wall-cell three-grid evidence](docs/openfoam-v2606-fixed-wall-cell-study.json)
- [OpenFOAM v2606 turbulent GCI-candidate evidence](docs/openfoam-v2606-turbulent-gci-candidate.json)
- [OpenFOAM v2606 wall-function sensitivity evidence](docs/openfoam-v2606-wall-function-study.json)
- [OpenFOAM v2606 Spalding fixed-wall evidence](docs/openfoam-v2606-spalding-fixed-wall-study.json)
- [OpenFOAM v2606 turbulence-model sensitivity evidence](docs/openfoam-v2606-turbulent-model-study.json)
- [OpenFOAM v2606 multi-Re turbulence-model matrix](docs/openfoam-v2606-turbulent-model-sweep.json)
- [Guide for AI agents](AGENT_GUIDE.md)

## License

AgentCFD is licensed under Apache-2.0. Optional numerical engines retain their
own licenses and are connected through explicit provider boundaries.
