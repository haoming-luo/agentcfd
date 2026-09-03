# Benchmark catalog

AgentCFD follows a building-block path: exact unit problems first, then
benchmark flows, industrial components, and only then coupled systems. A case
is not called validated merely because a solver tutorial exists.

| Stage | Case | Primary observables | Evidence target | Status |
|---|---|---|---|---|
| Unit | Fully developed circular pipe | flow, pressure gradient, wall shear | Hagen--Poiseuille plus three-grid GCI | active |
| Unit | Uniform-inlet developing pipe | entrance length, section pressure gradient, mass balance | grid study with a declared entrance policy | planned |
| Unit | Lid-driven cavity | centreline velocity and vortex location | trusted numerical reference and grid study | planned |
| Benchmark | Pitz--Daily backward-facing step | reattachment length, wall pressure, velocity profiles | experiment with turbulence-model sensitivity | planned |
| Benchmark | Laminar cylinder, Re 150 | drag, lift amplitude, Strouhal number | experimental comparison and temporal/spatial studies | planned |
| Benchmark | FDA benchmark nozzle | pressure, velocity, wall shear | FDA experimental dataset plus spatial/model studies | planned |
| Component | Conical diffuser | pressure recovery, separation | experimental data and model sensitivity | planned |
| Component | Bend, tee, and manifold | loss coefficient, branch balance, uniformity | published data plus conservation | planned |
| Thermal | Heated pipe / conjugate wall | bulk temperature, Nusselt number, heat balance | analytical correlation and mesh/time studies | planned |
| Steam | Single-phase steam pipe | pressure and enthalpy loss | property uncertainty plus experiment | planned |
| Thermal mixing | IAEA tee junction | temperature mean, fluctuations, wall response | IAEA benchmark plus temporal/model studies | planned |
| Reacting | Sandia TNF non-premixed flame | mixture fraction, temperature, species, velocity | workshop measurements plus model sensitivity | planned |
| Reacting multiphase | NIST spray flame | droplet size/velocity/flux and gas temperature | NIST benchmark database | planned |

The [NASA NPARC validation archive](https://www.grc.nasa.gov/WWW/wind/valid/archive.html)
provides documented cavity, backward-facing-step, diffuser, cylinder, and duct
cases. Its
[documentation guidelines](https://www.grc.nasa.gov/WWW/wind/valid/document.html)
motivate AgentCFD's requirement to retain grids, inputs, convergence histories,
runtime details, and comparison data. The official
[OpenFOAM v2606 quickstart](https://doc.openfoam.com/2606/quickstart/) supplies
the Pitz--Daily setup as an implementation cross-check, not as experimental
truth.

The US FDA also publishes a
[benchmark nozzle dataset](https://www.origin-cdrh-rst.fda.gov/benchmark-dataset-validating-computational-fluid-dynamic-cfd-simulation-blood-flow-through)
with geometry, flow conditions, properties, pressure, and velocity validation
data. Its nozzle is relevant beyond medical devices because it exercises an
industrial sequence of contraction, jet, expansion, and recirculation.

For thermal piping, the
[IAEA tee-junction benchmark](https://www-pub.iaea.org/MTCD/Publications/PDF/te_1318_web.pdf)
adds transient temperature mixing and wall loading. Combustion follows a staged
path: the [Sandia turbulent-flame program](https://www.sandia.gov/research/publications/details/thirteenth-international-workshop-on-measurement-and-computation-of-turbule-2016-12-01/)
for canonical gas flames, then the
[NIST spray-flame database](https://www.nist.gov/publications/benchmark-database-input-and-validation-multiphase-combustion-models)
for multiphase coupling. These are roadmap evidence sources, not claims of
current solver support.

The same roadmap is available to agents and automation as
`agentcfd benchmarks --json`. Each record carries a stable identifier,
physics, observables, authoritative source, implementation status, and the next
evidence gate. Every record defaults to
`link-only-pending-terms-review`: source data are linked, not copied, until
redistribution terms have been reviewed and recorded.
