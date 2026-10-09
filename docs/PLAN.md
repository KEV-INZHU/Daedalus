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
- [x] Proof Gate: run required checks, retries, bind evidence to candidate — `verification/proof_gate.py`
- [x] Crash recovery drills (checks, workers, restricted actions) covered by tests

## Phase 0 — Required control-plane tests (§16)

- [x] Mandatory check fails → never accepted
- [x] Candidate changes after a passing check → evidence STALE
- [x] Worker returns after cancellation → cannot integrate
- [x] Sensitive path modified → risk floor enforced
- [x] Contract criterion changes → version advances, evidence invalidated
- [x] Workers overlap interfaces/ownership → conflict detected before verification
- [x] Budget expires with blocker unresolved → never accepted
- [x] Authorization expires before action → action denied
- [x] Verifier untrusted → required check fails closed
- [x] Candidate identity changes mid-run → prior evidence cannot certify new candidate
- [x] Untracked file added → candidate identity changes
- [x] Contract drifts without authorization → rejected/blocked, never accepted
- [x] Orchestrator crashes during execution → recovery reconciles, no presumed success
- [x] Restricted op may have executed before crash → reconciliation before retry
- [x] Illegal state transition → rejected
- [x] Required check absent from results → acceptance impossible
- [x] Audit log tampering → detected by hash chain

## Phase 1 — Adapters, CLI, single-worker baseline

- [x] Adapter contract with declared capabilities, fail-closed `require()` — `adapters/base.py`
- [x] Inline adapter (harness is the executor) — `adapters/inline.py`
- [x] Command adapter with `claude-code` preset (codex/aider experimental) — `adapters/command.py`
- [x] Simulated adapter for tests — `adapters/simulated.py`
- [x] Lifecycle loop: Builder → verify → required reviews → finish — `orchestration/lifecycle.py`
- [x] Role prompts / skills (builder, reviewers, gate) shipped as package data
- [x] CLI: `init`, `on/off`, `start`, `run`, `status`, `verify`, `finish`, `cancel`, `log`/`audit`
- [x] CLI human decisions: `approve`, `deny`, `attest`, `amend`, `risk-exception`, `reconcile`, `act`
- [x] Claude Code shim: Stop hook gates "done", SessionStart injects run context, `daedalus install claude-code`
- [x] Human-authority commands require an interactive terminal (defence in depth)
- [x] End-to-end test with a scripted Builder and reviewer on a synthetic repo
- [ ] Live baseline: run real tasks through the `claude-code` adapter and record acceptance, rework, cost, latency
- [ ] Exercise the experimental `codex` / `aider` presets against the real CLIs

Status: 76 tests passing (`python -m pytest`).

## Docs and schemas

- [x] Build plan (this file)
- [x] README: three-surface quickstart (toggle, contract, status)
- [x] `docs/architecture.md`, `docs/state-machine.md`, `docs/security-model.md`, `docs/adapter-contract.md`
- [x] JSON schemas: task contract, evidence record, approval record, worker result, policy

## Phase 2 — Council and bounded fan-out (not started; gated on Phase 1 measurements)

- [x] Full Council for council mode (four launched perspectives); Plato arbitration of disputed findings (run 09eb250a50f2)
- [x] Isolated worktrees per worker (run f318fc00189d)
- [x] Bounded fan-out: planned DAG, concurrency up to the policy cap (run 8ea56a7320bc)
- [x] Integration conflict detection under concurrency (ownership, base hashes, prior integrations)
- [x] Independent first-pass reviews before peer exposure (launched reviewers see only their own findings; Plato sees all)

## Phase 3 — Measurement (not started)

- [x] `daedalus metrics`: acceptance, first-pass, rework, cost and latency per accepted change, manual interventions
- [ ] Escaped-defect and regression tracking (needs a way to link later fixes back to accepted runs)
- [ ] Parallelism savings vs integration overhead

## Deliberately out of scope until usage justifies it

Server, multi-user control plane, plugin marketplace, distributed scheduler,
public remote API.

## Dogfooding log — Daedalus building Daedalus

Remaining work runs under the gate in this repository (`.daedalus.yml`, review mode,
hybrid execution: the Builder works inline in the harness session; Daedalus launches the
independent reviewer through the `claude-code` adapter).

| Run | Objective | Rounds | Disposition | Notes |
|-----|-----------|--------|-------------|-------|
| 919e19288a2d | claude-code adapter: array-form JSON output, clean launch env | 3 candidates | ACCEPTED (17 min, $1.50) | Found by the first live launch. Reviewer caught a MAJOR (error subtypes reported COMPLETED) + 5 minor/advisory; F7 advisory left open |
| c51720a89616 | `daedalus metrics` from the audit log | 2 candidates | ACCEPTED ($1.63) | Reviewer: 3 minor + 3 advisory fixed, F7-F8 advisory left open. Metrics exposed 8 stop blocks per launched reviewer |
| 20912f92221d | Hooks ignore Daedalus-launched sessions | 2 candidates | ACCEPTED ($0.69) | Stop blocks 8 → 0. Reviewer caught a MAJOR (hook tests fail inside a launched agent); fixed without a conftest.py, which would have raised the tier to high |
| 64c62f68403b | Phase 1 follow-up backlog (UTF-8 output, stderr tail, metrics, marker constant) | 2 candidates | ACCEPTED ($0.82) | Reviewer: 1 minor (metrics could still abort) + 3 advisory fixed; F5-F7 advisory left open |
| 3e6772ef6067 | Lean launches: no MCP/skills, read-only allowlist, per-role models | 3 candidates | ACCEPTED ($0.75) | Startup 37k → 5k input tokens per launch. First BLOCKERs: the "locked" profile was a denylist with holes; rebuilt as an allowlist |
| 27c612afc6c3 | Lock every built-in preset; `--adapter` override; review follow-ups | 2 candidates | ACCEPTED ($0.53) | Reviewer caught a MAJOR: codex's `--full-auto` rode along on review launches. Presets now split builder-only flags |
| f318fc00189d | Phase 2: isolated worktrees for non-in-place workers | 4 candidates | ACCEPTED ($1.65) | Reviewer caught 2 MAJORs: committed worker changes dropped from proposals; a shared /tmp hooks path I introduced. F15/F16 fixed in 8ea5 |
| 8ea56a7320bc | Phase 2: bounded fan-out (plan → parallel worktrees → ordered integration) | 4 candidates | ACCEPTED ($1.65) | Default cap stays 1. Reviewer caught 2 MAJORs (unsanitized package ids as paths; aborted rounds leaking sessions) and a flaky test as a BLOCKER |
| 09eb250a50f2 | Phase 2: disputes and Plato arbitration | — | in progress | Overrules advisory unless policy opts in |

Follow-ups: run f318 F17 (filter drivers during proposal), F18 (tests use the real temp dir). Run 64c6 F6 (utf8_output only from the console entry point). Run 27c6: F7 (`--adapter claude-code` when
config omits `name`), F8 (aider's `--yes-always` sits in the shared argv), F9 (dedupe preset-lock tests).

Queue: Phase 3 escaped-defect tracking; run 8ea5 advisories F13 (public cancel_worker), F14 (cancel
sessions that outlive the abort grace period).
