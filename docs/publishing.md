# Publishing and PyPI name status

## Name check

On 2026-09-03 the official PyPI JSON endpoint for `agentcfd` returned HTTP 404,
so no public PyPI project existed at that moment. The GitHub repository
`haoming-luo/agentcfd` exists and is the canonical source repository.

A 404 is a point-in-time observation, not a reservation. Only a successful
upload creates the PyPI project. Recheck immediately before publishing.

## First-release gate

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

## Recommended first upload: trusted publishing

The repository includes `.github/workflows/release.yml`. In the PyPI account's
Publishing page, add a pending GitHub publisher with these exact values:

| Field | Value |
|---|---|
| PyPI project name | `agentcfd` |
| GitHub owner | `haoming-luo` |
| GitHub repository | `agentcfd` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

Protect the GitHub `pypi` environment with a required reviewer, then publish a
GitHub release for the matching version. The workflow builds in a separate job,
tests the exact wheel, passes only the distributions to the minimal OIDC publish
job, and publishes with attestations. A pending publisher does not reserve the
name until that first workflow upload succeeds.

The first upload should remain an alpha release. PyPI publication reserves the
distribution name; it does not establish trademark rights or scientific
maturity.
