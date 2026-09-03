# Thermophysical properties

AgentCFD keeps property evaluation separate from CFD solution. The optional
MIT-licensed CoolProp provider returns one transparent SI state from absolute
pressure and temperature:

```python
from agentcfd.properties import CoolPropPropertyProvider

state = CoolPropPropertyProvider().at_pressure_temperature(
    "IF97::Water",
    pressure=101325.0,
    temperature=500.0,
)
print(state.to_dict())
```

The record includes phase, density, dynamic viscosity, specific heat, thermal
conductivity, speed of sound, Prandtl number, backend, and provider version.
CoolProp documents the `PropsSI` pressure-temperature interface and the IF97
water/steam backend at:

- <https://coolprop.org/coolprop/HighLevelAPI.html>
- <https://coolprop.org/fluid_properties/IF97.html>

This boundary deliberately does not turn a property point into a CFD claim.
Near saturation, in the two-phase region, or outside IF97's validity region,
the user must select appropriate phase physics and a validated flow solver.
AgentCFD surfaces provider errors instead of falling back to ideal gas or
constant properties.

The first installed-runtime evidence record is
[`coolprop-if97-validation.json`](coolprop-if97-validation.json). CoolProp
8.0.0 evaluated superheated water at 101325 Pa and 500 K; the recovered density
and specific heat match the upstream IF97 examples, while the complete positive
transport state is retained for regression and provenance.

For preliminary gas screening, `agentcfd.engineering` also exposes the ideal-
gas density, calorically perfect-gas speed of sound, and Mach number. NASA's
compressible-flow relations describe the corresponding assumptions:
<https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/mass-flow-rate-equations/>.
These functions must not be used as an undocumented steam-property substitute.
