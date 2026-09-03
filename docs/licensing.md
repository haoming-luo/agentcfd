# Dependency and license policy

AgentCFD core is Apache-2.0. Mandatory Python dependencies stay permissively
licensed. Copyleft numerical engines are optional and remain across explicit
process, file, or dynamic-library boundaries. This inventory is engineering
policy, not legal advice; redistribution still receives a release-specific
review.

## Current core

| Component | Role | License | Distribution rule |
|---|---|---|---|
| AgentCFD | workflow and scientific contracts | Apache-2.0 | core |
| NumPy | arrays and reference profiles | BSD-3-Clause | only runtime dependency |
| setuptools | build backend | MIT | source-build dependency only |
| wheel | wheel build | MIT | source-build dependency only |

The core must not acquire a mandatory solver, mesher, GUI, LLM SDK, or machine-
learning framework dependency. Optional integrations import lazily at their
capability boundary.

## Planned and optional providers

| Component | Intended role | License posture | Integration rule |
|---|---|---|---|
| meshio | mesh interchange | MIT | optional Python dependency |
| Kratos Multiphysics | candidate CFD/CHT engine | BSD-4-Clause core; applications can differ | optional provider; audit every selected application |
| Cantera | thermochemistry | BSD-3-Clause | optional chemistry provider |
| CoolProp | fluid and steam properties, including IF97 | MIT | optional property provider |
| CoolProp IF97 | focused water/steam implementation | permissive MIT-style license | optional property provider |
| OpenFOAM | primary industrial finite-volume engine | GPL-3.0 | user-managed external executable; exchange only case files, logs, and results |
| SU2 | compressible flow and optimization | LGPL-2.1 | optional external provider; audit redistribution |
| preCICE | partitioned coupling | LGPL-3.0 | optional coupling provider; audit linking and redistribution |
| Gmsh | meshing | GPL | prefer user-managed external executable; Python/API distribution requires a separate review |

## Hard boundaries

- Do not copy OpenFOAM, Gmsh, SU2, or preCICE source into AgentCFD core.
- Do not import OpenFOAM shared libraries or compile AgentCFD extensions against
  them. Generate ordinary case files and invoke user-installed commands.
- Do not bundle a solver runtime in PyPI wheels. A future container, desktop
  runtime, or commercial distribution gets its own license inventory, notices,
  corresponding-source plan, and legal review.
- Record provider name, version, license posture, command boundary, and exact
  generated-case fingerprint in result provenance.
- Audit transitive dependencies and included data files, not only top-level
  package metadata, before every release.

License references were reviewed on 2026-09-03 against the upstream project
license files and official OpenFOAM licensing pages. Pinning an implementation
version remains part of each provider's future release gate.
