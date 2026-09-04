# AgentCFD Roadmap

## Product direction

AgentCFD will become a dependable and unusually usable open-source CFD platform
for selected industrial analyses. It will not begin by chasing every advanced
aerospace capability. The first territory is where industrial engineers most
often need repeatable decisions: pipes, ducts, flow distribution, pressure
loss, heat transfer, steam and gas systems, and combustion equipment.

The public workflow is the product:

```text
Study -> Model -> Geometry/Mesh -> Regions -> Fluids
      -> Boundaries/Sources -> Step -> SimulationResult
```

## P0 — trustworthy foundation

Current product checkpoint: the first shared project lifecycle now creates,
checks, plans, executes, and inspects the same readable `case.py` for people and
agents. The first standard field bundle publishes XDMF/H5 for visualization and
AgentFEM exchange plus pickle-free NPZ for learned workflows, with canonical
names, units, association, axis semantics, source identity, and hashes. These
are the product backbone; numerical benchmarks remain evidence underneath it.

- stabilize the public engineering vocabulary and versioned model/result schemas;
- release mesh import, named regions, boundary identity, and mesh-quality audits;
- lower one bounded incompressible laminar workflow to a real numerical provider;
- support velocity, mass-flow, pressure, wall, symmetry, periodic, and body-source conditions;
- standardize velocity, pressure, mass balance, forces, pressure loss, and wall quantities;
- add residual, conservation, runtime provenance, restart, and compact visualization contracts;
- validate with Poiseuille flow, manufactured solutions, lid-driven cavity, and cylinder flow;
- package and test on Linux, macOS, and Windows through WSL2.

## P1 — industrial internal flow

Current numerical checkpoint: OpenCFD v2606 laminar and two-equation RANS pipe
routes, developed-flow precursors, mapping, wall treatment, and a four-point
Reynolds/model matrix are executable and content addressed. The evidence rejects
a universal turbulence-model default and remains bounded to its declared pipe
cases. Detailed results live in `docs/numerical-strategy.md`; product development
now prioritizes end-to-end industrial geometry and decision workflows over
additional smooth-pipe tuning.

- follow the inlet, resolution, turbulence, and steam promotion sequence in
  `docs/numerical-strategy.md`;
- steady and transient incompressible flow;
- RANS workflows with k-epsilon and k-omega SST, wall treatment, and y-plus evidence;
- bends, tees, manifolds, valves, porous losses, fans, pumps, and rotating zones;
- conjugate heat transfer and buoyant flow;
- force, moment, pressure-loss, flow-uniformity, and section-balance histories;
- mesh and discretization convergence certificates;
- parameter campaigns and operating maps.

## P2 — steam, gases, and heat

- compressible pressure- and density-based procedures;
- temperature-dependent transport and thermodynamic properties;
- IAPWS-IF97/CoolProp-compatible steam property providers behind optional boundaries;
- nozzles, throttling, compressible pipe networks, heat exchangers, and thermal equipment;
- energy, enthalpy, heat-flux, and entropy-generation audits;
- conjugate heat transfer with AgentFEM thermal and structural models.

## P3 — reacting flow and combustion

- species transport, mixtures, reaction mechanisms, and chemistry provenance;
- optional Cantera-backed thermochemistry;
- premixed and non-premixed reference flames before turbulent combustion;
- radiation, heat release, ignition, extinction, and emissions observables;
- Sandia flames and other public validation data with frozen parameters;
- strict separation among chemistry, turbulence–chemistry interaction, and numerical evidence.

## P4 — multiphysics and learned computation

- conservative AgentCFD–AgentFEM pressure, traction, temperature, heat-flux, and mesh-motion exchange;
- partitioned FSI and conjugate heat transfer, optionally through preCICE;
- multiphase, boiling, condensation, cavitation, and particle transport after single-phase maturity;
- common campaigns and scientific datasets across simulations and experiments;
- user-owned PyTorch/JAX models, surrogates, PINNs, and neural operators through neutral protocols;
- active learning, calibration, inverse problems, optimization, and high-fidelity fallback.

## Evidence ladder

Every serious capability advances through:

1. formula and assumptions;
2. local or manufactured verification;
3. global numerical integration and failure handling;
4. engineering benchmark and convergence evidence;
5. installed-artifact, platform, MPI, and documentation gates.

A public name never implies the highest evidence level.
