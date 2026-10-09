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

`COMPLETED` means "produced a proposal". The proposal is integrated only
through `Ariadne.integrate`, which checks ownership, base hashes, contract
version and conflicts with already-integrated work.

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
