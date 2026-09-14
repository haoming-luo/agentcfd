# 0D cooling-water split

This readable `case.py` solves a supply header feeding two elevated machine
loads. It writes only two compact JSON files under `results/`, replacing the
previous result on the next run. No OpenFOAM case, mesh, or time directories are
created.

```bash
python case.py
```

The example demonstrates prescribed pressure, demanded volume flow, elevation
head, pipe roughness, minor losses, parallel flow, acceptance checks, and named
result access.

