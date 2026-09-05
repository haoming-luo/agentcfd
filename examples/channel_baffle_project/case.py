"""Readable wake-flow intent; backend lowering is intentionally still gated."""

from agentcfd import (
    Model,
    boundaries,
    fluids,
    geometry,
    initialization,
    meshing,
    outputs,
    procedures,
    studies,
)


def build():
    channel = geometry.rectangular_channel(
        length=1.2,
        height=0.20,
        width=0.10,
    ).with_baffle(
        name="baffle",
        x=0.35,
        height=0.12,
        thickness=0.01,
        attached_to="bottom",
    )
    model = Model(
        name="bottom-baffle-wake",
        study=studies.internal_flow(steady=False),
        domain=channel,
        fluid=fluids.newtonian(
            "water",
            density=998.2,
            dynamic_viscosity=1.002e-3,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.5),
        outlet=boundaries.pressure_outlet(),
        walls=boundaries.no_slip_wall(),
        baffle=boundaries.no_slip_wall(),
    )
    return model.step(
        procedure=procedures.transient(
            end_time=2.0,
            initial_time_step=0.001,
            maximum_time_step=0.005,
            maximum_courant_number=0.5,
        ),
        initialization=initialization.potential_flow(),
        mesh=meshing.automatic(
            base_size=0.02,
            local_sizing=(meshing.refine("baffle", size=0.005),),
            boundary_layers=(
                meshing.layers(
                    "walls",
                    "baffle",
                    count=5,
                    first_height=0.0008,
                    growth_rate=1.2,
                ),
            ),
        ),
        output=outputs.animation(
            every=0.01,
            maximum_frames=201,
            restart=outputs.checkpoints(every=0.25, keep=2),
            storage_budget="1 GiB",
            reports=(
                outputs.probe(
                    "near-wake",
                    at=(0.50, 0.05, 0.05),
                    fields=("fluid.velocity", "fluid.pressure"),
                ),
                outputs.surface_report(
                    "outlet-pressure",
                    region="outlet",
                    field="fluid.pressure",
                    operation="area-average",
                ),
                outputs.force_report(
                    "baffle-drag",
                    regions=("baffle",),
                    direction=(1.0, 0.0, 0.0),
                ),
            ),
        ),
    )
