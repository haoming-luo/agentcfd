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
> solution are executable. Deterministic OpenFOAM circular-pipe case generation
> is experimental; OpenFOAM field recovery and accepted numerical results are
> not yet released capabilities.

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

## Install from this repository

```bash
git clone https://github.com/haoming-luo/agentcfd.git
cd agentcfd
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
agentcfd doctor
agentcfd demo pipe
```

PyPI and conda-forge publication will follow an installed-artifact release gate.

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
`blockMesh` and `simpleFoam`. Even when both commands finish, AgentCFD refuses
to call the result scientifically accepted until mesh-field mass balance and
pressure-loss recovery are implemented.

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

The core package has no mandatory LLM and does not treat successful execution
as scientific acceptance. AI is a first-class operator of the workflow; fluid
mechanics and deterministic numerical computation remain authoritative.

## Documentation

- [Concepts](CONCEPTS.md)
- [Workflow](WORKFLOW.md)
- [Changelog](CHANGELOG.md)
- [Architecture](docs/architecture.md)
- [Roadmap](ROADMAP.md)
- [Product and market strategy](docs/product-strategy.md)
- [AgentFEM, CFD, and AI interoperability](docs/interoperability.md)
- [Dependency and license policy](docs/licensing.md)
- [OpenFOAM provider boundary](docs/openfoam-provider.md)
- [Publishing and PyPI name status](docs/publishing.md)
- [Validation policy](docs/validation.md)
- [Guide for AI agents](AGENT_GUIDE.md)

## License

AgentCFD is licensed under Apache-2.0. Optional numerical engines retain their
own licenses and are connected through explicit provider boundaries.
