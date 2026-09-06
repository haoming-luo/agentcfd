"""Imported closed duct: mesh workflow example, not yet a flow-solver claim."""

import json
from pathlib import Path

from agentcfd import Model, boundaries, fluids, geometry, meshing, studies


def build(*, base_size=0.05):
    root = Path(__file__).parent
    inspection = json.loads((root / "geometry/inspection.json").read_text())
    domain = geometry.imported_surface_from_inspection(
        inspection,
        asset="geometry/fluid.stl",
        interior_point_m=(0.5, 0.25, 0.1),
    )
    model = Model(
        name="imported-rectangular-duct",
        study=studies.internal_flow(),
        domain=domain,
        fluid=fluids.newtonian(
            "water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.5),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
    )
    return model.step(
        mesh=meshing.automatic(
            base_size=base_size,
            local_sizing=(meshing.refine("inlet", "outlet", size=base_size / 2),),
            maximum_cells=200_000,
        )
    )
