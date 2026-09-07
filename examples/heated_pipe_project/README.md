# Heated-pipe project request

Create a readable constant-property thermal-flow project from this strict,
versioned automation boundary:

```bash
agentcfd init my-heated-pipe --request project-request.json
```

The parameter names match the generated `case.py` factory contract. Their SI
units are length (m), diameter (m), mean velocity (m/s), inlet temperature (K),
and signed wall heat flux into the fluid (W/m^2). Positive heat flux heats the
fluid and negative heat flux cools it. Zero is rejected because the released
thermal provider requires a prescribed non-zero heat flux.

After creation, the JSON request is no longer consulted. Inspect and edit the
generated `case.py`, then follow `agentcfd status my-heated-pipe`.
