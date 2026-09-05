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

## Planning before solving

`agentcfd plan` resolves the requested field count and publishes:

- each output channel and its retention rule;
- estimated mesh cells when the provider mesh is predictable;
- estimated final portable bytes;
- estimated temporary peak bytes;
- compression and budget decisions;
- an addressable `OUTPUT_POLICY_INVALID` or `OUTPUT_BUDGET_EXCEEDED` issue.

The temporary estimate is deliberately conservative. The current external
OpenFOAM adapter may stage native and VTK fields while publishing HDF5. Future
streaming and in-situ adapters can lower that amplification without changing
the public output contract.

## Portable storage

XDMF remains the lightweight index and HDF5 the field payload. The exporter:

1. reads the first fixed-mesh frame and determines the exact selected arrays;
2. estimates geometry, topology, all frames, and optional NPZ duplication;
3. refuses the export before creating its destination when over budget;
4. writes the time series;
5. repacks numeric datasets with chunked `gzip` or `lzf` compression;
6. records estimated and actual byte counts in `manifest.json`.

HDF5 compression requires chunked datasets, and chunk selection affects I/O
performance. The implementation uses bounded-memory copies and a portable gzip
level rather than loading the complete time series into memory. See the h5py
dataset documentation: <https://docs.h5py.org/en/stable/high/dataset.html>.

NPZ remains opt-in. It is useful for explicit array/learning workflows, but it
duplicates the portable field payload and is not required for ParaView or
AgentFEM's XDMF/H5 path.

## Current and next implementation boundary

Implemented now:

- typed frame, checkpoint, compression, and budget policies;
- transient procedure intent with study/procedure consistency checks;
- plan-time frame and peak-storage guards;
- export-time selected-array budget enforcement;
- chunked HDF5 compression and storage provenance;
- binary OpenFOAM native output for generated cases;
- separated baffled-channel XDMF frame selection and content-addressed rolling
  restart ZIPs, with trust/model/member verification before continuation.

The current restart ZIP is published after a successful solve and retains only
the declared final `keep` checkpoints. It supports controlled continuation but
does not claim crash-safe mid-run archival. Next provider milestones are
streaming frame conversion, crash-safe checkpoint publication, and then an
in-situ extraction adapter. Those are execution optimizations, not new user
concepts; existing `case.py` files keep the same API.
