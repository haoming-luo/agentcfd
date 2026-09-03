# Installation and solver runtime

AgentCFD has two deliberately separate runtime layers.

## Python control plane

Python 3.11 or newer and NumPy are sufficient for the core API, reference
provider, case generation, result contracts, and command-line tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
agentcfd doctor
python -m pytest -q
```

Release candidates must also be installed from the built wheel into a clean
environment. Editable-source success alone is not a release gate.

## OpenFOAM numerical plane

OpenFOAM is an external solver runtime, not a Python dependency. AgentCFD first
looks for `blockMesh` and `simpleFoam` on `PATH`. Keeping this as a filesystem
and subprocess boundary makes generated cases inspectable and preserves the
license identity of both projects.

On macOS, a Linux container or remote Linux worker is generally the most
predictable route for production OpenFOAM. The current pre-alpha provider can
generate a case without OpenFOAM, but execution requires those commands and is
not scientifically accepted until field recovery and conservation checks ship.

Use `agentcfd doctor --json` to inspect the exact local capability state.
