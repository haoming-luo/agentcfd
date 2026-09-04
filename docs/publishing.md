# Publishing and PyPI name status

## Published status

`agentcfd` 0.1.0a3 was published on 2026-09-04 from GitHub commit `6b5952f`
through PyPI Trusted Publishing after the Linux, macOS, Windows, Python
3.11--3.13, offline-wheel, and installed turbulent-case-generation gates
passed. A second clean environment then installed 0.1.0a3 from the public PyPI
index and reproduced its version, capability catalog, and k-omega SST case
manifest. The distribution name is occupied by the project, and
`haoming-luo/agentcfd` remains the canonical source repository. Release
artifacts are rebuilt by GitHub Actions, receive PyPI digital attestations, and
are therefore expected to have different hashes from any local pre-release
build.

The pending publisher converted to an active publisher after the first OIDC
upload. Its identity is GitHub owner `haoming-luo`, repository `agentcfd`,
workflow `release.yml`, and environment `pypi`; no long-lived PyPI upload token
is required by the release workflow.

## Release gate

1. Run the complete tests and lint checks.
2. Build the sdist and wheel in a clean environment.
3. Inspect both archives; require `LICENSE`, `NOTICE`, public docs, schemas, and
   no local paths, secrets, environments, generated cases, or solver binaries.
4. Install the exact wheel into a fresh environment.
5. Run `agentcfd doctor --json`, `agentcfd capabilities --json`, the reference
   pipe demo, and OpenFOAM case preparation from that environment.
6. Run `twine check` on both artifacts.
7. Confirm version agreement in `pyproject.toml`, `_version.py`, and
   `CITATION.cff`.
8. Recheck `https://pypi.org/pypi/agentcfd/json`.
9. Publish the exact audited artifacts using a scoped PyPI token or trusted
   publishing. Never upload a rebuild with the same version.

## Configured publishing path

The repository includes `.github/workflows/release.yml`. The active PyPI
publisher uses these exact values:

| Field | Value |
|---|---|
| PyPI project name | `agentcfd` |
| GitHub owner | `haoming-luo` |
| GitHub repository | `agentcfd` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

For future versions, publish a GitHub release for the matching version. The
workflow builds in a separate job, tests the exact wheel, passes only the
distributions to the minimal OIDC publish job, and publishes with attestations.
The `pypi` environment can add required-reviewer protection as the maintainer
team grows.

The first upload is intentionally an alpha release. PyPI publication reserves
the distribution name; it does not establish trademark rights or scientific
maturity.
