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
| NumPy | optional array hashing/interchange and NPZ bundles | permissive composite (BSD-3-Clause, 0BSD, MIT, Zlib, CC0) | optional `arrays`/`io` extra; audit bundled notices |
| h5py/HDF5 | optional binary scientific field storage | BSD-3-Clause plus HDF5 permissive license | optional `io` extra; retain bundled licenses |
| meshio | optional XDMF/HDF5 and mesh interchange | MIT | optional `io` extra; retain license |
| setuptools | build backend | MIT | source-build dependency only |
| wheel | wheel build | MIT | source-build dependency only |
| jsonschema | development-time schema validation | MIT | development dependency only |
| pytest | tests | MIT | development dependency only |
| Ruff | linting | MIT | development dependency only |
| build | release artifact construction | MIT | development dependency only |
| Twine | release artifact checking/upload | Apache-2.0 | development dependency only |

The core has no mandatory third-party runtime dependency and must not acquire a
mandatory solver, mesher, GUI, LLM SDK, or machine-learning framework
dependency. Optional integrations import lazily at their capability boundary.
The current runtime boundary is also available to automation through
`agentcfd licenses --json`.

## Planned and optional providers

| Component | Intended role | License posture | Integration rule |
|---|---|---|---|
| PyVista | interactive result inspection | MIT | optional visualization dependency |
| VTK | field and mesh visualization backend | BSD-3-Clause | transitive optional dependency through PyVista |
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
- Treat accessible benchmark data as link-only until dataset redistribution
  terms are reviewed; public access alone is not a redistribution license.

License references were reviewed on 2026-09-03 against installed PEP 639
metadata, upstream project license files, and official OpenFOAM licensing pages.
Composite packages retain their bundled license files; the table is not a
replacement for release SBOM/notice generation. The current review links
to the upstream [meshio](https://github.com/nschloe/meshio),
[PyVista](https://github.com/pyvista/pyvista),
[Cantera](https://github.com/Cantera/cantera/blob/main/License.txt),
[CoolProp](https://github.com/CoolProp/CoolProp), and
[CoolProp IF97](https://github.com/CoolProp/IF97) records. Pinning an
implementation version remains part of each provider's future release gate.
