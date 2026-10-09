# Architecture

Daedalus coordinates AI agents, repository operations, verification, review
and authorization. The harness supplies agent execution, and Daedalus owns the
engineering lifecycle.

```
 Harness (Claude Code, Codex, terminal)
        │  hooks / CLI / adapter
        ▼
 ┌─────────────────────────── Daedalus Core ───────────────────────────┐
 │ Ariadne (orchestration/ariadne.py) — the only writer of state        │
 │   contracts · policy · risk · evidence · authorization · acceptance  │
 │   append-only audit log (SQLite, hash-chained) → state by replay     │
 └──────────────────────────────────────────────────────────────────────┘
        │                     │                         │
  Proof Gate             Adapters                 Repository
  (verification/)        (adapters/)              (repository/)
  trusted checks         launch/cancel agents     candidate identity
```

## Layers

| Layer            | Package                       | Responsibility                                           |
|------------------|-------------------------------|----------------------------------------------------------|
| Core             | `daedalus.core`               | Pure rules: contracts, state machine, risk, evidence, authorization, the acceptance predicate |
| Audit            | `daedalus.audit`              | Append-only, hash-chained event store                    |
| Orchestration    | `daedalus.orchestration`      | Ariadne (validates and appends every change), scheduler, lifecycle loop, role prompts |
| Verification     | `daedalus.verification`       | Proof Gate: runs trusted checks, binds results to candidates |
| Repository       | `daedalus.repository`         | Candidate manifests and diffs                            |
| Adapters         | `daedalus.adapters`           | Harness translation with declared capabilities           |
| Harness shims    | `daedalus.harness`            | Claude Code hooks and skill installation                 |
| Skills           | `daedalus/data/skills`        | Instruction packages; never enforcement                  |

## Key decisions

1. **State is a fold over events.** Nothing about a run is stored except its
   events. `core/run.py:replay` rebuilds state and re-checks every transition.
   A log that encodes an illegal transition is treated as corrupt and never
   repaired.
2. **Ariadne is the single writer.** Every mutation is a method on `Ariadne`
   that validates against replayed state and the pinned policy before
   appending. Adapters and agents call those methods; they never touch the store.
3. **Acceptance is a pure function.** `core/acceptance.py:evaluate(state, observation)`
   takes persisted state plus a live observation (current candidate, policy
   hash, contract file hash, environment fingerprints, time). Every unmet
   condition is returned as a reason tagged `agent`, `human` or `system`, so
   harnesses can route each one to whoever can fix it.
4. **Policy is pinned.** A run records the effective policy's hash at start. If
   the policy changes mid-run, that run can't be accepted. Policy must be
   committed before a run starts.
5. **Evidence is revision-bound.** Evidence records the candidate, contract
   version, policy hash, check definition and environment fingerprint. If any of
   them no longer matches, the check is STALE.
6. **Small on purpose.** One local CLI, one SQLite file per repository, one
   orchestrator at a time (`orchestration/lock.py`). No server, broker or
   multi-user layer until real usage calls for one.

## Roles

Roles live in the prompts and skills. Their authority lives in the core.

- **Brunel** (Builder) produces candidates. Recorded as an author, so it can't review its own candidate.
- **Council** reviewers (Socrates, Mozi, Aristotle, James) produce findings. Only the
  perspective that raised a finding, or a human with `attest` authority, can resolve it.
- **Plato** arbitrates material design disagreements. It's advisory and can't override rules.
- **Ariadne** enforces the rules and derives the disposition.
- **Humans** hold authorities (`merge`, `deploy`, `attest`, `contract_change`, ...).
  Principals beginning `agent:` never hold authority, whatever the policy file says.
