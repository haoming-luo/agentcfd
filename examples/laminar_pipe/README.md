# Laminar circular pipe

This first executable workflow validates AgentCFD's public model, result,
applicability, and provenance lifecycle against the Hagen–Poiseuille solution.

```bash
python case.py
```

It is intentionally a reference calculation rather than a mesh-based CFD
claim. The next numerical milestone will preserve this case as an independent
acceptance oracle.
Run `case.py` for the accepted analytical Hagen--Poiseuille workflow.

Run `grid_convergence.py` for a compact three-grid Richardson/GCI example.
The values in that example are demonstrative; replace them with the same
recovered quantity of interest from geometrically similar CFD grids.
