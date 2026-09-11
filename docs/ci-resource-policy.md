# CI resource policy

AgentCFD treats compute time, runner energy, developer attention, and hosted
quota as engineering resources. Public-runner pricing does not make redundant
work acceptable. These rules apply to humans and coding agents.

## Mandatory development behavior

1. Validate every increment locally. Run focused tests while editing and the
   full lightweight suite plus lint at a coherent feature boundary. Scientific
   solves, grid studies, and performance campaigns stay on local or explicitly
   authorized compute resources.
2. Commit in traceable units, but batch remote pushes into a reviewable feature
   increment. Do not push every tiny edit merely to obtain another CI signal.
3. Ordinary pushes and pull requests use exactly one Linux fast-test job. It
   runs lint, the complete lightweight test suite, one package build, and one
   installed-wheel smoke. Superseded runs are cancelled automatically.
4. Markdown-only, `docs/`, license, and notice changes do not start code CI.
   Executable examples, JSON contracts/evidence, package metadata, dependency
   files, and workflow files are not classified as documentation and must still
   run the fast gate.
5. The five-combination cross-platform acceptance workflow is manual. Run it
   only for a stage gate, meaningful platform/dependency/packaging change, or
   explicit human request. Never trigger it repeatedly for the same commit.
6. A release builds one wheel/sdist, runs the dependency-free wheel smoke, then
   tests that same wheel across the complete declared 3 OS × 3 Python matrix.
   PyPI publication depends on every release-platform job; it never rebuilds a
   different artifact after verification.
7. Cache Python downloads, bound job time, and retain transient artifacts for
   only three days. Do not split short commands into extra jobs without a
   measured latency or reliability benefit.
8. If GitHub reports billing, spending-limit, runner-infrastructure, or quota
   blockage, stop retrying immediately. Continue local work, record the exact
   unexecuted remote gate, and wait for an external-state change or explicit
   authorization. Local success must not be described as remote success.
9. A documentation record cites the already-tested code SHA and run URL. Never
   create a validation loop in which writing down a green result launches the
   same expensive validation again.

## Trigger map

| Change or event | Required remote evidence |
|---|---|
| Ordinary code/schema/example/workflow push | One Linux fast test |
| Markdown/docs/license-only push | No code workflow |
| Stage gate or platform-sensitive change | One manual five-combination acceptance |
| Published release | One build, full 3×3 wheel acceptance, then PyPI publish |
| Billing or runner blockage | No retries; local evidence plus pending-gate record |

The target is a 70–90% reduction in avoidable hosted usage relative to the
former per-push 3×3 matrix, without weakening release claims.
