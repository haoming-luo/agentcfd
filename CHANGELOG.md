# Changelog

## Unreleased

- End human project runs with a bounded four-quantity engineering summary and
  one state-appropriate next command, while leaving machine JSON unchanged.
- Stream changed, bounded project status in the same terminal during human
  `agentcfd run .` calls. The simulation stays on the main thread for normal
  Ctrl-C behavior, while `--json` and the new `--no-progress` mode remain quiet.
- Report bounded portable-field progress through `status` and `watch`: phase,
  completed/total frames, batch position, and fraction are written atomically
  once per micro-batch without opening field payloads. Human watch lines and the
  versioned status schema expose the same contract; solver ETA is deliberately
  suppressed during export because it cannot predict conversion throughput.
- Publish the terminal `run.json` through the same atomic replacement contract
  as live progress, so concurrent CLIs, GUIs, and agents cannot observe a
  partially written record during the result handoff.
- Show bounded phase/frame/batch milestones for interactive direct OpenFOAM
  exports on stderr, while keeping `--json` output silent and machine-stable.
- Expose pre-conversion `--time-interval`, `--latest-only`, and
  `--exclude-initial` filters on direct OpenFOAM field export, preventing
  unrequested native times from entering temporary VTU or portable HDF5 output.
- Add a direct-export `--maximum-frames` guard that fails before portable
  writing instead of silently subsampling or allowing an unbounded selection.
- Record exact selected native times and the complete include/interval/latest/
  ceiling policy in every managed field-bundle manifest, including
  preconverted-VTK exports.
- Make the human status parameter hint target the resolved selected project,
  including shell-safe paths with spaces, instead of incorrectly assuming the
  caller is already inside that project directory.
- Replace dense generated-project README prose with a scannable four-part guide
  for first run, typed operating points, bounded storage/history, and recovery;
  `case.py` remains the single scientific source and OpenFOAM stays hidden.
- Promote field-export observation to the public `progress_callback=` Python
  API and ship its standalone versioned JSON Schema for GUIs and agents; the
  former private keyword remains a compatibility alias only.
- Convert selected OpenFOAM times as a bounded four-frame micro-batch pipeline:
  one isolated `foamToVTK -time` invocation, direct HDF5 writes, then immediate
  batch release. The total timeout covers the complete pipeline, per-batch logs
  are accumulated and preserved as project evidence, storage planning has a
  fixed VTU ceiling independent of animation length, and manifests record the
  measured strategy. The bundle is
  built in a same-parent hidden directory and atomically renamed into place, so
  a failed conversion cannot expose a partial public XDMF/H5 destination.
- Publish transient baffled-channel restart state while `pimpleFoam` is still
  running. Stable checkpoint directories are detected without reading them into
  the status path, streamed into a bounded-memory ZIP, and atomically replace
  the previous good archive in project `evidence/`; a torn write therefore
  cannot destroy the last recovery point. `status` and `watch` expose retained
  times, bytes, integrity strategy, and a constant-size publication count/first/
  latest summary by reading only the bounded `restart.json` member.
- Write gzip/lzf HDF5 datasets directly as each XDMF frame is published. The
  exporter no longer creates an uncompressed HDF5 payload and a simultaneous
  `.repack` copy, reducing remaining conversion peak storage without changing
  the public output API. The field manifest records the single-pass strategy
  and zero temporary-HDF5-copy bytes; compressed and explicitly uncompressed
  paths are covered independently.
- Isolate every managed `foamToVTK` conversion with the upstream v2606 `-name`
  option instead of sharing a case's `VTK/` directory. Old conversions can no
  longer contaminate selected times, user-owned VTK output is preserved, and
  each generated VTU is removed immediately after its HDF5 frame is written.
  The field manifest records reclaimed source bytes and retention policy; all
  staging is cleaned on success or failure while preconverted export remains
  non-destructive.
- Add opt-in `sweep --max-parallel N` with CPU capping and aggregate storage
  admission covering concurrent temporary peaks plus final field growth. Serial
  remains the default; reports keep
  deterministic request order while independent identities finish concurrently,
  record one attempt and zero automatic retries, and reject ambiguous parallel
  `--fail-fast`. Run allocation, performance history, and imported-mesh cache
  publication are synchronized so wall-time savings do not corrupt evidence or
  duplicate shared meshing work.
- Add the one-step `industrial-elbow` project template and preview-first
  `geometry-sync`. A project now owns editable generated-geometry intent,
  deterministic STL bytes, independent inspection, roles, and exact generation
  provenance. Changing the spec blocks solver readiness until an explicit
  validated refresh updates the asset, inspection hash, and dynamic interior
  point together; project status and the AI action catalog expose that repair.
- Align imported-mesh acceptance with OpenFOAM's declared quality-policy path.
  AgentCFD now records solver-neutral maximum concavity, writes an inspectable
  `meshQualityDict`, executes `checkMesh -allTopology -meshQuality`, parses the
  total and concavity-specific violation counts, and requires zero violations.
  A real OpenCFD v2606 generated-elbow run now passes with 23,086 cells and a
  checked-in compact evidence record; extra `-allGeometry` diagnostics are no
  longer confused with violations of the configured finite-volume policy.
- Make continuous integration proportional to the evidence needed. Ordinary
  code pushes now run one cached Linux gate containing lint, the full lightweight
  suite, one build, and one installed-wheel smoke; documentation-only changes
  skip code CI and superseded runs are cancelled. A manual five-combination
  workflow serves deliberate stage acceptance. Releases alone test the exact
  built wheel over the complete 3 OS × 3 Python matrix before PyPI publication.
  Jobs have time bounds, transient artifacts expire after three days, and the
  mandatory repository policy forbids quota-error retries or relabeling local
  evidence as remote success. The 2026-09-11 incident baseline and hard
  per-increment execution budget are now part of the coding-agent contract:
  hosted CI verifies one locally complete batch, never acts as an interactive
  debugger, and public-runner pricing does not relax the resource discipline.
- Treat the semantic `checkMesh` verdict as authoritative for imported mesh
  acceptance. OpenFOAM may return process code zero after reporting failed
  all-geometry/all-topology checks; AgentCFD now marks the command check failed
  instead of publishing contradictory all-passed evidence with `accepted=false`.
- Add preview-first `geometry-create elbow` and the public
  `plan_circular_elbow_stl()` / `write_circular_elbow_stl()` APIs. The
  dependency-free generator creates a deterministic watertight 90-degree
  circular-elbow fluid volume with named inlet, outlet, and wall regions;
  refuses overwrite; and publishes exact bytes/hash, tessellation error,
  interior point, reusable observation planes, roles, inlet direction, and a
  mesh/project starting point through an installed versioned contract. The
  generated result passes the existing imported-project compatibility path.
- Add `Step.observation_catalog()` and embed its versioned, installed-schema
  contract in every project output plan. Agents and GUIs can now join compact
  reports to physical surfaces, internal sections, or inline probe points;
  inspect roles, cadence, and reuse; and distinguish scalar-history retention
  from full-field frames without opening XDMF/H5 or parsing `case.py`.
  `Project.observations()` and `agentcfd observations` expose the same bounded
  read path directly, and the state-aware action catalog advertises it.
- Add reusable solver-neutral `regions.plane()` measurement sections. A model
  declares upstream/downstream planes once in SI coordinates, then pressure-
  loss, flow-uniformity, and scalar surface reports reuse their names. Imported
  and baffled-channel OpenFOAM providers lower sections to sampled cutting
  planes, use velocity-normal weighting where face-only `phi` is unavailable,
  retain only compact histories, reject empty-bound candidates by origin bounds,
  and keep port balance and wall forces restricted to physical patches. The
  generated OpenCFD v2606 function objects pass a real solver dry-run.
- Accept repeated `--role REGION=ROLE` confirmations in `geometry-check` and
  imported project creation. This removes a needless hand-written intermediate
  file for small models while preserving mutual exclusion, duplicate
  detection, exact-region validation, and the same project-owned versioned
  role map used by JSON automation.
- Quantify edge-connected surface components during bounded STL/OBJ preflight.
  Each component exposes size, area fraction, region membership, and signed
  volume in native and SI units. Multiple shells now block imported setup until
  `--accept-multiple-components` records that detached debris versus intentional
  internal shells/baffles was reviewed; the count and acceptance become model
  identity. The topology memory guard yields explicit nulls instead of partial
  component evidence.
- Add preview-first `geometry-normalize` for named ASCII STL and OBJ imports.
  Unsafe region tokens become deterministic OpenFOAM-safe words, collisions
  receive stable content-derived suffixes, and an atomic apply always writes a
  new copy while preserving source bytes, coordinates, and face records. The
  versioned report directs users and agents back through geometry, unit,
  topology, boundary-role, and inlet-direction preflight instead of treating
  name cleanup as mesh approval.
- Add `Project.actions()` and `agentcfd actions` as a read-only, state-aware
  operation catalog for AI agents and future GUIs. Each exact argv publishes
  availability, recommendation, I/O cost, mutation, solver-start, external-app,
  and approval semantics; cleanup and archive are preview-only entries.
- Add preview-first verified project archives. The default `decision` profile
  carries readable intent, owned inputs, results, and evidence without native
  solver state or field payloads; `portable` explicitly adds XDMF/H5 and
  post-processing. Creation is streamed and atomic, while `verify archive`
  checks safe member paths, the exact manifest index, and every payload hash
  without extraction. `agentcfd restore` now verifies before extraction,
  streams into same-parent staging, refuses overwrite, records the source
  archive identity, rewrites only navigation paths as project-relative, and
  atomically publishes a project that remains verifiable after relocation. A
  decision restore with source fields becomes an explicit `summary-only`
  result: missing spatial references are removed while their names and source
  result identity remain in restoration provenance.
- Add the versioned `agentcfd templates` catalog. Every built-in starting point
  now publishes provider compatibility, geometry mode, physics, default
  outputs, required decisions, limitations, and a copyable creation command;
  CLI `init` choices and provider defaults consume this same source of truth.
- Add `open_scientific_dataset()` and `agentcfd dataset inspect` as the
  fail-closed consumption side of campaign publication. Agents and scripts can
  discover ordered columns, units, ranges, matrix shapes, trust counts, and
  bounded rows or obtain raw-unit immutable matrices without solver, pandas,
  or NumPy dependencies; the optional arrays adapter returns NumPy matrices.
  `dataset plan` adds a content-bound, deterministic train/validation split and
  explicit unit-preserving z-score contract without copying sample payloads.
- Add atomic verified campaign datasets through `Project.export_campaign_dataset()`
  and `agentcfd export dataset`. Accepted points become streamable independent
  `agentcae.scientific-sample` JSON Lines under one content-hashed manifest;
  unaccepted points and reasons remain recorded, all included runs pass full
  project integrity, and input/output schemas must agree before publication.
  `agentcfd verify dataset` independently checks the manifest, counts, JSONL
  bytes/hash, every sample contract, schema consistency, and unique source IDs.
- Add project-level `scientific_sample()` / `export_scientific_sample()` and
  `agentcfd export sample`. A readable project can now become a strict
  `agentcae.scientific-sample` without custom Python glue: numeric `case.py`
  parameters are inputs, explicitly selected canonical quantities are outputs,
  units and provenance remain attached, and full project/summary/result
  integrity must pass before the sample is written.
- Materialize every completed run's bounded decision view as `summary.json`.
  Normal `result` and unified `project` reads now consume this small artifact
  instead of reparsing complete scalar histories, while `result.json` remains
  the authoritative full record. The summary records its source result's
  byte count and SHA-256, keeps field payloads closed, exposes observation
  cost, and continues to direct trust-boundary workflows to explicit
  verification. Project verification proves the decision artifact exactly
  matches the full result; legacy runs without the artifact remain readable.
- Add solver-neutral `outputs.flow_distribution()` for one-inlet/multi-outlet
  equipment. One compact request now publishes per-branch volume and mass flow,
  outlet fractions, coefficient of variation, inlet/outlet imbalance, and
  optional target-fraction errors. Imported OpenFOAM projects accept multiple
  static-pressure outlets, generate the report by default, monitor every port,
  and fail closed if final flow reverses or any branch is omitted.
  CLI and strict JSON project creation can materialize complete outlet targets
  and an optional maximum-fraction-error design gate directly into readable
  `case.py` intent.
- Add provider-neutral `outputs.require()` design criteria over canonical
  scalar results. Inclusive bounds, missing values, and exact-unit mismatches
  become explicit `requirement.*` checks across every numerical provider;
  requirements affect acceptance while scientific trust remains independently
  derived from runtime, verification, and validation evidence. Lightweight JSON
  and human `result` views expose every requirement, status, value, and target
  and distinguish an unmet design from failed scientific evidence.
- Add solver-neutral `outputs.flow_uniformity()` with compact velocity-vector
  uniformity, signed area-normal velocity, and patch-area results. Imported and
  baffled OpenFOAM providers lower it to two field-free surface reductions,
  validate flow-surface roles, preserve histories, and fail closed on invalid
  index or inconsistent-area output. New projects request outlet quality by
  default without increasing XDMF/H5 frame cadence.
- Add solver-neutral `assess_component_loss()` and `agentcfd verify
  component-loss` for explicit straight-run baseline subtraction. The
  content-addressed assessment checks accepted source results, provider/runtime,
  fluid/study/procedure/walls, inlet area, bulk velocity, dynamic pressure,
  units, and reported K identity;
  equivalent geometry and measurement planes require a deliberate confirmation.
  Imported and baffled OpenFOAM results now expose canonical provider version
  and container-image provenance for future comparisons.
- Add `Project.campaign_operating_map()` and dependency-free SVG export for
  accepted campaign quantities. The CLI turns an explicit numeric `case.py`
  parameter and one canonical scalar result into a unit-labelled engineering
  curve without opening `result.json`, XDMF, or HDF5; excluded and unaccepted
  points remain explicit instead of silently influencing the decision line.
- Add `outputs.pressure_loss()` for compact mass-flow-averaged total-pressure
  loss and inlet-bulk loss coefficient reporting across imported and baffled
  internal-flow providers. Generated imported projects enable it by default;
  the derived total-pressure field is not retained, human results group the
  scalars as engineering reports, and OpenCFD v2606 integration evidence keeps
  static pressure drop distinct from irreversible total-pressure loss.
- Add `outputs.render_layout()` and `agentcfd view --layout` for reproducible
  multi-panel ParaView overviews that read one XDMF/H5 payload. Slice recipes
  can now select explicit Cartesian, magnitude, plane-normal, or tangential
  vector components; the generated normal/tangential arrays remain transient
  post-processing filters rather than duplicated volume fields.
- Add bounded per-project runtime history and `Project.performance()` /
  `agentcfd performance`. Completed runs contribute at most 50 field-free
  samples; active `status` and `watch` prefer comparable-run ETA calibration,
  while corrupt or unwritable advisory history can never fail a simulation.
- Add `Project.verify()` and `agentcfd verify project` as one explicit integrity
  boundary for the run record, content-addressed solution plan, scientific result,
  registered artifacts, and an optional XDMF/H5 field bundle. The report keeps
  byte verification, acceptance, and evidence trust separate, fails closed on
  identity drift, and can prove the legacy 0.1.0a3 OpenFOAM analysis identity
  from the verified plan without forcing an unnecessary rerun.
- Add a dependency-free `open_project()` Python entry point, `Project.snapshot()`,
  and `agentcfd project`. The versioned read-only snapshot unifies lifecycle
  state, compact results, published file roles, XDMF/H5 entry points, hidden
  provider-workspace retention, optional storage accounting, and a typed safe
  next operation without opening field payloads or starting a solver.
- Add optional `agentcfd[interop]` installation of the Apache-2.0 AgentCAE
  contract catalog and `agentcfd contracts --check-agentcae`. The audit derives
  AgentCFD's four produced cross-product identities from its installed JSON
  schemas, reports missing/duplicate/unexpected/version-drifted contracts, and
  keeps ordinary CFD workflows dependency-free.

## 0.1.0a4 — 2026-09-07

- Add solver-neutral thermal internal-flow intent: inlet temperatures, explicit
  adiabatic/fixed-temperature/signed-heat-flux wall conditions, energy-model
  completeness gates, canonical temperature output, and a CoolProp-state bridge
  to constant-property fluids. Add a bounded OpenCFD v2606 circular-pipe
  lowering using passive `scalarTransport`, flux-weighted bulk temperature,
  thermal XDMF/HDF5 output, and an explicit first-law energy-balance gate.
- Add `agentcfd init --template heated-pipe` with editable geometry, operating
  point, inlet temperature, and signed wall heat flux. Its project plan reports
  Re, Pr, Pe, expected outlet temperature, and the exact temperature-change
  policy before an external solver starts.
- Extend the versioned project-creation request with strictly validated
  heated-pipe defaults. Agents and future forms can now materialize geometry,
  velocity, inlet temperature, and signed wall heat flux into readable
  `case.py` without writing Python or backend dictionaries.
- Group the human `agentcfd result` view into flow, inputs, mesh quality,
  verification, runtime, and other engineering results, with dimensionless
  values rendered explicitly. The flat canonical JSON result contract is
  unchanged for agents and integrations.
- Add a strict, versioned `agentcfd.parameter-set/0.1` operating-point file to
  `check`, `plan`, `mesh`, and project `run`. Explicit `--param` values override
  the file, while the readable `case.py` remains the only model definition.
- Add `Project.parameter_set()` and `agentcfd params` to inspect or safely export
  a complete validated operating point from current factory defaults plus
  overrides; existing destination files are never replaced.
- Expose the existing compact OpenFOAM result profile on ordinary `check`,
  `plan`, and `run` commands through `--summary-only`, so a single screening run
  can avoid permanent XDMF/HDF5 fields without requiring a campaign.
- Tighten the project-status contract so every discovered factory parameter and
  its UI/agent metadata are structurally validated instead of exposed as an
  unconstrained object.
- Compute named-surface triangle count, area, centroid, oriented area vector,
  mean normal, and normal coherence during bounded STL/OBJ inspection. Empty
  inlet/outlet regions now fail before meshing, and human geometry output shows
  SI area and direction for confirmed flow patches.
- Reject reliably reversed Cartesian inlet velocities before project creation,
  planning, meshing, or solving. The signed-volume/patch-normal assessment is
  versioned, retained in model metadata, and stays explicitly indeterminate
  when surface orientation is insufficient instead of guessing.
- Lower `boundaries.mass_flow_inlet(kg_per_s)` for steady laminar imported
  volumes through OpenFOAM `flowRateInletVelocity`. Constant density converts
  the request to volume flow, patch normals set direction, and recovered mass
  flow must meet a relative target gate; imported RANS mass flow still fails
  closed pending a turbulence-aware inlet contract.
- Make velocity and mass flow mutually exclusive first-class inlet choices for
  imported-project creation in both CLI and versioned request APIs. Generated
  `case.py` preserves the chosen control as its readable default.
- Add steady laminar pressure-driven imported flow using the documented
  total-pressure inlet/static-pressure outlet combination with return-flow-safe
  velocity boundaries. Creation APIs accept an explicit total gauge pressure,
  while results distinguish requested total-to-static pressure, recovered
  static pressure drop, and mass flow.
- Expose every `case.py` factory parameter, default, current selection, and
  overrideability in project status and solution plans. Human status shows a
  compact editable-input summary, while one-command execution caches the
  imported factory to avoid repeated user-code side effects.
- Exercise pressure-controlled imported-project creation and parameter
  discovery from the exact dependency-free wheel in test and release jobs.
- Add solver-neutral `parameters.describe`, `parameters.number`, and
  `parameters.choice` metadata beside `case.py` factories. Generated pipe,
  baffle, and imported templates now publish labels, canonical units, bounds,
  choices, nullability, and descriptions for future forms and agents.
- Enforce declared numeric, integer, nullable, bound, and choice constraints on
  explicit project overrides before executing the factory or starting any
  provider work; coupled model validation remains in the engineering APIs.
- Publish signed-role-checked inlet/outlet volume and mass-flow histories for
  every imported flow control mode. A numerically balanced result now still
  fails acceptance when flux direction contradicts confirmed boundary roles.
- Add `agentcfd result` and `Project.result_summary()` as a filterable metadata
  view over the latest or selected run. It exposes quantities, failed checks,
  histories and field names without opening HDF5 or hashing external artifacts,
  and points to the explicit full-integrity verification command.
- Reuse byte-verified, content-addressed imported-geometry meshes across
  operating points. Corrupt or mismatched cache entries fail closed and are
  regenerated; storage inventory reports the cache separately and ordinary
  cleanup preserves it.
- Add preview-first `clean --include-cache`; ordinary cleanup protects reusable
  meshes, while explicit cache cleanup still preserves published results and
  campaigns.
- Surface imported-mesh acquisition (`generated` or `cache-hit`) in result and
  run provenance, and show the latest parameter overrides in human project
  status so parameterized results cannot be mistaken for `case.py` defaults.
- Add `init --template imported-internal-flow` as the safe geometry-to-project
  bridge. It inspects and owns STL/OBJ plus normalized role records, requires
  explicit units, interior seed, inlet vector, mesh size, and cell budget, and
  emits a ready-to-plan readable project without manual Python authoring.
- Add a strict, versioned project-creation request API and `init --request` for
  agents and future GUIs; relative geometry paths resolve beside the request,
  while generated `case.py` remains the sole long-lived scientific source.
- Add explicit `--accept-name-roles` and the matching versioned request gesture
  for well-named geometry. Every surface suggestion must be unambiguous; no
  fallback role is invented and failure occurs before project files are written.
- Add an explicit Cartesian turbulent-velocity inlet and the first imported
  k-omega SST RANS slice with blended wall functions, `k`/`omega`/`nut` fields,
  mandatory min/max/average y-plus evidence, a 30--300 hard acceptance range,
  and generated-project parameters.
- Make the public analysis-request fingerprint the sole project/provider result
  identity and refuse to publish a provider result carrying a different one.
- Lower imported-volume point probes, scalar pressure surface reports, and wall
  force reports to compact OpenFOAM function objects; recover canonical SI
  quantities and histories without adding field frames.
- Share report lowering/recovery across structured and imported providers, and
  correct pressure-area integrals to force units plus total-force aggregation.
- Bind project-published XDMF/H5 fields to the exact OpenFOAM source-mesh
  identity and avoid dangling provider-workspace paths after automatic cleanup.

- Add dependency-free `geometry-check` for binary/ASCII STL and OBJ. The
  versioned report makes units, SI bounds, regions, degeneracy, open and
  non-manifold edges, orientation, topology memory limits, and imported-mesh
  readiness explicit; STEP/IGES fail closed pending controlled tessellation.
- Add explicit boundary-role confirmation to geometry preflight. Name-based
  suggestions never apply automatically; versioned maps must cover exact
  regions, and internal-flow policy requires confirmed inlet and outlet roles.
- Add content-addressed `ImportedSurface` model intent, explicit Cartesian
  velocity inlets, source presence/hash gates, required interior points, and
  hard mesh-cell budgets without machine-specific paths in model fingerprints.
- Add `agentcfd mesh` planning, deterministic preparation, OpenFOAM-native
  geometry dry-run, `snappyHexMesh -overwrite`, and checkMesh execution with
  cell-count, non-orthogonality, skewness, and aspect-ratio acceptance gates.
- Add the first steady incompressible isothermal laminar imported-volume flow
  provider. It runs the same project lifecycle through SIMPLE convergence,
  conservation and output checks, then publishes standard verified XDMF/H5.
- Add run-scoped `logs --run-id` and `diagnose --run-id` so a later successful
  campaign point cannot hide an earlier failure. Sweep failures now preserve
  their immutable run id, directory, exact diagnosis command, and distinct
  failed-versus-engineering-review outcome.
- Add `sweep --max-runs` as a hard zero-work approval boundary after accepted
  cache reuse. Equivalent points inside a request share one solver attempt even
  when that attempt fails, and reports expose planned versus actual starts.
- Add `sweep --summary-only` for low-storage operating-map screening. It keeps
  quantities, checks, logs, and provenance while skipping VTK/XDMF/HDF5 and
  removing provider-native bulk; a distinct fingerprint prevents lightweight
  results from satisfying later full-field requests.
- Add `agentcfd promote` to turn one accepted immutable summary point into a
  provenance-linked full-field campaign result. Promotion fails on changed
  analysis intent and reuses an existing accepted full-field identity.
- Add preview-first `agentcfd compact` for accepted full-field campaign points.
  Explicit `--apply` preserves engineering metadata, logs, and derived
  CSV/images while removing regenerable spatial bulk and updating identity.
- Add `agentcfd campaigns` as a field-free design-point index over immutable
  project runs. New run markers carry compact canonical quantities; optional
  recursive storage accounting and unit-preserving CSV export are explicit,
  while the default path opens neither result histories nor HDF5 fields.
- Add explicit scalar `case.py` factory parameters across project check, plan,
  and run. Repeatable `--param NAME=VALUE` inputs are signature-checked,
  fingerprinted, preserved in interrupted/final run markers, propagated through
  resume, and flattened into the campaign design table.
- Add versioned campaign-request and campaign-sweep contracts plus
  `agentcfd sweep`: plan every named point before work starts, reuse only exact
  accepted result identities, persist atomic progress, continue after isolated
  runtime failures by default, and remain serial until bounded scheduling lands.
- Add zero-solve `sweep --plan-only` with explicit ready/reusable/solver-start
  counts and request-level duplicate detection. Accepted reuse now also requires
  the compact result payload to exist, not merely a stale completion marker.
- Add typed final-frame line profiles over portable fields. The generated
  ParaView recipe uses explicit endpoints and sample count, publishes a compact
  canonical two-column CSV, requires an explicit component or magnitude for
  vector intent, and still shares the existing XDMF/HDF5 payload without
  another field copy.
- Add mutually exclusive interactive `view --launch` and headless
  `view --batch` execution. Batch mode discovers `pvbatch`, waits for its exit
  status, fails visibly on script errors, and reports only outputs that exist.
- Persist explicit solver-workspace retention as protected user intent. Normal
  cleanup cannot remove `--keep-workspace` or manifest-retained data; the new
  `--include-retained` scope must be previewed and applied deliberately, while
  active runs and sole recovery checkpoints remain non-removable.
- Extend text-only ParaView recipes with typed camera and render intent:
  deterministic position/focal/up vectors, optional parallel projection,
  image size, transparent PNG screenshot, PNG animation sequence, or MP4. The
  generated outputs remain opt-in and share the existing XDMF/HDF5 payload.
- Extend `agentcfd doctor` with an optional project context that audits model,
  provider, runtime, output budget, filesystem headroom, latest-run health,
  acceptance, storage, and recovery in one contract. Report a comparable
  cell-update work proxy while refusing to invent energy use without executor
  power or joule telemetry.
- Preserve the generated solver workspace automatically after a failed run and
  add `agentcfd logs` as a bounded, project-aware diagnostic surface. It reads
  either the live workspace or compact published evidence, supports explicit
  provider-command selection, and returns a versioned JSON contract for agents.
- Add deterministic `agentcfd diagnose` classification for storage/memory,
  missing files, boundary and dictionary configuration, mesh quality,
  numerical divergence, MPI, timeout, killed process, and generic OpenFOAM
  failures. Findings retain the exact bounded-log evidence, confidence, a
  conservative repair, and an explicit no-automatic-repair decision.
- Add identity-gated `agentcfd resume` for failed or interrupted transient
  channel projects. Complete native checkpoints are integrity-checked, staged
  outside replace-mode deletion, restored with `latestTime`, and linked to the
  source run in result provenance; changed model/runtime inputs fail closed.
- Carry compact monitor/report histories inside restart bundles, accept a
  complete end-time checkpoint for termination recovery, and exclude only
  non-solver operational controls (timeout, workspace retention, portable
  export) from resume compatibility.
- Expose recovery availability in status and diagnosis, protect the sole
  recoverable workspace from cleanup, and remove the superseded source
  workspace only after a successful resumed publication.
- Stream OpenFOAM command output to bounded-memory live logs and expose a
  field-free progress snapshot through `status`: command, elapsed time,
  physical time/iteration, conservative ETA range for transient runs, latest
  residuals and Courant number, compact mass/pressure monitors, and opt-in
  workspace size.
- Apply live logging consistently to steady pipe, transient channel, and
  turbulent precursor providers; treat permission-denied PID probes as proof
  of liveness across managed execution boundaries instead of falsely reporting
  an active run as interrupted.
- Discover the nearest project manifest from any nested file or directory and
  keep recommended commands short when the user is already somewhere inside
  the project.
- Add `agentcfd watch` for low-overhead polling until a terminal state, with
  compact human lines or machine-friendly JSON Lines; active project status now
  recommends this hands-off path.
- Add typed plane-slice, scalar-contour, and line-seeded streamline output
  recipes. Runs publish portable ParaView Python scripts and a versioned recipe
  manifest that share the existing XDMF/HDF5 payload; `view --recipe --launch`
  opens the reproducible pipeline directly.
- Calibrate temporary-output preflight with 1.25 measured headroom after a real
  20-frame v2606 run exposed the gap between raw staging arithmetic and managed
  peak usage; keep native channel fields binary and avoid ineffective
  non-ASCII `writeCompression`.
- Add a unified project `status` state machine with one shell-safe next action,
  input-change detection, run phase, acceptance summary, and post-processing
  target for both human and JSON clients.
- Record atomic `preparing`, `running`, `exporting`, `failed`, and completed run
  phases; refuse concurrent replace runs and retain repairable failure details.
- Add managed `storage` inventory and preview-first `clean`; applying cleanup
  preserves current results, campaigns, and every live solver workspace.
- Add `view` result discovery and ParaView launching, including macOS App
  discovery when the executable is not on `PATH`; make each output directory
  self-documenting with a compact `README.md`, and summarize frame axis, size,
  canonical variables, and point/cell association without loading HDF5.
- Select OpenFOAM time directories and native fields before `foamToVTK`, record
  that selection in field provenance, and avoid staging unused VTK data.
- Add versioned project status, logs, storage, cleanup, view, and structured error
  JSON Schemas; JSON failures now include code, repair, and retry guidance.

- Add backend-neutral named regions, rectangular channels with wall-attached
  baffles, pressure-inlet, mass-flow-outlet, slip, and symmetry boundaries.
- Add explicit uniform, potential-flow, and previous-result initialization plus
  global/local mesh intent, wall layers, and mesh-quality gates.
- Add compact point probes, surface reductions, and force reports independently
  from full-field frames, plus discoverable typed result queries.
- Validate region coverage and roles across boundaries, mesh controls, and
  reports; require providers to reject unsupported step intent during planning.
- Add an executable low-Re transient bottom-baffle project and reusable
  `baffle-channel` project template backed by OpenCFD v2606 `blockMesh`,
  `potentialFoam`, and `pimpleFoam`.
- Add deterministic five-block conformal channel meshing, Reynolds
  applicability rejection, adaptive Courant control, mass/pressure histories,
  canonical probe/surface/force recovery, field recovery, and mesh/runtime
  evidence.
- Add an installed `analysis-request` schema and a public complete-step content
  fingerprint for deterministic AI/GUI planning and comparison.

- Separate full-field frames, scalar histories, and native restart checkpoints
  in the public output contract; add `outputs.animation`, `outputs.checkpoints`,
  human-readable storage budgets, hard frame caps, and transient procedures.
- Add plan-time frame/temporary-peak estimates with fail-closed output issues,
  plus exact selected-array budget enforcement before portable export.
- Repack numeric XDMF/HDF5 datasets with bounded-memory chunked compression,
  record estimated and actual storage in the field-bundle manifest, and request
  binary compressed native output from the OpenFOAM pipe provider.

- Add the first AgentFEM-style public project lifecycle: `init`, `check`,
  inspectable `plan`, short-form `run`, and `inspect`, with `case.py` as modeling
  truth and operational settings isolated in `agentcfd.toml`.
- Add addressable project issues, deterministic plan fingerprints, explicit
  provider/runtime/I/O readiness, and structured run records.
- Make a single readable `output/` the default project publication surface;
  reruns safely replace only AgentCFD-managed output, while `--campaign`
  explicitly preserves immutable history.
- Move generated OpenFOAM cases and VTK conversion data into a hidden solver
  workspace, copy compact execution evidence into the published result, and
  discard backend bulk after success unless `--keep-workspace` is requested.
- Mark a run directory as AgentCFD-owned before external execution so an
  interrupted solver or field export remains diagnosable and can be safely
  replaced on the next ordinary run.
- Add a standard `agentcae.field-bundle/0.1.0` export with XDMF/HDF5 time
  series, pickle-free NPZ arrays, canonical CFD names and units, explicit
  point/cell and axis semantics, source context, and SHA-256 artifacts.
- Add `agentcfd export openfoam` and `verify field-bundle`; OpenFOAM project
  runs automatically attach portable field artifacts and field records.
- Add `export field-sample`, an exact pickle-free bridge to AgentFEM's
  `FEMFieldSample` NPZ keys with field encoding, units, axis, and provenance.
- Identify the neutral `agentcae.field-bundle` contract inside HDF5 itself, so
  detached heavy-data files fail closed instead of relying only on filenames.
- Add explicit `visualization`, `native`, and `both` portable-field profiles
  plus repeatable canonical field selection; ordinary CLI and project output
  now avoid duplicate point/cell arrays by default.
- Recover transient physical time from OpenFOAM `case.vtm.series` rather than
  misinterpreting adaptive-step VTK sequence numbers as time.
- Register canonical vorticity and Q-criterion names and SI units.
- Make XDMF/HDF5 the ordinary portable output and NPZ an explicit opt-in via
  `--with-npz` or `OutputRequest.portable_formats`, avoiding duplicate storage
  and time-series array stacking in visualization-only workflows.
- Honor `FieldFrames.include_initial` during OpenFOAM conversion with
  `foamToVTK -noZero`, preventing missing derived fields at time zero; avoid
  duplicating native field snapshots in published evidence unless the solver
  workspace is explicitly retained.
- Keep NumPy, h5py, and meshio optional under the `io` extra; the mandatory
  AgentCFD core remains dependency-free and Apache-2.0.

- Add a content-addressed periodic `simpleFoam`/`meanVelocityForce` k-omega SST
  circular-pipe precursor with CLI prepare/run workflows.
- Recover developed `U`, `p`, `k`, `omega`, and `nut`, pressure-gradient and friction
  evidence, residuals, y-plus, mesh identity, and immutable container identity.
- Record OpenCFD v2606 c8 execution evidence and the fail-closed rejection of
  `boundaryFoam` for circular geometry.
- Add accepted-precursor mapping to downstream turbulent pipes with source,
  mesh, runtime, and field identity checks before and after OpenFOAM `mapFields`.
- Separate 1.05% precursor-to-target transfer verification from the 3.81%
  smooth-Colebrook model-form diagnostic, and solve mapped velocity fields to
  their absolute linear tolerance.
- Publish the precursor-map JSON Schema and a verified 38,400-cell OpenCFD
  v2606 execution record.
- Add physical near-wall control through `nominal_wall_cell_fraction`; solve
  OpenFOAM block grading from the geometric series and preserve the control in
  prepared-case recovery, result quantities, CLI, and precursor compatibility.
- Reject cumulative precursor grading before meshing when the estimated axial-
  to-smallest-radial cell ratio already exceeds the declared aspect limit.
- Add the installed `turbulent-wall-study` contract and CLI verifier, which
  separates wall-function consistency and a fine-pair plateau from formal GCI
  applicability and uncertainty promotion.
- Add content-addressed `prepare`/`run openfoam-turbulent-wall-study` commands
  that bind three precursor meshes and iteration budgets, execute them, and
  emit the assessment without manual result assembly.
- Replace the precursor's five-sample local stability gate with an explicit
  50-sample window after extended runs exposed slow false convergence.
- Record real OpenCFD v2606 c8/c16/c32 fixed-wall-cell evidence at Re 99,621.
  Mean y-plus varies by 1.72%, pressure gradient converges monotonically, and
  the fine-pair change is 0.630%; non-similar refinement correctly remains
  ineligible for GCI.
- Add a precursor-specific geometrically similar GCI candidate verifier using
  `h/D = 1/cross_section_cells` rather than an invalid 3-D cell-count exponent.
  The real uniform c8/c12/c18 candidate keeps y-plus above 30 and reaches a
  0.212% fine-pair plateau, but rejects GCI because the sequence is oscillatory.
- Make the periodic SST momentum wall function an explicit, hashed runtime
  control with three supported OpenFOAM implementations and prepared-case
  integrity enforcement.
- Add content-addressed `prepare`/`run` and direct `verify` wall-function study
  workflows plus installed JSON contracts.
- Record identical-mesh v2606 sensitivity evidence: Spalding reduces the c16
  Colebrook difference from 5.634% to 1.851%. Its tighter-solver fixed-wall
  c8/c16/c32 family reaches a 0.00689% fine-pair pressure-gradient plateau with
  1.746--1.858% correlation differences, while default promotion remains false
  pending broader Reynolds and experimental validation.
- Add a bounded periodic k-epsilon precursor path with model-specific
  `epsilon`, `epsilonWallFunction`, `nutkWallFunction`, output semantics,
  capability identity, prepared-case verification, and CLI controls.
- Separate outer SIMPLE convergence targets from tighter inner linear-solver
  tolerances. This removes the skipped-equation residual plateau observed in
  real k-epsilon runs and turns the c16/4000 case into accepted, stable evidence.
- Add content-addressed `prepare`/`run` and source-hashed `verify` turbulence-
  model studies with installed JSON contracts. On one identical OpenCFD v2606
  c16 mesh, SST/Spalding differs from smooth Colebrook by 1.851% versus 3.289%
  for k-epsilon/nutk; the latter is faster, while global default promotion
  remains explicitly false.
- Add a correlation-based OpenFOAM wall-mesh preflight and `--target-y-plus`
  model-study option, while retaining solved patch y-plus as the acceptance
  authority. Expose the underlying estimate through `calculate wall-resolution`.
- Add a source-hashed multi-Re turbulence-model sweep contract and CLI. Its
  four-point v2606 evidence accepts the matrix but rejects a global default:
  SST/Spalding wins at Re 49,810 and 99,621, while k-epsilon/nutk wins at Re
  199,242 and 498,104; the two high-Re best errors remain above 2%.
- Add content-addressed `prepare/run openfoam-turbulent-model-sweep` campaign
  orchestration with adaptive y-plus meshes, point-level progress, nested-plan
  integrity checks, artifact-reverified resume, and automatic final aggregation.

All notable AgentCFD changes are recorded here. Versions follow semantic
versioning; scientific capability maturity remains independently visible in the
machine-readable capability catalog.

## 0.1.0a3 — 2026-09-04

- add an explicit `k-omega-sst` smooth circular-pipe model, declared wall
  treatment, and a typed turbulence-intensity/length-scale inlet that all
  remain part of model identity;
- lower the turbulent slice to OpenCFD v2606 `simpleFoam` using an exact
  flow-rate inlet, `kOmegaSST`, `k`, `omega`, and blended wall functions;
- recover native `U`, `p`, `k`, `omega`, and `nut` fields plus per-iteration
  y-plus, flow, pressure, residual, and linear-iteration histories;
- add explicit wall-y-plus, turbulent residual, observable-stability, friction,
  runtime-version, mesh, conservation, and output-completeness gates;
- keep the smooth-pipe friction comparison diagnostic and fail reference
  applicability closed until developed-inlet and turbulent grid evidence pass;
- add human- and agent-facing `prepare` and `run openfoam-turbulent-pipe`
  commands with deterministic manifests, stable completed-unaccepted status,
  and compact failed-gate guidance in both JSON and human output;
- reduce the measured 38,400-cell benchmark solve from a multi-minute strict
  linear-solver configuration to about 15 seconds while preserving explicit
  RANS convergence evidence;
- stop the exact Docker container on keyboard interruption as well as timeout,
  avoiding orphaned OpenFOAM work;
- validate the first real OpenCFD v2606 arm64 run at Re 99,621: converged,
  relative mass imbalance `5.27e-7`, average y-plus `86.55`, and intentionally
  unaccepted 10.94% smooth-pipe friction difference.

## 0.1.0a2 — 2026-09-03

- validate the fully developed OpenCFD v2606 pipe workflow with an accepted
  8/16/32 three-grid study: observed order 2.0443, fine-grid GCI 0.5079%, and
  fine-grid Hagen--Poiseuille pressure-drop error 0.2873%;
- normalize the analytic inlet profile by its discrete area integral so every
  mesh matches the requested physical circular-pipe flow to machine precision;
- accept bounded axis-aligned pipe convergence from axial residual, pressure
  stability, and conservation evidence when zero transverse-component
  normalized residuals prevent OpenFOAM's aggregate marker;
- bind prepared cases to model, procedure, and output request with an analysis
  SHA-256, and require matching analysis identities in new GCI result records;
- update the validated grid-study default to 12,800 / 102,400 / 819,200 cells
  and repair the GCI schema for its three acceptance checks.

- recover OpenFOAM inlet/outlet flow and pressure histories automatically;
- add `checkMesh` execution, structured mesh-quality observables, physical-Pa
  pressure drop, mass balance, final native fields, and runtime provenance;
- add an explicit non-compiling fully developed circular-pipe velocity inlet;
- add end-to-end OpenFOAM execution to the CLI;
- add explicit Docker-backed OpenFOAM execution for macOS and CI without
  wrapper scripts;
- add solver-neutral three-grid Richardson extrapolation and GCI utilities with
  unequal-ratio support and fail-closed oscillatory-convergence handling.
- add dependency-free hydraulic diameter, Reynolds number, Darcy--Weisbach,
  bracketed Colebrook--White, and local-loss engineering functions; transitional
  flow is rejected instead of silently interpolated;
- add an auditable composite pipe-loss estimate for laminar and turbulent
  incompressible screening calculations;
- reject non-finite physical inputs and non-integral solver/mesh controls at the
  public boundary;
- expose auditable cross-section and axial mesh resolution controls in the CLI;
- add a CLI workflow and JSON schema that computes GCI directly from three
  converged same-model AgentCFD result files and hashes every source;
- make the core runtime dependency-free while retaining NumPy array support as
  an optional `arrays` extra;
- add ideal-gas/Mach screening functions and a lazy optional CoolProp property
  provider with structured IF97 water/steam state records;
- add a fail-closed same-model OpenFOAM three-grid preparation workflow with
  isotropic resolution scaling, per-case identities, and a JSON study schema;
- make OpenFOAM mass-balance and pressure-error acceptance thresholds explicit,
  validated, and part of every result's scientific inputs;
- reject unknown boundary objects, non-boolean study flags, duplicate output
  names, and non-finite or non-JSON model metadata before fingerprinting;
- report Reynolds number and an explicit uniform-inlet laminar development
  length diagnostic instead of conflating entrance effects with mesh error;
- verify model, file, combined-case, and path-containment identities before
  executing an existing prepared OpenFOAM case;
- execute a prepared three-grid family end to end and automatically write
  source-hashed GCI evidence, while rejecting mixed prior execution output.
- recover structured per-equation initial residual, final residual, and linear
  iteration histories from OpenFOAM solver evidence.
- reject unrecorded prepared-case files, directories, and symbolic links that
  could alter solver semantics outside the content-addressed manifest.
- make the console fail closed with stable exit codes for execution failure,
  expected input errors, and completed-but-unaccepted results.
- record the immutable Docker image SHA-256, repository digests, and platform,
  and fail container-run acceptance when that provenance cannot be verified.
- make generated-case byte identities and unrecorded-path checks portable
  across LF and CRLF hosts.
- add fail-closed turbulent-pipe wall-resolution and target-`y+` screening
  functions for future RANS mesh setup.
- compare recovered inlet flow against the public boundary request under an
  explicit policy, so coarse inlet integration error cannot be hidden.
- record resolved mesh controls, verify expected versus actual cell count, and
  bind final fields to a content-addressed native `polyMesh` manifest.
- add a dependency-free installed-contract discovery API and CLI for AgentCFD,
  AgentFEM, AI pipelines, and external validators.
- add dependency-free result reopening that recomputes trust and verifies every
  artifact identity; use it automatically before file-based GCI.
- reject duplicate JSON keys and non-standard non-finite numbers when reopening
  scientific result evidence.
- stop the exact Docker container identified by a per-command CID file when an
  OpenFOAM subprocess times out.
- add explicit turbulence-intensity/length-scale initialization for `k`,
  `omega`, and `epsilon` as a bounded RANS setup primitive.
- add a bracketed circular-pipe operating-point solver that inverts available
  pressure into flow without hiding laminar, transitional, or turbulent choice.
- expose forward pipe loss and inverse pressure-to-flow calculations through a
  dependency-free JSON CLI for engineers and agents.
- publish the content-addressed OpenFOAM mesh manifest as an installed,
  versioned JSON Schema contract.
- add an explicit GCI promotion policy for fine-grid uncertainty and
  asymptotic-range evidence, with fail-closed CLI status.
- fail scientific acceptance closed for unvalidated OpenFOAM runtime versions.
- validate OpenFOAM output requests and fail acceptance when requested native
  fields or histories are missing after execution.
- record command-level return codes and monotonic wall-clock durations for mesh,
  mesh checking, and solver execution.
- require structured pressure and velocity outer-residual evidence below the
  configured tolerance in addition to OpenFOAM's convergence marker.
- expose a validated per-command OpenFOAM timeout for single and three-grid CLI
  executions.
- require a configurable stable tail window for pressure drop in addition to
  algebraic residual convergence.
- reject ambiguous or non-standard JSON in prepared OpenFOAM case and grid-study
  control records.
- make non-orthogonality, skewness, and aspect-ratio mesh limits explicit
  scientific acceptance inputs instead of relying only on `Mesh OK`.
- separate uniform-inlet entrance effects from discretization error by making
  fully developed pressure-reference applicability an explicit validation gate.
- add authoritative IAEA thermal-mixing, Sandia turbulent-flame, and NIST spray
  combustion cases to the machine-readable benchmark roadmap.
- mark every benchmark dataset link-only until redistribution terms are
  explicitly reviewed.
- expand the development dependency inventory and record NumPy's composite
  permissive license expression rather than reducing it to one top-level label.
- expose a dependency-free machine-readable license catalog for core, optional
  Python extras, and the external OpenFOAM process boundary.
- publish installed JSON Schema contracts for benchmark and license catalogs.
- add a solver-neutral single-point validation assessment with explicit
  numerical, input, experimental, and coverage-factor uncertainty components.
- expose validation-point assessment through the CLI and an installed JSON
  Schema with fail-closed scientific exit status.
- reject boolean values consistently across public engineering correlations,
  including roughness and local-loss inputs.
- reject boolean numeric quantities and non-boolean result/check state before
  computing scientific acceptance or trust.
- enforce unused-import linting in the default quality gate.
- reject boolean serialized quantities and non-boolean derived acceptance state
  before reopening result evidence or computing GCI.
- validate serialized check names/kinds and reject boolean artifact sizes during
  result evidence reopening.
- reject non-string mapping keys before model or scientific-input fingerprinting
  to prevent JSON key-normalization collisions.
- require identical quantity units and dimensionless cell counts across all
  serialized results in a GCI study.
- validate typed quantity, field, history, artifact, and check collections when
  constructing a result instead of failing later during serialization.
- add an explicit, auditable low-Mach incompressible-model screening result.
- reject ambiguous non-string array and learning-sample names, and duplicate
  requested outputs, before AgentCAE serialization.
- fail closed on malformed or duplicate CFD-to-FEM coupling fields and validate
  emitted coupling manifests against the installed contract.
- expose low-Mach model screening through the human- and agent-facing CLI.
- publish a capability-catalog JSON contract and advertise gas screening and
  single-observable validation with explicit maturity boundaries.
- enforce runtime types for model/step components and non-empty string names
  for physical geometry and fluid assets.
- cross-check quantity records and artifact indexes when reopening a result so
  redundant exchange representations cannot silently disagree.
- validate every thermophysical-state identity and positive SI property at its
  construction boundary, including manually created records.
- expose versioned CoolProp/IF97 pressure-temperature states through the CLI.
- version thermophysical-state records and ship their JSON Schema contract.
- document agent-facing gas screening, IF97 state evaluation, and installed
  contract discovery in the primary quickstart.
- exercise low-Mach screening in both offline-wheel CI and release smoke gates.
- fail closed with domain errors when GCI receives malformed record, label,
  policy, or solution types instead of leaking incidental attribute failures.
- validate constructed GCI result records for finite values, ordered refinement
  ratios, non-negative uncertainty, boolean state, and a valid safety factor.
- include the declared monotonic-convergence state in GCI promotion acceptance.
- keep the turbulent inverse pipe-flow bracket strictly above Re 4000 despite
  floating-point reconstruction roundoff.

## 0.1.0a1 — 2026-09-03

- establish the Apache-2.0 AI-native CFD engineering workflow;
- add typed internal-flow studies, circular-pipe geometry, Newtonian fluids,
  boundaries, procedures, outputs, model fingerprints, and structured results;
- add the released Hagen–Poiseuille reference provider with applicability,
  Darcy–Weisbach identity, and mass-balance checks;
- add experimental content-addressed OpenFOAM `simpleFoam` case lowering for a
  full three-dimensional O-grid circular pipe;
- preserve the circular pipe boundary with explicit `blockMesh` arc edges and
  add a reproducible OpenCFD v2606 Linux/arm64 execution-evidence record;
- distinguish normal solver completion from OpenFOAM's explicit numerical
  convergence marker in structured result trust semantics;
- add bounded external-process execution that remains scientifically unaccepted
  until field conservation and pressure-loss recovery are implemented;
- add AgentFEM interoperability records and versioned JSON schemas;
- align results with AgentFEM semantics for quantities, fields, histories,
  artifacts, scientific-input fingerprints, evidence claims, and trust levels;
- add solver-neutral `agentcae.simulation-result` and AgentFEM-compatible
  `agentcae.scientific-sample` records, with schemas included in release wheels;
- document concepts, workflow, validation, licensing, roadmap, market strategy,
  and PyPI trusted publishing;
- test Python 3.11–3.13 across Linux, macOS, and Windows in CI.
