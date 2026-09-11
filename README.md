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
> solution are executable. Deterministic OpenFOAM laminar, constant-property
> heated laminar, k-omega SST, and
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

The current product and engineering sequence is maintained in the
[CTO roadmap](docs/cto-roadmap.md): executable separated internal flow first,
then practical imported geometry, dependable turbulent equipment flow, heat
and steam, and only later combustion and multiphase breadth.
The [product experience roadmap](docs/product-experience-roadmap.md) tracks the
parallel goal of reducing user attention, failure recovery work, and storage
amplification per trusted result.
[Thermal and steam architecture](docs/thermal-and-steam-roadmap.md) defines the
solver-neutral heat-flow intent, the bounded passive-temperature OpenFOAM slice,
and the evidence gates required before advancing to real-fluid steam.
[Post-processing recipes](docs/postprocessing-recipes.md) turn named slices,
contours, streamlines, and multi-panel engineering overviews into portable
ParaView scripts without copying the XDMF/HDF5 field payload. Optional typed
camera and render intent can reproduce a screenshot, PNG animation sequence,
or MP4; nothing is rendered unless the project explicitly asks for it.

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
python -m pip install agentcfd==0.1.0a4
agentcfd doctor
agentcfd demo pipe
```

## One project lifecycle

The current development version exposes the same readable project to people,
agents, CI, and future GUIs:

```bash
agentcfd templates                       # discover scope before creating files
agentcfd templates imported-internal-flow
agentcfd init --template industrial-pipe my-flow
cd my-flow
agentcfd doctor .       # project/runtime/resource audit; no solve or field read
agentcfd status .       # one state, one recommended next action
agentcfd project .      # unified project/result/output view; no HDF5 read
agentcfd params . --output operating-point.json  # freeze validated inputs
agentcfd result .       # quantities and field metadata without opening HDF5
agentcfd verify project . # hash result artifacts and verify XDMF/H5 consistency
agentcfd archive . --plan-only             # preview exact compact handoff bytes
agentcfd archive . result-decision.zip     # evidence without field payload
agentcfd archive . result-portable.zip --profile portable # include XDMF/H5
agentcfd verify archive result-decision.zip
agentcfd run .          # full portable fields for spatial review
agentcfd run . --summary-only  # compact evidence, no permanent field bundle
agentcfd watch .        # follow a long active run, then stop automatically
agentcfd performance .  # bounded comparable-run timing and ETA calibration
agentcfd diagnose .     # classify bounded evidence and recommend one safe action
agentcfd logs .         # raw bounded tail when deeper evidence is needed
agentcfd resume .       # continue an identical interrupted transient checkpoint
agentcfd view .         # prints the latest XDMF or result target
agentcfd view . --recipe centerline-pressure --batch  # headless CSV/state
agentcfd view . --layout wake-overview --batch  # one shared-data dashboard
agentcfd campaigns . --export-csv design-points.csv   # compact comparison
agentcfd campaigns . --plot-svg loss-map.svg \
  --x-parameter mean_velocity \
  --y-quantity report.system-loss.loss_coefficient    # accepted operating map
agentcfd verify component-loss equipment/result.json straight-run/result.json \
  --confirm-equivalent-baseline --output local-loss.json
```

`agentcfd templates --json` is the machine-readable source of truth for each
starting point's provider, geometry mode, physics, default outputs, required
inputs, limitations, and executable creation command. The `init` parser and
provider defaults consume the same catalog, so a new frontend or AI agent does
not need its own hard-coded template list. Each entry also links the installed
`project-creation-request` schema and declares template-specific required,
optional, and exactly-one request fields; CI checks that the catalog and static
schema enumerate the same templates and providers.

For the first bounded heat-transfer workflow:

```bash
agentcfd init --template heated-pipe my-heated-pipe
cd my-heated-pipe
agentcfd plan .         # Re, Pr, Pe and first-law outlet estimate
agentcfd run .          # pressure, velocity, temperature and conservation gates
agentcfd view .         # standard XDMF/H5 contains T beside U and p
```

This template is steady, laminar, incompressible and constant-property, with a
prescribed non-zero wall heat flux. It does not claim buoyancy, conjugate heat
transfer, phase change, or steam support.

Agents and future forms can create the same readable thermal project without
editing Python text. A strict request may set any subset of the five documented
factory defaults; AgentCFD validates the values first and then writes them into
`case.py`, which remains the sole project source of truth:

```bash
agentcfd init my-heated-pipe --request heated-pipe-request.json
```

See `examples/heated_pipe_project/project-request.json` for the versioned,
unit-described request pattern.

The default `result` view groups engineering values by purpose instead of
printing one undifferentiated list. Add `--json` when an agent or integration
needs the unchanged flat canonical names and versioned machine contract.

`status` also discovers the editable `build()` parameters and their current
defaults directly from `case.py`. Human output shows the first controls and the
single `--param NAME=JSON` pattern; `status --json` and `plan --json` expose the
complete parameter contract for agents and future forms. Unknown names still
fail before any provider work.

Projects can keep UI/agent meaning beside the Python model with the optional
`parameters.describe(...)` decorator. Generated templates use
`parameters.number(...)` and `parameters.choice(...)` to declare labels,
canonical units, bounds, nullability, choices, and short descriptions. This is
not a second input file: AgentCFD uses the declared scalar bounds and choices
for immediate parameter preflight, while the engineering constructors remain
the final source of coupled scientific validation.

Store a reusable operating point without duplicating the model:

```json
{
  "schema": "agentcfd.parameter-set/0.1",
  "parameters": {"diameter": 0.08, "mean_velocity": 0.015}
}
```

`agentcfd check . --param-file operating-point.json`, `plan`, `mesh`, and `run`
consume the same file. A repeated `--param mean_velocity=0.02` deliberately
overrides only that value for a one-off trial. The strict installed schema
rejects duplicate keys, nested values, unknown envelope fields, and non-finite
numbers before the project factory or provider runs.

`agentcfd params .` lists the complete resolved operating point with engineering
units. Add `--param` and `--output operating-point.json` to freeze a validated
snapshot without hand-writing JSON; an existing destination is never replaced.

For one disposable screening point, add `--summary-only` consistently to
`check`, `plan`, and `run`. OpenFOAM still solves and publishes quantities,
histories, checks, logs, and provenance, but skips permanent XDMF/H5 fields.
Use a normal run when spatial review or AgentFEM field exchange is required.

For owned STL/OBJ internal-flow geometry, initialization can perform the
inspection-to-project handoff without asking the user to author `case.py` from
scratch. Every ambiguous physical input stays explicit:

```bash
agentcfd init my-duct --template imported-internal-flow \
  --geometry fluid.stl --unit mm --roles boundary-roles.json \
  --interior-point-m 0.15 0.03 0.03 \
  --inlet-velocity-m-s 1 0 0 --base-size-m 0.005 \
  --maximum-cells 500000
cd my-duct
agentcfd status .
```

For constant-density laminar equipment specified by throughput, replace the
velocity vector with one positive SI mass flow:

```bash
agentcfd init my-duct --template imported-internal-flow \
  --geometry fluid.stl --unit mm --roles boundary-roles.json \
  --interior-point-m 0.15 0.03 0.03 \
  --inlet-mass-flow-kg-s 0.25 --base-size-m 0.005 \
  --maximum-cells 500000
```

Pressure-driven equipment is also a first-class laminar setup. This declares
inlet total gauge pressure against the generated zero-gauge static outlet:

```bash
agentcfd init my-duct --template imported-internal-flow \
  --geometry fluid.stl --unit mm --roles boundary-roles.json \
  --interior-point-m 0.15 0.03 0.03 \
  --inlet-total-gauge-pressure-pa 0.001 --base-size-m 0.005 \
  --maximum-cells 500000
```

Velocity, mass flow, and total pressure are mutually exclusive creation
controls. The generated `case.py` keeps the selected control as a readable
default and can still be parameterized for screening or campaigns.

When every surface carries an unambiguous name such as `inlet`, `outlet`, and
`walls`, replace `--roles boundary-roles.json` with
`--accept-name-roles`. That flag is the explicit confirmation gesture; it
fails before writing the project if even one region name is ambiguous.

The new project owns a copy of the surface plus its content hash, normalized
role map, and portable inspection record. The generated Python remains the
editable source for inlet control, fluid properties, mesh size, cell budget,
and outputs; OpenFOAM files remain disposable implementation detail.

Agents and future GUIs can send the same inputs as a strict versioned creation
request instead of constructing a long shell command:

```bash
agentcfd init my-duct --request project-request.json
```

Relative geometry paths resolve beside that JSON file. The request is an
auditable creation boundary, not a second project language: after creation,
only the generated `case.py` controls the scientific model. A complete example
lives at `examples/imported_duct_mesh/project-request.json`. Automation may
provide an exact `boundary_roles` object or explicitly set
`"role_confirmation": "accept-name-suggestions"`; it cannot request a silent
fallback wall.

Imported projects use the same compact decision-output API as parametric
channels. A single-outlet generated `case.py` includes
`outputs.pressure_loss(...)` and `outputs.flow_uniformity(...)` reports by
default. The first publishes
mass-flow-averaged inlet/outlet total pressure,
total-pressure loss, inlet-bulk reference velocity and dynamic pressure, and a
dimensionless system-loss coefficient. The inlet area is recovered from the
post-mesh OpenFOAM patch unless `reference_area` is explicit. Add
`outputs.probe(...)`, a scalar-pressure `outputs.surface_report(...)`, or
`outputs.force_report(...)` for further engineering decisions. These reports
are compact histories and do not increase XDMF/H5 frame count or retain the
derived total-pressure field. The second publishes outlet velocity-vector
uniformity, signed mean normal velocity, and patch area under stable
`report.outlet-quality.*` names, also without adding a field frame.

```python
outputs.pressure_loss("valve-loss", inlet="inlet", outlet="outlet")
outputs.flow_uniformity("outlet-quality", region="outlet")
```

For two or more outlets, project generation instead adds one
`outputs.flow_distribution(...)` report covering every branch. It publishes
positive role-directed volume/mass flow, branch fractions, aggregate balance,
and coefficient of variation without retaining field frames. Optional targets
must name every outlet and sum to one; an explicit `outputs.require(...)` can
then turn maximum fraction error into a machine-checkable design gate.
Per-outlet loss and uniformity reports remain opt-in, avoiding O(N) histories
when the current decision only needs the split.

```python
outputs.flow_distribution(
    "flow-split",
    inlet="inlet",
    outlets=("branch_a", "branch_b"),
    targets={"branch_a": 0.5, "branch_b": 0.5},
)
```

The same intent can be materialized at project creation, so an agent or GUI
does not need to patch Python text:

```bash
agentcfd init manifold --template imported-internal-flow \
  ...geometry-and-inlet-options... \
  --outlet-target branch_a=0.5 --outlet-target branch_b=0.5 \
  --maximum-fraction-error 0.001
```

The strict JSON creation request accepts the equivalent
`flow_distribution.targets` and optional `maximum_fraction_error`. Both paths
write ordinary, inspectable `outputs.flow_distribution()` and
`outputs.require()` calls into `case.py`; the creation request does not become
a second project language.

For pressure-loss reports, the final coefficient is available as
`report.valve-loss.loss_coefficient`; the corresponding dimensional value is
`report.valve-loss.total_pressure_loss`. This is the complete loss between the
declared measurement surfaces. Subtracting a separate straight-run friction
baseline to claim a fitting-only minor-loss coefficient remains an explicit
engineering decision.

Declare design gates beside those reports so batch runs and AI agents reach the
same decision without hidden thresholds:

```python
outputs.require(
    "outlet-uniform-enough",
    quantity="report.outlet-quality.velocity_uniformity_index",
    unit="1",
    minimum=0.95,
)
```

Missing quantities, unit mismatches, and bound violations fail acceptance. A
design miss remains distinct from numerical trust, so a verified result is not
mislabelled as untrustworthy merely because it exceeds an engineering budget.

The same generated project can move from laminar screening to explicit
k-omega SST without editing provider files. Supply all three RANS assumptions
as project parameters; omitting any one fails before meshing:

```bash
agentcfd run my-duct \
  --param turbulence_model='"k-omega-sst"' \
  --param turbulence_intensity=0.05 \
  --param turbulence_length_scale=0.01
```

AgentCFD publishes `k`, `omega`, `nut`, and compact wall y-plus histories. The
current arbitrary-geometry RANS slice requires all wall y-plus values to stay
between 30 and 300, but remains for workflow development and engineering
review; it does not claim validated near-wall accuracy before prism-layer and
grid evidence exist.

`case.py` is the modeling source of truth. `agentcfd.toml` contains only
operational settings such as the default provider, output directory, container,
and mesh controls. An ordinary execution replaces the managed `output/`
directory, so editing parameters and rerunning keeps one obvious current
answer. Preserve an immutable run only when that is the intent:

```bash
agentcfd run . --campaign
agentcfd campaigns .             # read-only design-point index; no H5 access
agentcfd run . --campaign --param mean_velocity=0.03
agentcfd sweep . sweep.json       # preflight all, execute/reuse design points
agentcfd sweep . sweep.json --plan-only  # zero-solve cost/reuse preview
agentcfd sweep . sweep.json --max-runs 4 # hard pre-execution compute limit
agentcfd sweep . sweep.json --summary-only # metrics/evidence, no permanent H5
agentcfd promote . <run-id>      # publish full fields for one screened point
agentcfd compact . <run-id>      # preview full-field bulk removal; add --apply
agentcfd geometry-check fluid.stl --unit mm --roles roles.json \
  --internal-flow --output geometry/inspection.json
agentcfd mesh . --plan-only     # imported-surface cell/refinement/quality budget
agentcfd mesh . --output mesh-case # native dry-run + snappy + checkMesh gates
agentcfd run .                      # bounded imported laminar flow + XDMF/H5
agentcfd run . --keep-workspace  # expert backend debugging
agentcfd export sample . pressure-loss.json \
  --input mean_velocity --output-quantity flow.pressure_drop
agentcfd export dataset . training/pressure-map \
  --input mean_velocity --output-quantity flow.pressure_drop
agentcfd verify dataset training/pressure-map
agentcfd dataset inspect training/pressure-map --preview 3
agentcfd dataset plan training/pressure-map --seed 17 \
  --validation-fraction 0.2 --output training/plan.json
agentcfd storage .               # output/campaign/workspace inventory
agentcfd clean .                 # safe preview; add --apply to reclaim workspace
# agentcfd clean . --include-retained --apply  # release expert copies
```

Python and learning tools can open the same verified dataset without importing
OpenFOAM, AgentFEM, pandas, or NumPy:

```python
from agentcfd import open_scientific_dataset

dataset = open_scientific_dataset("training/pressure-map")
X, Y = dataset.matrices()        # immutable tuples, values remain in declared units
plan = dataset.training_plan(seed=17)  # content-bound split + explicit z-score stats
# X_np, Y_np = dataset.to_numpy()  # optional: pip install agentcfd[arrays]
```

All project commands discover the nearest `agentcfd.toml` while walking upward,
so the same `agentcfd status .` and `agentcfd view .` commands work from
`input/`, `output/fields/`, or any other project subdirectory.

The ordinary project surface stays small:

```text
case.py             readable scientific model and outputs
agentcfd.toml       operational provider/runtime settings
input/              optional user-owned geometry and data
output/             current summary, full result, fields, and compact evidence
campaigns/<run-id>/ explicitly preserved runs only
.agentcfd/          hidden disposable solver workspace
```

Generated OpenFOAM dictionaries, native time directories, and temporary VTK
files live below `.agentcfd/` and are removed after successful publication by
default. They can be regenerated from `case.py`; selected logs and content
manifests are copied to `output/evidence/` first. Validation is attached
evidence; it does not replace the engineering workflow.

Every published `output/` is self-explaining: its `README.md` points humans to
the visualization and evidence, while `status --json` and versioned JSON
schemas give agents the same state, next action, and repair path. During a long
run, status distinguishes solver and field-export phases and reads only a
bounded log tail plus compact monitor rows to report physical time/iteration,
residuals, Courant number, mass imbalance, pressure drop, elapsed time, and a
wide transient ETA range. Add `--storage` when recursive workspace size is
worth the extra I/O. A dead process becomes `interrupted`, and the next replace
run can recover without manual folder surgery.

Completed runs publish `summary.json` beside `result.json`. The first is the
small default decision surface for people, CLIs, GUIs, and AI agents; the
second retains complete scalar histories and the evidence index. Summary reads
do not open HDF5 and report their own I/O cost. The recorded result SHA-256
preserves traceability, while `agentcfd verify project .` remains the deliberate
full-integrity boundary before coupling, training, or archive handoff.

For Python, GUI, and agent integration, `agentcfd project . --json` combines
that state with the compact result, published file roles, standard XDMF/H5
entry points, hidden provider-workspace status, and an optional storage scan.
Its `next_action.operation` is a bounded machine verb, so consumers do not need
to infer intent by parsing a display command. The equivalent dependency-free
Python surface is `agentcfd.open_project(path).snapshot()`. A snapshot is
strictly observational: it never opens HDF5, launches ParaView, or starts a
solver. Runtime scheduling evidence is available separately through
`agentcfd.open_project(path).performance()` so it cannot be confused with the
scientific result.

When integrity—not just a lightweight overview—is required, run `agentcfd
verify project . --json`. This explicit operation hashes every registered
result artifact, verifies the saved solution-plan identity, compares the run
and scientific-result identities, and opens the XDMF/H5 bundle when one exists.
The reproducible compatibility rule recognizes 0.1.0a3 OpenFOAM identities
without weakening verification. Integrity and scientific acceptance remain
separate fields, so an intact but unaccepted result cannot be mistaken for an
engineering decision.

`agentcfd watch .` polls this same lightweight contract every two seconds and
stops by itself at `complete`, `review`, `failed`, or `interrupted`. Use
`watch --json` for one complete JSON object per line; add `--storage` only when
live disk growth matters enough to justify a recursive scan on every poll.

Failed runs retain their generated solver workspace automatically, even when
ordinary successful runs would clean it. `agentcfd status .` then recommends
`agentcfd diagnose .`. The deterministic classifier recognizes common
resource, configuration, mesh, numerical, and runtime signatures and always
attaches the exact evidence line, confidence, conservative repair, and a
machine-readable statement that it did not modify the model automatically.
`agentcfd logs .` returns a bounded raw tail from the newest live log or the
small published evidence copy. Select a phase with `--command checkMesh` or
`--command pimpleFoam`; use `--json` for either versioned contract. In a
campaign, append `--run-id <id>` to `logs` or `diagnose` so a later successful
point cannot hide the failure you are investigating.

Transient templates declare sparse rolling checkpoints independently from
visualization frames. After a failed or interrupted run, `status` and
`diagnose` report whether an identity-matched checkpoint is resumable and from
which physical time. `agentcfd resume .` refuses changed project or runtime
inputs, validates generated-case and archive-member hashes, skips repeated
initialization, and records the source run in the new result. The sole native
checkpoint copy is protected from `clean`; it becomes reclaimable only after a
published checkpoint exists or a resumed run succeeds.
Operational limits such as `timeout_seconds` may be relaxed before resume;
solver-affecting model, mesh, and runtime identity must remain unchanged.

An expert workspace retained by `--keep-workspace` or project policy is also
protected from ordinary cleanup. Releasing that deliberate copy requires the
separate, previewable `agentcfd clean . --include-retained` scope; add `--apply`
only after reviewing the exact candidate paths and bytes.

`agentcfd doctor .` is the deliberate heavier preflight: it combines readiness,
latest-run health, recovery, recursive managed storage, output-budget and free
disk checks. Its resource estimate reports cells, nominal solver steps,
pressure-velocity correctors, and a cell-update proxy for comparing alternatives.
It reports energy as `not-measured` until an executor supplies power or joule
telemetry; mesh size alone is not presented as an energy measurement.

Output is declared by purpose instead of by OpenFOAM directory frequency. For
example, a transient run can keep frequent scalar histories, one visualization
frame every `0.05 s`, and only two rolling restart checkpoints:

```python
output = outputs.animation(
    every=0.05,
    maximum_frames=240,
    restart=outputs.checkpoints(every=1.0, keep=2),
    storage_budget="512 MiB",
)
```

`agentcfd plan` resolves the frame count and estimates final plus temporary
storage before solving. XDMF/H5 numeric datasets are chunked and compressed by
default; NPZ remains explicit opt-in.

The backend-neutral workflow API also includes named regions, short rectangular
channels with wall-attached baffles, pressure and mass-flow boundary variants,
uniform/potential/previous-result initialization, mesh intent, compact probes,
surface reductions, force reports, and final-frame line profiles that publish
only distance plus one requested scalar or explicit vector component/magnitude
to CSV. See the
[common workflow API](docs/common-workflow-api.md) and the readable
[bottom-baffle project](examples/channel_baffle_project/case.py). New intent is
checked against provider capabilities before execution and is never silently
ignored.

```bash
agentcfd init wake-study --template baffle-channel
agentcfd plan wake-study --json
agentcfd run wake-study
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
The core remains dependency-free. To install the neutral AgentCAE catalog and
verify that AgentCFD's emitted record identities match it exactly:

```bash
python -m pip install "agentcfd[interop]"
agentcfd contracts --check-agentcae --json
```

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

## Portable fields: XDMF/H5 by default, NPZ on demand

Install the permissively licensed optional I/O stack, then export every saved
OpenFOAM field frame into one versioned bundle:

```bash
python -m pip install "agentcfd[io]"
agentcfd export openfoam OPENFOAM_CASE fields \
  --container-image opencfd/openfoam-run:2606 \
  --profile visualization \
  --field fluid.velocity --field fluid.pressure \
  --compression gzip --storage-budget "2 GiB" --json
agentcfd verify field-bundle fields --json
agentcfd export openfoam OPENFOAM_CASE fields-with-arrays --with-npz
agentcfd export field-sample fields-with-arrays velocity-final.npz \
  --field fluid.velocity --association point --frame -1
```

`fields.xdmf` plus `fields.h5` is the default mesh-and-field route for
ParaView, AgentFEM exchange, and other scientific tools. `fields.npz` mirrors
the same geometry, topology, axis, point fields, and native cell fields without
pickles for NumPy, PyTorch, JAX, and dataset pipelines only when `--with-npz`
is selected. `manifest.json` retains
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
output follows `OutputRequest.portable_profile`, `portable_formats`, and
canonical `fields`. Association and file format are independent choices.

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
- [Output architecture and storage budgets](docs/output-architecture.md)
- [Numerical strategy and performance tiers](docs/numerical-strategy.md)
- [Publishing and PyPI name status](docs/publishing.md)
- [Imported geometry preflight](docs/imported-geometry.md)
- [Runnable imported duct mesh](examples/imported_duct_mesh/README.md)
- [Validation policy](docs/validation.md)
- [Engineering correlations](docs/engineering-correlations.md)
- [Benchmark catalog](docs/benchmark-catalog.md)
- [OpenFOAM v2606 execution evidence](docs/openfoam-v2606-validation.json)
- [OpenFOAM v2606 heated-pipe evidence](docs/openfoam-v2606-heated-pipe-validation.json)
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
- [OpenFOAM v2606 imported laminar duct-flow evidence](docs/openfoam-v2606-imported-duct-flow.json)
- [OpenFOAM v2606 imported mass-flow duct evidence](docs/openfoam-v2606-imported-duct-mass-flow.json)
- [OpenFOAM v2606 imported k-omega SST duct-flow evidence](docs/openfoam-v2606-imported-duct-rans.json)
- [Guide for AI agents](AGENT_GUIDE.md)

## License

AgentCFD is licensed under Apache-2.0. Optional numerical engines retain their
own licenses and are connected through explicit provider boundaries.
