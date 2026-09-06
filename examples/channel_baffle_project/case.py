"""Executable low-Re wall-baffle wake project."""

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


def build(*, mean_velocity=0.5, baffle_height=0.12):
    channel = geometry.rectangular_channel(
        length=1.2,
        height=0.20,
        width=0.10,
    ).with_baffle(
        name="baffle",
        x=0.35,
        height=baffle_height,
        thickness=0.01,
        attached_to="bottom",
    )
    model = Model(
        name="bottom-baffle-wake",
        study=studies.internal_flow(steady=False),
        domain=channel,
        fluid=fluids.newtonian(
            "viscous-liquid",
            density=1000.0,
            dynamic_viscosity=0.05,
        ),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(mean_velocity),
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
        initialization=initialization.potential_flow(maximum_iterations=500),
        mesh=meshing.structured(base_size=0.01),
        output=outputs.animation(
            every=0.01,
            maximum_frames=201,
            storage_budget="2 GiB",
            restart=outputs.checkpoints(every=0.5, keep=2),
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
            views=(
                outputs.slice_view(
                    "midplane-vorticity",
                    field="fluid.vorticity",
                    origin=(0.6, 0.1, 0.05),
                    normal=(0.0, 0.0, 1.0),
                ),
                outputs.streamline_view(
                    "wake-streamlines",
                    seed_start=(0.02, 0.01, 0.05),
                    seed_end=(0.02, 0.19, 0.05),
                    seeds=40,
                ),
                outputs.line_profile(
                    "centerline-pressure",
                    field="fluid.pressure",
                    start=(0.05, 0.10, 0.05),
                    end=(1.15, 0.10, 0.05),
                    samples=121,
                ),
            ),
        ),
    )
