# AgentCFD contributor instructions

Read `CONCEPTS.md`, `WORKFLOW.md`, `AGENT_GUIDE.md`, `ROADMAP.md`, and
`docs/validation.md` before changing a scientific capability. Keep the public
model backend-neutral, fail closed on unsupported physics, and run tests plus
the installed-wheel smoke check before claiming completion.

Do not modify AgentFEM from this repository. Cross-product work belongs in the
versioned interoperability contract until a genuinely shared implementation has
been proven in both products.

## Mandatory CI resource discipline

Read and follow `docs/ci-resource-policy.md` before any commit, push, workflow
dispatch, rerun, or release. In particular: validate locally first; batch pushes
by coherent feature; ordinary remote validation is one Linux job; documentation
must not launch code CI; cross-platform acceptance is manual or release-only;
and billing/quota/infrastructure failures must never be retried until external
state changes. Never report local evidence as remote evidence.

Before every push, classify the change as documentation-only, ordinary,
platform-sensitive, or release. Record the matching validation tier in the
development handoff. A coding agent must not use empty commits, cosmetic edits,
workflow reruns, or fragmented pushes to probe hosted CI. macOS and Windows
runners are prohibited for ordinary push and pull-request validation. Any
change to `.github/workflows/` must keep `tests/test_ci_resource_policy.py`
passing; changing or weakening that test requires explicit human approval.

The hosted-execution budget is a hard agent contract, not a suggestion:

- one coherent ordinary feature batch permits one push and therefore one
  automatic Linux fast gate; it does not permit a manual workflow dispatch;
- run focused checks during implementation, then one complete local gate before
  pushing; hosted CI verifies the batch and must not be used as a debugging loop;
- inspect the resulting hosted run at most once during normal development and
  keep working locally instead of repeatedly polling it;
- make zero rerun attempts for billing, quota, unavailable-runner, or GitHub
  infrastructure failures; a reproducible code failure may receive one corrected
  push only after the correction passes locally;
- dispatch cross-platform acceptance only for a stage gate, a demonstrably
  platform-sensitive change, or an explicit human request; publish only from an
  explicit release;
- public-repository runner pricing does not relax any of these limits. Compute,
  energy, queue capacity, and developer attention remain product resources.

Every pushed handoff must state the change class, the exact local gate that
passed, the single expected remote workflow, and whether that remote evidence is
passed, pending, or blocked. If there is no coherent feature ready to hand off,
do not push merely because a work interval ended.
