from agentcfd import Model, boundaries, fluids, geometry, outputs, procedures, studies


model = Model(
    name="laminar-water-pipe",
    study=studies.internal_flow(),
    domain=geometry.circular_pipe(length=10.0, diameter=0.05),
    fluid=fluids.newtonian(
        "water",
        density=998.2,
        dynamic_viscosity=1.002e-3,
    ),
).boundaries(
    inlet=boundaries.mean_velocity_inlet(0.02),
    outlet=boundaries.pressure_outlet(),
    wall=boundaries.no_slip_wall(),
)

result = model.step(
    procedure=procedures.steady(),
    output=outputs.standard(),
).run(provider="reference")

result.require_accepted()
result.write("result.json")
print(result.quantities["flow.pressure_drop"])
