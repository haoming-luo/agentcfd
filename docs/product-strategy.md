# Product and market strategy

## Positioning

AgentCFD is an AI-native industrial CFD workflow, not an LLM wrapper around a
solver and not an attempt to replace every Fluent capability at launch. It uses
OpenFOAM as the primary industrial engine while making model intent, capability
limits, generated cases, checks, and results readable to both engineers and
agents.

The initial promise is narrow and valuable:

> Turn a common internal-flow engineering question into an inspectable,
> repeatable, checked CFD result without requiring the user to become an
> OpenFOAM dictionary specialist.

## Beachhead users and problems

The first users are process, energy, building-services, equipment, and general
mechanical engineers in small and mid-sized teams, plus CAE consultants and
automation groups. Their early problems include:

- pipe and duct pressure loss;
- bends, tees, manifolds, valves, fans, pumps, and porous equipment;
- flow distribution and section uniformity;
- water, air, gas, and later steam transport;
- conjugate heat transfer and thermal loads passed to AgentFEM;
- repeatable operating maps rather than one-off visual demonstrations.

Education and advanced aerospace remain useful secondary markets, but they do
not set the first product backlog.

## Differentiation

Commercial CFD products lead in breadth, integrated preprocessing, support,
and long industrial validation histories. Raw OpenFOAM leads in openness and
solver depth but asks users to manage many coupled files and expert decisions.
Recent CFD agents emphasize natural-language case generation and error repair.

AgentCFD should win a different layer:

- one stable engineering Python and JSON language across people, agents, CLI,
  and future GUI clients;
- explicit provider capability and fail-closed lowering;
- acceptance based on physics and conservation, not process success;
- content-addressed cases and results suitable for audit and automation;
- an industrial internal-flow template and benchmark library;
- direct semantic continuity with AgentFEM for pressure, traction, temperature,
  heat flux, and later mesh motion;
- provider-neutral datasets, surrogates, neural operators, and high-fidelity
  fallback after simulation maturity.

The durable moat is the verified engineering workflow and evidence corpus, not
prompt text or a thin chat interface.

## Open-source and commercial posture

The public core stays Apache-2.0 with permissive mandatory dependencies. Users
bring their own OpenFOAM runtime across a visible GPL process/file boundary.
Future revenue can come from support, verified runtimes, private domain packs,
enterprise deployment, collaborative workflows, and validated industrial
extensions. Private assets remain separate packages and must not weaken the
public capability truth.

## Go-to-market sequence

1. Reserve `agentcfd` on PyPI with an honest alpha and publish the GitHub source.
2. Make the circular-pipe reference and generated OpenFOAM case a five-minute,
   bilingual onboarding path.
3. Promote the OpenFOAM provider only after actual mass balance, pressure-loss
   recovery, and mesh convergence pass on supported platforms.
4. Publish reproducible industrial benchmarks for ducts, bends, tees, and
   manifolds before adding broad solver menus.
5. Grow adoption through executable examples, engineering notes, AgentFEM
   coupling demonstrations, and integrations that consume stable JSON.
6. Add RANS, wall treatment, heat transfer, steam properties, and combustion in
   evidence-gated vertical slices.

## Product metrics

Track metrics that resist demo inflation:

- median time from stated problem to first accepted result;
- physically accepted rate, reported separately from execution success;
- proportion of failures carrying an addressable code and repair hint;
- mass/energy imbalance and benchmark error distributions;
- repeatability across provider versions and supported platforms;
- number of real industrial workflows with mesh-convergence evidence;
- returning projects, campaign reuse, and successful AgentFEM handoffs.

Downloads, stars, and generated cases are useful distribution signals but are
not evidence of engineering value.

## Near-term non-goals

- claiming Fluent-equivalent breadth;
- leading with high-Mach aerospace or every turbulence model;
- making an LLM a mandatory runtime dependency;
- hiding OpenFOAM assumptions behind a chat-only interface;
- accepting a result because a solver exited normally;
- bundling copyleft engines into the Apache-2.0 PyPI wheel;
- extracting a shared AgentCAE core before AgentCFD and AgentFEM prove the same
  implementation contract independently.
