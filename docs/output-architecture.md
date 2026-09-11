# Output architecture: observe more, store less

AgentCFD treats output as part of the analysis contract, not as a provider file
setting. Solver steps, scalar monitoring, visualization frames, and restart
checkpoints have different jobs and therefore have separate retention policies.

```text
solver timesteps       dense, normally never retained
├── histories/probes   small scalar series, sampled frequently
├── field frames       selected variables for XDMF/H5 visualization
└── checkpoints        sparse native states, rolling retention
```

Completed output has a matching control/data split: `summary.json` is the
bounded decision plane, `result.json` retains complete scalar histories and
evidence metadata, and `fields/fields.h5` is the optional large spatial payload.
Normal agent polling touches only the first layer; explicit inspection and
verification opt into the deeper layers.

This mirrors mature solver practice without exposing backend vocabulary:

- OpenFOAM function objects calculate and write requested derived data at
  independent execute/write frequencies, explicitly reducing the need to store
  every generated field: <https://doc.openfoam.com/2212/tools/post-processing/function-objects/>.
- OpenFOAM `controlDict` separately provides write cadence, rolling `purgeWrite`,
  format, precision, and compression:
  <https://doc.openfoam.com/2306/fundamentals/case-structure/controldict/>.
- SU2 independently distinguishes screen/history output, volume output, and
  restart/visualization file frequencies:
  <https://su2code.github.io/docs_v7/Custom-Output/>.
- ParaView Catalyst demonstrates the long-term in-situ direction: extract only
  meaningful data during the simulation instead of writing and rereading every
  full field: <https://docs.paraview.org/en/latest/Catalyst/introduction.html>.

## Public Python contract

An ordinary steady analysis retains one final visualization field:

```python
output=outputs.standard()
```

A transient animation declares physical cadence, a hard frame cap, sparse
restart intent, and a fail-closed storage budget:

```python
output=outputs.animation(
    every=0.05,
    fields=(
        "fluid.velocity",
        "fluid.pressure",
        "fluid.vorticity",
    ),
    maximum_frames=240,
    restart=outputs.checkpoints(every=1.0, keep=2),
    storage_budget="512 MiB",
)
```

`every=0.05` means one full field frame per 0.05 seconds. It does not set the
solver time step. Compact histories such as mass balance and pressure drop can
still be sampled much more frequently. `maximum_frames` prevents an accidental
millisecond interval from creating ten thousand full-domain snapshots.

`storage_budget` accepts bytes or explicit SI/IEC strings such as `750 MB` and
`2 GiB`. The current policy fails closed when a conservative estimate exceeds
the budget; AgentCFD never silently drops requested scientific data.

For a single OpenFOAM screening run that needs engineering decisions but no
spatial field review, use `agentcfd check . --summary-only`, then the same flag
on `plan` and `run`. The result retains compact quantities, histories, checks,
logs, and provenance while omitting the permanent XDMF/H5 bundle. This is an
explicit result profile with its own execution identity, not an automatic
deletion policy; rerun normally when full fields are needed.

## Planning before solving

`agentcfd plan` resolves the requested field count and publishes:

- each output channel and its retention rule;
- estimated mesh cells when the provider mesh is predictable;
- estimated final portable bytes;
- estimated temporary peak bytes;
- compression and budget decisions;
- an addressable `OUTPUT_POLICY_INVALID` or `OUTPUT_BUDGET_EXCEEDED` issue.

The temporary estimate includes a measured 1.25 headroom factor over native,
VTK, and portable staging. A 23,880-cell, 20-frame OpenCFD v2606 baffled-channel
run occupied 122.05 MiB with its workspace deliberately retained, versus a
101.84 MiB raw staging estimate; the calibrated preflight is 127.30 MiB. The
factor and evidence are visible in `estimate_calibration`, rather than hidden
as an unexplained constant.

The adapter passes requested time directories and native field names directly
to `foamToVTK`, avoiding VTK copies for unused checkpoints and variables. Each
managed conversion uses a unique `-name` directory inside the disposable case,
so an old or user-owned `VTK/` tree cannot enter a new bundle. The exporter reads
one frame, writes it to HDF5, then removes that VTU before reading the next; its
manifest records the reclaimed byte count. Preconverted VTU inputs remain
untouched. Each numeric HDF5 dataset is created with its final chunking and
gzip/lzf filter, so no uncompressed HDF5 file or `.repack` copy coexists with
the result. Native OpenFOAM data and all selected temporary VTUs still coexist
at the beginning of conversion; later in-situ adapters can remove that remaining
amplification without changing the public output contract. Generated channel
cases use binary native fields. OpenCFD v2606 explicitly disables
`writeCompression` for non-ASCII format, so requesting compression there only
adds a warning and no savings; compression is applied to the durable HDF5
product instead.

## Portable storage

XDMF remains the lightweight index and HDF5 the field payload. The exporter:

1. reads the first fixed-mesh frame and determines the exact selected arrays;
2. estimates geometry, topology, all frames, and optional NPZ duplication;
3. refuses the export before creating its destination when over budget;
4. writes each numeric dataset once with its final chunked `gzip` or `lzf`
   compression;
5. records estimated and actual byte counts in `manifest.json`.

HDF5 compression requires chunked datasets, and chunk selection affects I/O
performance. The implementation writes each mesh/field array directly through
h5py with automatic chunks, shuffle, and a portable gzip level; it neither
loads the complete time series nor recopies the completed HDF5 file. See the
h5py dataset documentation: <https://docs.h5py.org/en/stable/high/dataset.html>.

NPZ remains opt-in. It is useful for explicit array/learning workflows, but it
duplicates the portable field payload and is not required for ParaView or
AgentFEM's XDMF/H5 path.

## Current and next implementation boundary

Implemented now:

- typed frame, checkpoint, compression, and budget policies;
- transient procedure intent with study/procedure consistency checks;
- plan-time frame and peak-storage guards;
- measured temporary-staging headroom with inspectable calibration evidence;
- export-time selected-array budget enforcement;
- single-pass chunked HDF5 compression and storage provenance, with zero
  temporary HDF5-copy bytes;
- binary OpenFOAM native output for generated cases;
- selected-time and selected-field conversion before temporary VTK creation;
- isolated `foamToVTK` output with per-frame VTU consumption and failure-safe
  staging cleanup, preserving any user-owned `VTK/` tree;
- multi-view ParaView overview scripts and derived slice-vector projections
  that continue to reference one portable XDMF/H5 field payload;
- project storage inventory plus preview-first cleanup that protects active
  runs, sole recovery checkpoints, and explicitly retained expert workspaces;
- a 50-sample, field-free runtime history used to calibrate `status`/`watch`
  ETA for comparable project setups without growing with solver time steps;
- separated baffled-channel XDMF frame selection and content-addressed rolling
  restart ZIPs, with trust/model/member verification before continuation;
- stable in-run checkpoint detection, bounded-memory ZIP streaming, and atomic
  replacement of the previous good recovery archive while `pimpleFoam` runs;
- constant-size checkpoint publication telemetry (count and first/latest time)
  rather than an event list that grows with a long transient solve.

The rolling publisher waits for two identical filesystem signatures before
accepting a newly written OpenFOAM time directory. It builds the next ZIP beside
the published archive, hashes the exact streamed bytes, writes the index, then
atomically replaces the previous good copy. A host failure can still lose work
after the most recently published checkpoint, but cannot expose a half-written
archive as the current recovery state on a filesystem that provides atomic
same-directory replacement. The next provider milestone is an in-situ
extraction adapter that avoids producing every selected VTU before consumption.
This remains an execution optimization, not a new user concept; existing
`case.py` files keep the same API.

The compact-monitoring direction follows OpenFOAM's function-object model,
which is explicitly intended to standardize batch post-processing while
avoiding unnecessary full-field retention:
<https://doc.openfoam.com/2212/tools/post-processing/function-objects/>.
