# Daedalus build plan

Tracks implementation of the v6 specification (engineering control plane) with the
"togglable gate" packaging: a strong deterministic core behind a small surface
(`daedalus on/off`, a short contract, `daedalus status`). Tick items as they land.

Build order (spec "Final recommendation"): schemas and state machine → acceptance
predicate and invariant tests → candidate/evidence manifest and audit log → local
execution adapter → one harness adapter and one Builder → single-worker baseline →
Council and fan-out only after measured readiness.

## Phase 0 — Control plane (core)

- [x] State vocabularies and legal transition tables — `core/state_machine.py`
- [x] Error types — `core/errors.py`
- [x] Repository glob semantics (`**`, segment-bound `*`) — `core/paths.py`
- [x] Hash-pinned policy, check definitions, authorities, tier requirements — `core/policy.py`
- [x] Default policy (risk floors, checks, authorities, budgets) — `data/default_policy.yaml`
- [x] `.daedalus.yml` loading, zero-config check detection — `core/config.py`
- [x] Versioned task contracts and amendment rules — `core/contracts.py`
- [x] Risk classification: max(declared, path, change, behavioral), uncertainty escalation — `core/risk.py`
- [x] Candidate manifest (tracked + untracked + submodules, content-hashed) — `repository/candidate.py`
- [x] Revision-bound evidence records and STALE derivation — `core/evidence.py`
- [x] Scoped, expiring, single-use authorization — `core/authorization.py`
- [x] Append-only hash-chained audit store (SQLite) — `audit/store.py`
- [x] Run state as a pure replay of events — `core/run.py`
- [x] Acceptance predicate and disposition derivation (§5.5, §6) — `core/acceptance.py`
- [x] Work packages, plan validation, dispatch eligibility — `orchestration/scheduler.py`
- [x] Single-orchestrator lock with stale-holder takeover — `orchestration/lock.py`
- [x] Ariadne: the single authority that validates and appends every state change — `orchestration/ariadne.py`
- [x] Check subprocess runner (scrubbed env, timeouts → ERROR) — `verification/check_runner.py`
- [ ] Proof Gate: run required checks, retries, bind evidence to candidate — `verification/proof_gate.py`
- [ ] Crash recovery drills (checks, workers, restricted actions) covered by tests

## Phase 0 — Required control-plane tests (§16)

- [ ] Mandatory check fails → never accepted
- [ ] Candidate changes after a passing check → evidence STALE
- [ ] Worker returns after cancellation → cannot integrate
- [ ] Sensitive path modified → risk floor enforced
- [ ] Contract criterion changes → version advances, evidence invalidated
- [ ] Workers overlap interfaces/ownership → conflict detected before verification
- [ ] Budget expires with blocker unresolved → never accepted
- [ ] Authorization expires before action → action denied
- [ ] Verifier untrusted → required check fails closed
- [ ] Candidate identity changes mid-run → prior evidence cannot certify new candidate
- [ ] Untracked file added → candidate identity changes
- [ ] Contract drifts without authorization → rejected/blocked, never accepted
- [ ] Orchestrator crashes during execution → recovery reconciles, no presumed success
- [ ] Restricted op may have executed before crash → reconciliation before retry
- [ ] Illegal state transition → rejected
- [ ] Required check absent from results → acceptance impossible
- [ ] Audit log tampering → detected by hash chain

## Phase 1 — Adapters, CLI, single-worker baseline

- [x] Adapter contract with declared capabilities, fail-closed `require()` — `adapters/base.py`
- [x] Inline adapter (harness is the executor) — `adapters/inline.py`
- [x] Command adapter with `claude-code` preset (codex/aider experimental) — `adapters/command.py`
- [x] Simulated adapter for tests — `adapters/simulated.py`
- [ ] Lifecycle loop: Builder → verify → required reviews → finish — `orchestration/lifecycle.py`
- [ ] Role prompts / skills (builder, reviewers, gate) shipped as package data
- [ ] CLI: `init`, `on/off`, `start`, `run`, `status`, `verify`, `finish`, `cancel`, `log`/`audit`
- [ ] CLI human decisions: `approve`, `deny`, `attest`, `amend`, `risk-exception`, `reconcile`, `act`
- [ ] Claude Code shim: Stop hook gates "done", SessionStart injects run context, `daedalus install claude-code`
- [ ] Human-authority commands require an interactive terminal (defence in depth)
- [ ] End-to-end test with a scripted Builder and reviewer on a synthetic repo

## Docs and schemas

- [x] Build plan (this file)
- [ ] README: three-surface quickstart (toggle, contract, status)
- [ ] `docs/architecture.md`, `docs/state-machine.md`, `docs/security-model.md`, `docs/adapter-contract.md`
- [ ] JSON schemas: task contract, evidence record, approval record, worker result, policy

## Phase 2 — Council and bounded fan-out (not started; gated on Phase 1 measurements)

- [ ] Full Council for review/council modes; Plato arbitration on material disagreement
- [ ] Isolated worktrees per worker; concurrency 2
- [ ] Integration conflict detection under concurrency
- [ ] Independent first-pass reviews before peer exposure

## Phase 3 — Measurement (not started)

- [ ] Metrics: escaped defects, first-pass acceptance, rework, cost/latency per accepted change
- [ ] Parallelism savings vs integration overhead

## Deliberately out of scope until usage justifies it

Server, multi-user control plane, plugin marketplace, distributed scheduler,
public remote API.
