# Bottom-baffle wake project

This project is the first executable non-pipe workflow: a short, thick channel
with a bottom-attached baffle, physical-time field frames, and compact probes,
surface reports, and forces.  Its deliberately viscous fluid keeps the declared
laminar model below the provider's Reynolds-number applicability limit.

```bash
agentcfd plan examples/channel_baffle_project --json
agentcfd run examples/channel_baffle_project --json
agentcfd inspect examples/channel_baffle_project --json
```

The generated OpenFOAM workspace is disposable implementation detail.  The
project publishes a compact result record, evidence logs, and XDMF/H5 fields in
`output/`; replace mode safely replaces only an AgentCFD-owned prior result.
