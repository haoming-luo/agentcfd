"""A small passive cooling-water network; all values use SI units."""

from pathlib import Path

from agentcfd import fluids, zero_d


HERE = Path(__file__).resolve().parent

water = fluids.newtonian(
    "water",
    density=998.2,
    dynamic_viscosity=1.002e-3,
)

system = zero_d.network("cooling-water-split", fluid=water)
system.node("supply", pressure=300_000.0, elevation=0.0)
system.node("junction", elevation=2.0)
system.node("machine-a", volume_flow_source=-0.0010, elevation=4.0)
system.node("machine-b", volume_flow_source=-0.0007, elevation=7.0)
system.pipe(
    "header",
    "supply",
    "junction",
    length=12.0,
    diameter=0.065,
    roughness=1.0e-5,
    loss_coefficient=1.0,
)
system.pipe(
    "branch-a",
    "junction",
    "machine-a",
    length=8.0,
    diameter=0.04,
    roughness=1.0e-5,
    loss_coefficient=3.0,
)
system.pipe(
    "branch-b",
    "junction",
    "machine-b",
    length=14.0,
    diameter=0.04,
    roughness=1.0e-5,
    loss_coefficient=4.0,
)

result = system.solve().require_accepted()
system.write_json(HERE / "results" / "zero_d_network.json")
result.write_json(HERE / "results" / "zero_d_result.json")

print(f"accepted: {result.accepted}")
print(f"junction pressure: {result.pressure('junction'):.3f} Pa")
for branch_name in ("header", "branch-a", "branch-b"):
    print(
        f"{branch_name}: {result.volume_flow_rate(branch_name):.9f} m^3/s"
    )

