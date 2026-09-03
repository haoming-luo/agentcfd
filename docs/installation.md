# Installation and solver runtime

AgentCFD has two deliberately separate runtime layers.

## Python control plane

Python 3.11 or newer is sufficient for the dependency-free core API, reference
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

NumPy array interoperability and CoolProp thermophysical properties are
separate permissively licensed extras:

```bash
python -m pip install "agentcfd[arrays]"
python -m pip install "agentcfd[properties]"
```

## OpenFOAM numerical plane

OpenFOAM is an external solver runtime, not a Python dependency. AgentCFD first
looks for `blockMesh`, `checkMesh`, and `simpleFoam` on `PATH`. Keeping this as a filesystem
and subprocess boundary makes generated cases inspectable and preserves the
license identity of both projects.

On macOS, a Linux container or remote Linux worker is generally the most
predictable route for production OpenFOAM. The current pre-alpha provider can
generate a case without OpenFOAM. Execution recovers conservation, pressure,
mesh, convergence, and final-field evidence; scientific acceptance is decided
from those checks rather than the process exit code.

Docker can be selected explicitly without wrapper scripts:

```bash
agentcfd run openfoam-pipe openfoam-pipe \
  --fully-developed \
  --container-image opencfd/openfoam-run:2606
```

The provider mounts only the selected case directory at `/case`, passes every
argument without a shell, and records both the image identity and the actual
OpenFOAM version reported by the runtime.

Use `agentcfd doctor --json` to inspect the exact local capability state.
