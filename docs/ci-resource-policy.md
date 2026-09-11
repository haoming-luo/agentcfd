# CI resource policy

AgentCFD treats compute time, runner energy, developer attention, and hosted
quota as engineering resources. Public-runner pricing does not make redundant
work acceptable. These rules apply to humans and coding agents.

## Incident baseline and objective

The 2026-09-11 billing audit identified AgentCFD as the dominant avoidable
hosted-compute consumer: 103 workflow runs and USD 22.79 of gross usage before
free allowance, about 78% of the gross usage among the three audited private
projects. The historical workload included 99 push-test runs and four release
runs. The principal multiplier was frequent small pushes combined with a full
3 OS × 3 Python matrix; macOS alone represented most of the gross private-runner
cost. These figures are an incident baseline, not money already charged.

This policy is the corrective control. The initial operating target is a
70–90% reduction in avoidable hosted usage at comparable development activity,
without weakening release compatibility claims. Making the repository public
can change billing, but does not close the incident: energy, queue capacity,
latency, signal quality, and maintainer attention remain finite.

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
4. Markdown-only documentation, license, and notice changes do not start code
   CI. Executable examples, JSON contracts/evidence (including files below
   `docs/`), package metadata, dependency files, and workflow files are not
   classified as documentation and must still run the fast gate.
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
10. Before pushing, classify the batch as documentation-only, ordinary,
    platform-sensitive, or release and select exactly the corresponding tier
    from the trigger map. Do not create empty commits, cosmetic follow-up
    commits, or partial pushes merely to obtain a new hosted status.
11. macOS and Windows runners are scarce acceptance resources. They are never
    part of ordinary push or pull-request validation, even when a public-repo
    billing plan currently prices those runs at zero to the project.
12. Treat a hosted failure by cause. Fix and repush a reproducible code failure
    only after the corrected local gate passes. For billing, quota, unavailable
    runner, or service-infrastructure failures, make zero rerun attempts until
    the external state is known to have changed.
13. Hosted CI is an acceptance signal, not an interactive debugger. During an
    ordinary feature increment, perform one coherent push, allow one automatic
    fast-gate run; the workflow deliberately has no manual dispatch trigger.
    Inspect its status at most once during routine development; repeated polling
    does not improve evidence and must not stall useful local work.
14. Cross-platform acceptance and release workflows require an actual stage
    boundary, a platform/dependency/packaging risk, or an explicit human request.
    A coding agent may not invoke them merely because they are available or free.
15. Do not push just because a timer, conversational turn, or work session ends.
    Push only a coherent, locally validated, reviewable feature batch. Several
    traceable local commits may be delivered in one remote push.

## Enforced workflow invariants

`tests/test_ci_resource_policy.py` guards the minimum resource contract:

- the fast workflow has one Ubuntu job, no matrix, documentation path filters,
  concurrency cancellation, bounded runtime, one build, and one wheel smoke;
- cross-platform acceptance has no automatic push or pull-request trigger and
  contains only the approved five-combination manual matrix;
- release builds the distribution once, retains it briefly, tests that exact
  artifact on the declared 3 OS × 3 Python matrix, and publishes only after
  acceptance succeeds.

Workflow changes must pass this guard locally. Weakening the guard or expanding
a hosted matrix requires explicit human approval and a written reason. A free
runner allowance changes the bill, not this engineering discipline.

## Trigger map

| Change or event | Required remote evidence |
|---|---|
| Ordinary code/schema/example/workflow push | One Linux fast test |
| Markdown/license/notice-only push | No code workflow |
| Stage gate or platform-sensitive change | One manual five-combination acceptance |
| Published release | One build, full 3×3 wheel acceptance, then PyPI publish |
| Billing or runner blockage | No retries; local evidence plus pending-gate record |

The target is a 70–90% reduction in avoidable hosted usage relative to the
former per-push 3×3 matrix, without weakening release claims.

## Per-increment execution budget

| Activity | Hosted budget | Authority |
|---|---:|---|
| Focused development checks | 0 jobs | Local only |
| Ordinary feature handoff | 1 automatic Linux workflow | Default after the local gate |
| Documentation-only handoff | 0 jobs | Default |
| Corrected reproducible code failure | 1 new automatic Linux workflow | Only after local reproduction and correction |
| Billing/quota/infrastructure failure | 0 reruns | Wait for external state change |
| Cross-platform stage acceptance | 1 five-combination workflow per SHA | Stage/platform risk or explicit human request |
| Release | 1 build and one 3×3 acceptance of that same artifact | Explicit release only |

The limits are per coherent increment and per commit SHA. They must not be reset
by renaming a workflow, making a cosmetic commit, opening a duplicate branch, or
moving from CLI to the web UI.

## Required handoff record

Every pushed feature handoff states: the change classification, local commands
that passed, the single expected remote workflow, and any remote gate still
pending or blocked. This keeps scientific and packaging claims auditable without
manufacturing extra runs solely to document earlier runs.

Use this minimum record:

```text
Change class: documentation-only | ordinary | platform-sensitive | release
Local gate: <exact commands and result>
Remote budget: <workflow name or none; expected job count>
Remote evidence: <run URL and passed/pending/blocked, or not applicable>
Retry count: 0
```

At a stage or release boundary, compare workflow count, cancelled/superseded
runs, infrastructure reruns, and platform-job mix with the incident baseline.
Do not launch a workflow merely to measure the workflow system.
