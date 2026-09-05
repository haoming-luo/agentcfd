# Bottom-baffle wake project

This project demonstrates the intended common-workflow API for a short, thick
channel with a wall-attached baffle, fine physical-time frames, compact probes,
surface reports, local refinement, and boundary layers.

```bash
agentcfd check examples/channel_baffle_project --json
agentcfd plan examples/channel_baffle_project --json
```

The public model is valid. The current circular-pipe OpenFOAM provider must
report `PROVIDER_INCOMPATIBLE`: unstructured channel/baffle mesh and transient
report lowering are explicit development gates, not silently ignored options.
