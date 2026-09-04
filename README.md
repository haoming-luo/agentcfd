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
> solution are executable. Deterministic OpenFOAM laminar and k-omega SST
> smooth-pipe generation, execution, mesh checks, result recovery, and
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

AgentCFD lowers this to `flowRateInletVelocity`, `kOmegaSST`, explicit `k` and
`omega` inputs, blended wall functions, and in-run `yPlus` recovery. A completed
and converged run remains unaccepted until its inlet/reference applicability and
grid evidence pass; the trust state is intended to be safe for unattended AI
workflows.
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
- [Guide for AI agents](AGENT_GUIDE.md)

## License

AgentCFD is licensed under Apache-2.0. Optional numerical engines retain their
own licenses and are connected through explicit provider boundaries.
