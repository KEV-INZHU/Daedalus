# State machine

Daedalus tracks execution, verification, authorization and disposition
independently (spec §5). The transition tables live in
`src/daedalus/core/state_machine.py`. Ariadne rejects anything else, and replay
treats an illegal transition found in the log as corruption.

## Execution

```
QUEUED ──► RUNNING ──► FINISHED
   │          │           ▲
   └──► CANCELLING ───────┘
```

`FINISHED` is terminal. Resuming work means starting a new run.

## Verification (per required check)

| State     | Meaning                                                                 |
|-----------|-------------------------------------------------------------------------|
| `NOT_RUN` | No evidence in this run                                                 |
| `PASS`    | A trusted verifier passed it on the *current* candidate, contract, policy and environment |
| `FAIL`    | Non-zero exit on the current candidate                                  |
| `STALE`   | Evidence exists but a binding changed (candidate, contract, policy, definition, environment) or it was invalidated |
| `ERROR`   | Timeout, crash, missing executable, interrupted by a crash, tree changed during the check, or an untrusted verifier |

Check state is **derived** each time from the latest evidence and the live
observation. It is never stored.

## Authorization (per required action)

`NOT_REQUIRED · PENDING · APPROVED · DENIED · EXPIRED`. An approval binds to an
action, candidate, contract version and approver, and carries an expiry and
single-use semantics. It is re-validated on every use.

## Workers

```
PENDING ─► DISPATCHED ─► RUNNING ─► COMPLETED ─► (SUPERSEDED | REJECTED_STALE)
   │            │            │
   │            └────────────┴─► FAILED | TIMED_OUT | CANCELLED | REJECTED_STALE
   └─► CANCELLED | SUPERSEDED
```

A package that isn't `in_place` is dispatched into an **isolated git worktree**
outside the repository. The worktree is seeded byte for byte with the current
candidate's changes and the package's owned files. The dispatch event records
its path and seed hashes. When the session ends (any transition out of
`DISPATCHED`/`RUNNING`), the proposal is derived from the worktree, not from
the agent's report, and the worktree is removed (`worker.workspace_removed`).

A non-in-place package needs a git repository with a base commit, and dispatch
fails otherwise. Gitignored paths, the policy's `candidate.exclude` paths and
symlinks aren't part of isolation. A worktree shares the repository's `.git`, so
the gate guards a cooperative-but-fallible worker, not a hostile one (see
docs/security-model.md).

### Bounded fan-out

When a run's `budget.max_workers` is above 1 (the policy cap, `default_budget.max_workers`,
defaults to **1**), the lifecycle's first round asks Brunel for a dependency graph of
packages. Ariadne validates it with the same checks as any plan (disjoint ownership,
known dependencies, no cycles). Packages then run concurrently, never more than the cap,
each in its own worktree as the agent's working directory. Each one integrates as it
finishes, and dependents start only after their prerequisites have integrated. A failed
package's dependents are cancelled without being started, and the failures are fed to
an in-place Builder in the next round. An unusable, rejected or single-package plan
falls back to the single in-place Builder. Agent sessions are the only work on worker
threads; every audit-log write stays on the orchestrator thread.

`COMPLETED` means "produced a proposal". The proposal is integrated only
through `Ariadne.integrate`, which checks ownership, base hashes, contract
version and conflicts with already-integrated work.

## Findings, disputes and arbitration

A review finding is open until the perspective that raised it resolves it in a
later review, a human with `attest` authority resolves it, or arbitration
resolves it (below). Findings at a `blocking_severities` level block acceptance
while open.

Anyone can **dispute** an open finding with a reason (`finding.disputed`).
`daedalus arbitrate F#` (or the lifecycle, when a Builder reports disputes)
launches a read-only Plato session. It sees the contract, the diff, the
evidence, the dispute and every perspective's findings: this is where reviewers'
independent first-pass conclusions meet. Plato rules `uphold` or `overrule`
with a rationale (`arbitration.recorded`). Only an `agent:plato:*` principal
that didn't author the candidate can record a ruling. A malformed ruling, or an
arbitration that changed the working tree, is recorded as `arbitration.failed`
and resolves nothing.

An `overrule` resolves the finding only when the pinned policy sets
`arbitration_resolves_findings: true`. By default the ruling is advisory: the
finding stays open, its reason is routed to a human, and it names the ruling and
`daedalus resolve F#`. Arbitration never touches checks, approvals or risk floors.

## Disposition precedence (spec §5.5)

1. A recorded ACCEPTED is preserved. Later edits are reported but need a new run.
2. A completed cancellation → CANCELLED.
3. A denied required authorization → REJECTED.
4. A declared-unresolvable condition, or an unauthorized policy/contract/scope change
   (when `violation_disposition: REJECTED`) → REJECTED.
5. Budget exhausted with anything unmet → REJECTED.
6. Anything else unmet → BLOCKED (recoverable).
7. Otherwise the predicate holds, and `finish` records ACCEPTED.

Only ACCEPTED, REJECTED and CANCELLED are recorded as final. BLOCKED leaves the
run open.

## Events

Every event type is a constant in `core/run.py`. Notable ones:

| Event                 | Written by        | Effect                                           |
|-----------------------|-------------------|--------------------------------------------------|
| `run.created`         | starter           | pins contract v1, policy and policy hash         |
| `candidate.recorded`  | system / author   | new candidate identity; authors become ineligible reviewers |
| `check.started`       | Proof Gate        | intent recorded before the check runs            |
| `evidence.recorded`   | Proof Gate / recovery | closes an in-flight check                    |
| `contract.amended`    | human             | new version; earlier evidence and approvals stale |
| `action.intent`       | Ariadne           | written before a restricted action runs          |
| `action.unknown`      | recovery          | outcome unknown; blocks retries until a human reconciles |
| `disposition.recorded`| Ariadne           | final, derived disposition                       |
