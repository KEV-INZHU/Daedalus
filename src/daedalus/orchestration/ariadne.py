"""Ariadne: the deterministic orchestrator (spec §2.1, §5, §10-§13).

Every state change goes through here. Each method validates the request
against the replayed run state and the pinned policy *before* appending an
event; agents and adapters only ever call these methods, never the store.
Authority checks key off the acting principal: principals starting with
`agent:` hold no authority, whatever the policy says.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from daedalus.adapters.base import Adapter
from daedalus.audit.store import EventStore
from daedalus.core import run as ev
from daedalus.core.acceptance import Decision, Observation, evaluate, observed_violations
from daedalus.core.authorization import ApprovalRecord, authorization_status
from daedalus.core.config import STATE_DIR, RepoConfig, find_root, load_config, uncommitted_policy_files
from daedalus.core.contracts import (
    MODES,
    PERSPECTIVES,
    TaskContract,
    amend_contract,
    build_contract,
    removed_hard_constraints,
)
from daedalus.core.errors import (
    AuthorizationError,
    ContractError,
    DaedalusError,
    IllegalTransition,
    IntegrationError,
    PolicyError,
)
from daedalus.core.evidence import EvidenceRecord, environment_fingerprint
from daedalus.core.paths import normalize
from daedalus.core.policy import ActionDefinition, is_agent, sha256_hex
from daedalus.core.run import RunState, WorkerRecord, replay
from daedalus.core.state_machine import (
    WORKER_ACTIVE,
    AuthorizationState,
    Disposition,
    ExecutionState,
    Severity,
    WorkerState,
    check_execution_transition,
)
from daedalus.orchestration.lock import OrchestratorLock
from daedalus.orchestration.scheduler import WorkPackage, dispatch_blockers, live_packages, validate_plan
from daedalus.repository import worktree
from daedalus.repository.candidate import CandidateManifest, capture, file_sha256, git_head

DEFAULT_VERIFIER = "daedalus-proof-gate@local"
CLEANUP_ATTEMPTS = 3  # workspace removals tried before leaving it to a human
SYSTEM = "system:ariadne"


def _uid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def _require_principal(actor: str) -> None:
    if ":" not in actor or not actor.split(":", 1)[1]:
        raise AuthorizationError(f"principal {actor!r} must look like human:<id>, agent:<id> or system:<id>")


class Ariadne:
    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        verifier_id: str = DEFAULT_VERIFIER,
        adapter: Adapter | None = None,
    ):
        self.root = Path(root).resolve()
        self.state_dir = self.root / STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(self.state_dir / "daedalus.db")
        self.clock = clock
        self.verifier_id = verifier_id
        self.adapter = adapter
        self.lock = OrchestratorLock(self.state_dir / "orchestrator.lock")

    @classmethod
    def open(cls, start: str | Path = ".", **kw: Any) -> Ariadne:
        return cls(find_root(start), **kw)

    def close(self) -> None:
        self.store.close()

    # =============================================================== reading
    def config(self) -> RepoConfig:
        return load_config(self.root)

    def state(self, run_id: str) -> RunState:
        events = self.store.events(run_id)
        if not events:
            raise DaedalusError(f"no run {run_id!r}")
        return replay(events)

    def active_run_id(self) -> str | None:
        ids = self.store.open_run_ids()
        return ids[0] if ids else None

    def require_active(self) -> str:
        rid = self.active_run_id()
        if rid is None:
            raise DaedalusError(
                'no open run. Start one with `daedalus start "<goal>"` or `daedalus run "<goal>"`.'
            )
        return rid

    def _contract_path(self, run_id: str) -> Path:
        return self.state_dir / "runs" / run_id / "contract.yaml"

    def _contract_file_hash(self, run_id: str) -> str | None:
        p = self._contract_path(run_id)
        try:
            return file_sha256(p)
        except OSError:
            return None

    def capture(self, state: RunState) -> CandidateManifest:
        return capture(self.root, state.contract.base_revision, state.policy.candidate_exclude)

    def observe(self, state: RunState) -> Observation:
        try:
            pol = self.config().policy
            policy_hash, policy_error = pol.policy_hash, None
        except (DaedalusError, yaml.YAMLError, OSError) as exc:
            policy_hash, policy_error = None, str(exc)
        fps = {
            cid: environment_fingerprint(state.policy.checks[cid], self.verifier_id)[0]
            for cid in state.contract.required_checks
            if cid in state.policy.checks
        }
        return Observation(
            candidate=self.capture(state),
            policy_hash=policy_hash,
            policy_error=policy_error,
            contract_file_hash=self._contract_file_hash(state.run_id),
            env_fingerprints=fps,
            now=self.clock(),
        )

    def evaluate(self, run_id: str) -> Decision:
        state = self.state(run_id)
        return evaluate(state, self.observe(state))

    # ============================================================== writing
    def _append(self, run_id: str, type_: str, actor: str, payload: dict[str, Any]) -> None:
        _require_principal(actor)
        self.store.append(run_id, type_, actor, payload, self.clock())

    def _transition(self, state: RunState, target: ExecutionState, actor: str, reason: str) -> None:
        check_execution_transition(state.execution, target)
        self._append(
            state.run_id,
            ev.EXECUTION,
            actor,
            {"from": state.execution.value, "to": target.value, "reason": reason},
        )
        state.execution = target

    def _running(self, run_id: str) -> RunState:
        state = self.state(run_id)
        if state.execution is not ExecutionState.RUNNING or state.cancel_requested:
            raise IllegalTransition(f"run {run_id} is {state.execution.value}; this needs a RUNNING run")
        return state

    def _write_contract(self, contract: TaskContract) -> tuple[str, str]:
        p = self._contract_path(contract.run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# Authoritative copy lives in the audit log. Editing this file is contract drift;\n"
            "# change requirements with `daedalus amend` instead.\n"
            + yaml.safe_dump(contract.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return p.relative_to(self.root).as_posix(), file_sha256(p)

    def record_candidate(
        self, run_id: str, *, actor: str, authored: bool = True, label: str = ""
    ) -> CandidateManifest:
        state = self.state(run_id)
        cand = self.capture(state)
        if cand.candidate_id != state.current_candidate_id or (
            authored and actor not in state.candidate_authors
        ):
            self._append(
                run_id,
                ev.CANDIDATE_RECORDED,
                actor,
                {
                    "candidate_id": cand.candidate_id,
                    "manifest": cand.manifest,
                    "complete": cand.complete,
                    "incomplete_reasons": list(cand.incomplete_reasons),
                    "authored": authored and is_agent(actor),
                    "label": label,
                },
            )
        return cand

    def note_violations(self, state: RunState, obs: Observation) -> None:
        """Persist live violations so they cannot vanish by being reverted later."""
        known = {v["key"] for v in state.violations.values()}
        for key, message, who in observed_violations(state, obs):
            if key not in known:
                self._append(
                    state.run_id,
                    ev.VIOLATION_RECORDED,
                    SYSTEM,
                    {
                        "violation_id": f"V{len(state.violations) + 1}",
                        "key": key,
                        "message": message,
                        "fixable_by": who,
                        "candidate_id": obs.candidate.candidate_id,
                    },
                )
                state = self.state(state.run_id)
                known.add(key)

    # ================================================================ start
    def start(self, spec: dict[str, Any], *, actor: str, mode: str | None = None) -> str:
        _require_principal(actor)
        open_run = self.active_run_id()
        if open_run:
            raise DaedalusError(f"run {open_run} is still open; finish, cancel or abandon it first")
        dirty = uncommitted_policy_files(self.root)
        if dirty:
            raise PolicyError(
                f"uncommitted policy configuration {dirty}: commit it before starting a run "
                "(a run may only be certified by committed, reviewable policy)"
            )
        cfg = self.config()
        policy = cfg.policy
        mode = mode or cfg.mode
        if mode not in MODES:
            raise ContractError(f"mode must be one of {MODES}")
        run_id = uuid.uuid4().hex[:12]
        contract = build_contract(run_id, spec, policy, git_head(self.root))
        rel, fhash = self._write_contract(contract)
        self._append(
            run_id,
            ev.RUN_CREATED,
            actor,
            {
                "contract": contract.to_dict(),
                "policy": policy.raw,
                "policy_hash": policy.policy_hash,
                "mode": mode,
                "contract_file": rel,
                "contract_file_hash": fhash,
                "config_source": cfg.source,
                "config_notes": list(cfg.notes),
            },
        )
        self._transition(self.state(run_id), ExecutionState.RUNNING, SYSTEM, "run started")
        self.record_candidate(run_id, actor=SYSTEM, authored=False, label="baseline")
        return run_id

    # ============================================================== verify
    def verify(self, run_id: str, *, only: Iterable[str] | None = None) -> list[EvidenceRecord]:
        from daedalus.verification.proof_gate import verify

        return verify(self, run_id, only=set(only) if only else None)

    # =============================================================== finish
    def finish(self, run_id: str, *, actor: str = SYSTEM) -> Decision:
        with self.lock:
            state = self.state(run_id)
            if state.is_finished:
                return evaluate(state, self.observe(state))
            if state.execution is not ExecutionState.RUNNING:
                raise IllegalTransition(f"cannot finish a {state.execution.value} run")
            obs = self.observe(state)
            self.note_violations(state, obs)
            cid = obs.candidate.candidate_id
            state = self.state(run_id)
            if cid != state.current_candidate_id:
                self.record_candidate(run_id, actor=SYSTEM, authored=False, label="finalize")
            if cid != state.finalized_candidate_id:
                self._append(run_id, ev.CANDIDATE_FINALIZED, actor, {"candidate_id": cid})
            state = self.state(run_id)
            decision = evaluate(state, obs, finishing=True)
            if decision.disposition in (Disposition.ACCEPTED, Disposition.REJECTED):
                self._record_disposition(state, decision, actor)
            return decision

    def _record_disposition(self, state: RunState, d: Decision, actor: str) -> None:
        self._transition(state, ExecutionState.FINISHED, SYSTEM, f"final disposition {d.disposition.value}")
        self._append(
            state.run_id,
            ev.DISPOSITION,
            actor,
            {
                "disposition": d.disposition.value,
                "candidate_id": d.candidate_id,
                "risk_tier": d.risk.effective_tier,
                "checks": {k: v[0].value for k, v in d.check_states.items()},
                "reasons": [r.to_dict() for r in d.reasons],
            },
        )
        d.terminal = True

    # ======================================================= cancel / abandon
    def cancel(self, run_id: str, *, actor: str, reason: str = "") -> RunState:
        with self.lock:
            state = self.state(run_id)
            if state.is_finished:
                raise IllegalTransition(f"run {run_id} is already FINISHED")
            if not state.cancel_requested:
                self._append(run_id, ev.CANCEL_REQUESTED, actor, {"reason": reason})
                self._transition(state, ExecutionState.CANCELLING, SYSTEM, reason or "cancel requested")
                for w in list(state.workers.values()):
                    if w.state is WorkerState.PENDING:
                        self._worker_transition(run_id, w.task_id, WorkerState.CANCELLED, "run cancelled")
                    elif w.state in WORKER_ACTIVE and self.adapter and w.session_id:
                        self.adapter.cancel(w.session_id)
            self._maybe_finish_cancel(run_id)
            return self.state(run_id)

    def _maybe_finish_cancel(self, run_id: str) -> None:
        state = self.state(run_id)
        if (
            state.cancel_requested
            and not state.is_finished
            and not state.active_workers
            and not state.checks_in_flight
        ):
            self._transition(state, ExecutionState.FINISHED, SYSTEM, "cancellation complete")
            self._append(
                run_id,
                ev.DISPOSITION,
                SYSTEM,
                {
                    "disposition": Disposition.CANCELLED.value,
                    "candidate_id": state.current_candidate_id,
                    "reasons": [{"code": "cancelled", "message": "run was cancelled", "fixable_by": "human"}],
                },
            )

    def abandon(self, run_id: str, *, actor: str, reason: str) -> Decision:
        """Declare a mandatory condition unresolvable: the run is REJECTED."""
        state = self._running(run_id)
        if not state.policy.has_authority(actor, "unresolvable"):
            raise AuthorizationError(f"{actor} cannot declare a run unresolvable")
        if state.active_workers:
            raise DaedalusError("workers are still active; cancel the run instead")
        if not reason.strip():
            raise ContractError("a reason is required")
        self._append(run_id, ev.UNRESOLVABLE, actor, {"reason": reason})
        return self.finish(run_id, actor=actor)

    # ======================================================== reviews/findings
    def submit_review(
        self,
        run_id: str,
        *,
        reviewer: str,
        perspective: str,
        summary: str = "",
        findings: Iterable[dict[str, Any]] = (),
        resolves: Iterable[str] = (),
        source: str = "submitted",
        exposed_to: Iterable[str] = (),
        candidate_id: str | None = None,
    ) -> str:
        """Record a review. `source="launched"` is reserved for reviews Daedalus
        itself launched through an adapter (orchestration.lifecycle)."""
        state = self._running(run_id)
        _require_principal(reviewer)
        if perspective not in PERSPECTIVES:
            raise ContractError(f"perspective must be one of {PERSPECTIVES}")
        if source not in ("launched", "submitted"):
            raise ContractError("source must be launched or submitted")
        if reviewer.startswith("system:"):
            raise AuthorizationError("system principals do not review")
        if reviewer in state.candidate_authors:
            raise AuthorizationError(f"{reviewer} authored this candidate and cannot review it")
        cid = candidate_id or self.capture(state).candidate_id
        out = []
        for i, f in enumerate(findings):
            title, evidence = str(f.get("title", "")).strip(), str(f.get("evidence", "")).strip()
            if not title or not evidence:
                raise ContractError(f"finding {i + 1} needs a title and evidence")
            try:
                severity = Severity(str(f.get("severity", "")).upper())
            except ValueError as exc:
                raise ContractError(
                    f"finding {i + 1} severity must be one of {[s.value for s in Severity]}"
                ) from exc
            out.append(
                {
                    "finding_id": f"F{len(state.findings) + i + 1}",
                    "title": title,
                    "evidence": evidence,
                    "severity": severity.value,
                    "required_change": str(f.get("required_change", "")),
                    "verification": str(f.get("verification", "")),
                }
            )
        resolves = [str(r) for r in resolves]
        for fid in resolves:
            f = state.findings.get(fid)
            if f is None:
                raise ContractError(f"unknown finding {fid}")
            if f.perspective != perspective:
                raise AuthorizationError(
                    f"{fid} was raised by {f.perspective}; only that perspective or a human resolves it"
                )
        review_id = f"R{len(state.reviews) + 1}"
        self._append(
            run_id,
            ev.REVIEW_RECORDED,
            reviewer,
            {
                "review": {
                    "review_id": review_id,
                    "perspective": perspective,
                    "candidate_id": cid,
                    "contract_version": state.contract.contract_version,
                    "source": source,
                    "exposed_to": list(exposed_to),
                    "summary": summary,
                    "findings": out,
                    "resolves": resolves,
                }
            },
        )
        return review_id

    def record_failed_review(self, run_id: str, perspective: str, error: str) -> None:
        self._append(run_id, ev.REVIEW_FAILED, SYSTEM, {"perspective": perspective, "error": error})

    def resolve_finding(self, run_id: str, finding_id: str, *, actor: str, note: str = "") -> None:
        state = self._running(run_id)
        f = state.findings.get(finding_id)
        if f is None or f.resolved_by is not None:
            raise ContractError(f"{finding_id} is not an open finding")
        if actor != f.reviewer and not (not is_agent(actor) and state.policy.has_authority(actor, "attest")):
            raise AuthorizationError(
                "only the raising reviewer or a human with `attest` authority resolves a finding"
            )
        self._append(run_id, ev.FINDING_RESOLVED, actor, {"finding_id": finding_id, "note": note})

    def raise_blocker(self, run_id: str, description: str, *, actor: str) -> str:
        state = self._running(run_id)
        if not description.strip():
            raise ContractError("blocker needs a description")
        bid = f"B{len(state.blockers) + 1}"
        self._append(run_id, ev.BLOCKER_RAISED, actor, {"blocker_id": bid, "description": description})
        return bid

    def resolve_blocker(self, run_id: str, blocker_id: str, *, actor: str) -> None:
        state = self._running(run_id)
        b = state.open_blockers.get(blocker_id)
        if b is None:
            raise ContractError(f"{blocker_id} is not an open blocker")
        if actor != b["raised_by"] and not state.policy.has_authority(actor, "attest"):
            raise AuthorizationError(
                "only whoever raised a blocker, or a human with `attest` authority, resolves it"
            )
        self._append(run_id, ev.BLOCKER_RESOLVED, actor, {"blocker_id": blocker_id})

    def resolve_violation(self, run_id: str, violation_id: str, *, actor: str, note: str = "") -> None:
        state = self._running(run_id)
        if state.policy.violation_disposition is Disposition.REJECTED:
            raise AuthorizationError(
                "policy rejects runs with violations; they cannot be waived inside the run"
            )
        if violation_id not in state.open_violations:
            raise ContractError(f"{violation_id} is not an open violation")
        if not state.policy.has_authority(actor, "contract_change"):
            raise AuthorizationError(f"{actor} lacks `contract_change` authority")
        self._append(run_id, ev.VIOLATION_RESOLVED, actor, {"violation_id": violation_id, "note": note})

    # ====================================================== human decisions
    def attest(
        self,
        run_id: str,
        criterion_id: str,
        status: str,
        *,
        actor: str,
        evidence: str,
        interactive: bool = False,
    ) -> None:
        state = self._running(run_id)
        crit = state.contract.criterion(criterion_id)
        if crit.checks:
            raise ContractError(
                f"{criterion_id} is disposed by its checks {list(crit.checks)}, not by attestation"
            )
        if not state.policy.has_authority(actor, "attest"):
            raise AuthorizationError(f"{actor} lacks `attest` authority")
        status = status.upper()
        if status not in ("PASS", "FAIL"):
            raise ContractError("attestation status must be PASS or FAIL")
        if not evidence.strip():
            raise ContractError("an attestation needs supporting evidence")
        self._append(
            run_id,
            ev.CRITERION_ATTESTED,
            actor,
            {
                "criterion_id": criterion_id,
                "status": status,
                "evidence": evidence,
                "candidate_id": self.capture(state).candidate_id,
                "contract_version": state.contract.contract_version,
                "interactive": interactive,
            },
        )

    def approve(
        self,
        run_id: str,
        action: str,
        *,
        actor: str,
        decision: str = "APPROVED",
        expires_in: float | None = None,
        single_use: bool = True,
        rationale: str = "",
        interactive: bool = False,
    ) -> ApprovalRecord:
        state = self.state(run_id)
        if state.is_finished and state.disposition is not Disposition.ACCEPTED:
            raise IllegalTransition(
                f"run {run_id} is finished ({state.disposition.value if state.disposition else '?'})"
            )
        if action not in state.policy.authorities:
            raise ContractError(f"no authority named {action!r} in the pinned policy")
        if not state.policy.has_authority(actor, action):
            raise AuthorizationError(f"{actor} lacks `{action}` authority under the pinned policy")
        if decision not in ("APPROVED", "DENIED"):
            raise ContractError("decision must be APPROVED or DENIED")
        now = self.clock()
        ttl = state.policy.approval_ttl_seconds if expires_in is None else float(expires_in)
        rec = ApprovalRecord(
            approval_id=_uid("AP"),
            run_id=run_id,
            action=action,
            decision=decision,
            approver=actor,
            candidate_id=self.capture(state).candidate_id,
            contract_version=state.contract.contract_version,
            granted_at=now,
            expires_at=now + ttl if decision == "APPROVED" else None,
            single_use=single_use,
            interactive=interactive,
            rationale=rationale,
        )
        self._append(run_id, ev.APPROVAL_RECORDED, actor, {"approval": rec.to_dict()})
        return rec

    def deny(
        self, run_id: str, action: str, *, actor: str, rationale: str = "", interactive: bool = False
    ) -> ApprovalRecord:
        return self.approve(
            run_id, action, actor=actor, decision="DENIED", rationale=rationale, interactive=interactive
        )

    def risk_exception(
        self, run_id: str, tier: str, *, actor: str, rationale: str, interactive: bool = False
    ) -> None:
        state = self._running(run_id)
        state.policy.tier_rank(tier)
        if not state.policy.has_authority(actor, "risk_exception"):
            raise AuthorizationError(f"{actor} lacks `risk_exception` authority")
        if not rationale.strip():
            raise ContractError("a risk exception needs a rationale")
        self._append(
            run_id,
            ev.RISK_EXCEPTION,
            actor,
            {
                "tier": tier,
                "rationale": rationale,
                "candidate_id": self.capture(state).candidate_id,
                "interactive": interactive,
            },
        )

    def amend(
        self, run_id: str, changes: dict[str, Any], *, actor: str, rationale: str, interactive: bool = False
    ) -> TaskContract:
        state = self._running(run_id)
        if not state.policy.has_authority(actor, "contract_change"):
            raise AuthorizationError(f"{actor} lacks `contract_change` authority")
        if not rationale.strip():
            raise ContractError("a contract amendment needs a rationale")
        new, changed = amend_contract(state.contract, changes, state.policy)
        removed = removed_hard_constraints(state.contract, new)
        if removed and not state.policy.has_authority(actor, "hard_constraint_waiver"):
            raise AuthorizationError(
                f"removing hard constraints {removed} needs `hard_constraint_waiver` authority"
            )
        old, nd = state.contract.to_dict(), new.to_dict()
        _, fhash = self._write_contract(new)
        self._append(
            run_id,
            ev.CONTRACT_AMENDED,
            actor,
            {
                "contract": nd,
                "previous_version": state.contract.contract_version,
                "changed": changed,
                "previous": {k: old[k] for k in changed},
                "new": {k: nd[k] for k in changed},
                "rationale": rationale,
                "interactive": interactive,
                "invalidated_evidence": [
                    e.evidence_id
                    for e in state.evidence
                    if e.contract_version == state.contract.contract_version
                ],
                "superseded_workers": [
                    w.task_id for w in state.workers.values() if w.state is WorkerState.PENDING
                ],
                "contract_file_hash": fhash,
            },
        )
        return new

    # ==================================================== restricted actions
    def execute_action(
        self, run_id: str, action: str, *, actor: str, runner: Callable[[], int] | None = None
    ) -> dict[str, Any]:
        with self.lock:
            state = self.state(run_id)
            policy = state.policy
            if action not in policy.restricted_actions:
                raise AuthorizationError(f"{action!r} is not a restricted action")
            if state.unknown_actions:
                ids = ", ".join(state.unknown_actions)
                raise AuthorizationError(
                    f"restricted action(s) {ids} have unknown outcomes; reconcile before acting"
                )
            defn = policy.actions.get(action) or ActionDefinition(action)
            obs = self.observe(state)
            cid = obs.candidate.candidate_id
            if defn.requires_acceptance:
                if state.disposition is not Disposition.ACCEPTED:
                    raise AuthorizationError(f"`{action}` requires an ACCEPTED run")
                if cid != state.accepted_candidate_id:
                    raise AuthorizationError("the working tree differs from the accepted candidate")
            st, msg, rec = authorization_status(state, action, cid, obs.now)
            if st is not AuthorizationState.APPROVED or rec is None:
                raise AuthorizationError(msg)
            action_id = _uid("A")
            self._append(
                run_id,
                ev.ACTION_INTENT,
                actor,
                {
                    "action_id": action_id,
                    "action": action,
                    "approval_id": rec.approval_id,
                    "single_use": rec.single_use,
                    "candidate_id": cid,
                    "command": list(defn.command) if defn.command else None,
                },
            )
            if runner is None and defn.command is None:
                result = {
                    "action_id": action_id,
                    "ok": True,
                    "note": "authorized; performed outside Daedalus",
                }
                self._append(run_id, ev.ACTION_COMPLETED, SYSTEM, result)
                return result
            try:
                code = (
                    runner()
                    if runner
                    else subprocess.run(list(defn.command or ()), cwd=str(self.root)).returncode
                )
            except Exception as exc:
                result = {"action_id": action_id, "ok": False, "error": str(exc)}
                self._append(run_id, ev.ACTION_COMPLETED, SYSTEM, result)
                raise
            result = {"action_id": action_id, "ok": code == 0, "exit_code": code}
            self._append(run_id, ev.ACTION_COMPLETED, SYSTEM, result)
            return result

    def reconcile_action(
        self, run_id: str, action_id: str, *, occurred: bool, actor: str, interactive: bool = False
    ) -> None:
        state = self.state(run_id)
        a = state.unknown_actions.get(action_id)
        if a is None:
            raise ContractError(f"{action_id} has no unknown outcome to reconcile")
        if not state.policy.has_authority(actor, a["action"]):
            raise AuthorizationError(f"{actor} lacks `{a['action']}` authority")
        self._append(
            run_id,
            ev.ACTION_RECONCILED,
            actor,
            {
                "action_id": action_id,
                "occurred": occurred,
                "interactive": interactive,
            },
        )

    # ============================================================== workers
    def plan(
        self,
        run_id: str,
        packages: list[dict[str, Any]],
        *,
        actor: str,
        interfaces: dict[str, str] | None = None,
    ) -> list[str]:
        state = self._running(run_id)
        pkgs = [WorkPackage.from_dict(p) for p in packages]
        conflicts = validate_plan(pkgs, interfaces or {}, live_packages(state))
        if conflicts:
            raise IntegrationError("plan rejected: " + "; ".join(conflicts))
        for p in pkgs:
            self._append(
                run_id,
                ev.WORKER_CREATED,
                actor,
                {
                    "package": p.to_dict(),
                    "contract_version": state.contract.contract_version,
                    "base_candidate_id": state.current_candidate_id,
                },
            )
        self._append(
            run_id,
            ev.PLAN_VALIDATED,
            SYSTEM,
            {
                "task_ids": [p.task_id for p in pkgs],
                "interfaces": interfaces or {},
            },
        )
        return [p.task_id for p in pkgs]

    def _worker_transition(
        self, run_id: str, task_id: str, target: WorkerState, reason: str, **extra: Any
    ) -> None:
        self._append(
            run_id,
            ev.WORKER_TRANSITION,
            SYSTEM,
            {"task_id": task_id, "to": target.value, "reason": reason, **extra},
        )
        if target not in WORKER_ACTIVE:
            # The session is over and its outcome is in the log: the workspace has no further use.
            self._release_workspace(run_id, task_id)

    def _release_workspace(self, run_id: str, task_id: str) -> None:
        """Best effort: the worker's outcome is already logged, so a cleanup failure
        is recorded (and retried by recovery) rather than raised."""
        w = self.state(run_id).workers[task_id]
        if not w.workspace:
            return
        try:
            if Path(w.workspace).resolve() != worktree.workspace_path(self.root, run_id, task_id).resolve():
                # Only ever delete the exact location this task's workspace is created at.
                raise IntegrationError(f"recorded workspace {w.workspace} is not this task's location; not deleting")
            removed, error = worktree.remove(self.root, Path(w.workspace)), None
        except Exception as exc:  # noqa: BLE001 — never let cleanup undo a logged transition
            removed, error = False, f"{type(exc).__name__}: {exc}"
        if not removed and error is None:
            error = "workspace directory still present after removal"
        self._append(
            run_id,
            ev.WORKSPACE_REMOVED,
            SYSTEM,
            {"task_id": task_id, "path": w.workspace, "removed": removed, "error": error},
        )

    def dispatch(self, run_id: str, task_id: str, *, principal: str | None = None) -> WorkerRecord:
        """Persist dispatch intent before the adapter is called (spec §13).

        A package that is not in-place gets an isolated worktree, seeded with the
        current candidate; its path and seed hashes go into the dispatch event.
        """
        with self.lock:
            state = self.state(run_id)
            blockers = dispatch_blockers(state, task_id)
            if blockers:
                raise DaedalusError(f"cannot dispatch {task_id}: " + "; ".join(blockers))
            principal = principal or f"agent:worker:{task_id}"
            if not is_agent(principal):
                raise AuthorizationError("workers run as agent principals")
            cand = self.capture(state)
            pkg = WorkPackage.from_dict(state.workers[task_id].package)
            isolation: dict[str, Any] = {}
            if not pkg.in_place:
                ws = worktree.create(
                    self.root,
                    run_id,
                    task_id,
                    state.contract.base_revision,
                    cand.changed_paths,
                    pkg.owned_paths,
                    state.policy.candidate_exclude,
                )
                isolation = {"workspace": str(ws.path), "workspace_seed": ws.seed}
            try:
                self._worker_transition(
                    run_id,
                    task_id,
                    WorkerState.DISPATCHED,
                    "dispatched",
                    principal=principal,
                    base_candidate_id=cand.candidate_id,
                    **isolation,
                )
            except BaseException:
                if isolation:  # never leave a workspace the log does not know about
                    try:
                        worktree.remove(self.root, Path(isolation["workspace"]))
                    except Exception:  # noqa: BLE001 — keep the original error; recovery sweeps leftovers
                        pass
                raise
            return self.state(run_id).workers[task_id]

    def worker_started(self, run_id: str, task_id: str, session_id: str) -> None:
        self._worker_transition(run_id, task_id, WorkerState.RUNNING, "running", session_id=session_id)

    def worker_finished(
        self,
        run_id: str,
        task_id: str,
        *,
        status: str,
        proposal: dict[str, Any] | None = None,
        cost: float = 0.0,
        note: str = "",
        session_id: str | None = None,
    ) -> WorkerRecord:
        state = self.state(run_id)
        w = state.workers.get(task_id)
        if w is None:
            raise ContractError(f"unknown task {task_id}")
        if cost:
            self._append(run_id, ev.COST_CHARGED, SYSTEM, {"cost": cost, "task_id": task_id})
        if w.state not in WORKER_ACTIVE:
            raise IntegrationError(
                f"late result for {task_id} ({w.state.value}) rejected; nothing was recorded as authoritative"
            )
        extra: dict[str, Any] = {"session_id": session_id} if session_id else {}
        if state.cancel_requested or state.execution is not ExecutionState.RUNNING:
            self._worker_transition(
                run_id,
                task_id,
                WorkerState.CANCELLED,
                "result arrived after cancellation",
                diagnostic=proposal,
                **extra,
            )
        elif w.contract_version != state.contract.contract_version:
            self._worker_transition(
                run_id,
                task_id,
                WorkerState.REJECTED_STALE,
                "contract changed while the worker ran",
                diagnostic=proposal,
                **extra,
            )
        else:
            target = WorkerState(status)
            if target not in (
                WorkerState.COMPLETED,
                WorkerState.FAILED,
                WorkerState.TIMED_OUT,
                WorkerState.CANCELLED,
            ):
                raise ContractError(f"invalid worker result status {status}")
            if target is WorkerState.COMPLETED and w.workspace:
                # The proposal is what changed in the workspace, never what the agent reports.
                reported = {k: v for k, v in (proposal or {}).items() if k not in ("files", "base_hashes")}
                try:
                    derived = worktree.proposal(
                        Path(w.workspace),
                        w.workspace_seed or {},
                        state.contract.base_revision or "HEAD",
                        state.policy.candidate_exclude,
                    )
                    proposal = {**reported, **derived}
                except Exception as exc:  # noqa: BLE001 — any failure to read the workspace fails the worker
                    target, note, proposal = WorkerState.FAILED, f"no usable proposal: {exc}", reported
            self._worker_transition(
                run_id, task_id, target, note or status.lower(), proposal=proposal, **extra
            )
            if target is WorkerState.COMPLETED and w.package.get("in_place"):
                self.record_candidate(
                    run_id,
                    actor=w.principal or f"agent:worker:{task_id}",
                    authored=True,
                    label=f"in-place {task_id}",
                )
                self._append(run_id, ev.WORKER_INTEGRATED, SYSTEM, {"task_id": task_id, "in_place": True})
        self._maybe_finish_cancel(run_id)
        return self.state(run_id).workers[task_id]

    def integrate(self, run_id: str, task_id: str) -> CandidateManifest:
        with self.lock:
            state = self.state(run_id)
            w = state.workers.get(task_id)
            if w is None:
                raise ContractError(f"unknown task {task_id}")
            if state.cancel_requested or state.execution is not ExecutionState.RUNNING:
                raise IntegrationError(f"run is {state.execution.value}; worker results cannot be integrated")
            if w.state is not WorkerState.COMPLETED or w.integrated:
                raise IntegrationError(
                    f"{task_id} is {w.state.value}{' (integrated)' if w.integrated else ''}; "
                    "only un-integrated COMPLETED proposals integrate"
                )
            pkg = WorkPackage.from_dict(w.package)

            def reject(why: str) -> IntegrationError:
                self._worker_transition(run_id, task_id, WorkerState.REJECTED_STALE, why)
                return IntegrationError(f"{task_id} rejected: {why}")

            if w.contract_version != state.contract.contract_version:
                raise reject("contract changed since the task was planned")
            proposal = w.proposal or {}
            files: dict[str, str | None] = proposal.get("files") or {}
            base_hashes: dict[str, str | None] = proposal.get("base_hashes") or {}
            safe: dict[str, str | None] = {}
            for raw_path, content in files.items():
                rel = normalize(raw_path)
                if rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
                    raise reject(f"unsafe path {raw_path!r}")
                safe[rel] = content
            outside = [p for p in safe if not pkg.owns(p)]
            if outside:
                raise reject(f"touches paths outside its ownership: {outside[:5]}")
            for other in state.workers.values():
                if other.task_id != task_id and other.integrated and other.proposal:
                    clash = set(safe) & {normalize(p) for p in (other.proposal.get("files") or {})}
                    if clash:
                        raise reject(f"conflicts with integrated {other.task_id} on {sorted(clash)[:5]}")
            for rel in safe:
                target = self.root / rel
                current = file_sha256(target) if target.is_file() else None
                if rel not in base_hashes or base_hashes[rel] != current:
                    raise reject(f"{rel} changed since the worker's base revision")
            for rel, content in safe.items():
                target = self.root / rel
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content.encode("utf-8"))  # bytes: no newline translation
            cand = self.record_candidate(
                run_id,
                actor=w.principal or f"agent:worker:{task_id}",
                authored=True,
                label=f"integrated {task_id}",
            )
            self._append(
                run_id, ev.WORKER_INTEGRATED, SYSTEM, {"task_id": task_id, "candidate_id": cand.candidate_id}
            )
            return cand

    def charge(self, run_id: str, *, cost: float = 0.0, attempts: int = 0, note: str = "") -> None:
        self._append(run_id, ev.COST_CHARGED, SYSTEM, {"cost": cost, "attempts": attempts, "note": note})

    def note_stop_blocked(self, run_id: str, reasons: list[str]) -> None:
        self._append(run_id, ev.HARNESS_STOP_BLOCKED, SYSTEM, {"reasons": reasons})

    # ============================================================= recovery
    def needs_recovery(self, run_id: str) -> bool:
        state = self.state(run_id)
        pending = (
            state.checks_in_flight
            or state.active_workers
            or any(a["status"] == "intent" for a in state.actions.values())
            or any(self._cleanup_due(w) for w in state.workers.values())
        )
        return bool(pending) and not self.lock.holder_alive()

    def recover(self, run_id: str) -> list[str]:
        """Reconcile a run after an orchestrator crash. Nothing is presumed to
        have succeeded because its intent was recorded (spec §13)."""
        notes: list[str] = []
        with self.lock:
            state = self.state(run_id)
            for key, info in list(state.checks_in_flight.items()):
                check_id = info["check_id"]
                defn = state.policy.checks[check_id]
                fp, env = environment_fingerprint(defn, self.verifier_id)
                now = self.clock()
                rec = EvidenceRecord(
                    run_id=run_id,
                    check_id=check_id,
                    attempt=int(info.get("attempt", 1)),
                    candidate_id=info["candidate_id"],
                    contract_version=state.contract.contract_version,
                    policy_version=state.policy.policy_version,
                    policy_hash=state.policy_hash,
                    definition_hash=defn.definition_hash,
                    command=defn.command,
                    result="ERROR",
                    exit_code=None,
                    error="interrupted: the orchestrator stopped before the check reported",
                    verifier_id=self.verifier_id,
                    verifier_trusted=self.verifier_id in state.policy.trusted_verifiers,
                    env_fingerprint=fp,
                    environment=env,
                    stdout_sha256=None,
                    stderr_sha256=None,
                    log_path=None,
                    started_at=float(info.get("started_at", now)),
                    ended_at=now,
                    timeout_s=defn.timeout_s,
                    mandatory=defn.mandatory,
                )
                self._append(run_id, ev.EVIDENCE_RECORDED, SYSTEM, {"key": key, "record": rec.to_dict()})
                notes.append(f"check {check_id}: interrupted, recorded as ERROR")
            for w in state.active_workers:
                res = None
                if self.adapter and w.session_id and self.adapter.capabilities.status_query:
                    res = self.adapter.status(w.session_id)
                if res is None:
                    self._worker_transition(
                        run_id, w.task_id, WorkerState.FAILED, "untraceable after orchestrator restart"
                    )
                    notes.append(f"worker {w.task_id}: untraceable, marked FAILED")
                else:
                    proposal = (res.structured or {}).get("proposal")
                    self.worker_finished(
                        run_id,
                        w.task_id,
                        status=res.status,
                        proposal=proposal,
                        session_id=w.session_id,
                        note="reconciled after restart",
                    )
                    notes.append(f"worker {w.task_id}: reconciled as {res.status}")
            for aid, a in state.actions.items():
                if a["status"] == "intent":
                    self._append(run_id, ev.ACTION_UNKNOWN, SYSTEM, {"action_id": aid})
                    notes.append(f"action {a['action']} ({aid}): outcome unknown; needs human reconciliation")
            notes += self._clean_workspaces(run_id)
            if notes:
                self._append(
                    run_id,
                    ev.RECOVERY,
                    SYSTEM,
                    {"notes": notes, "took_over_stale_lock": self.lock.took_over_stale},
                )
            self._maybe_finish_cancel(run_id)
        return notes

    @staticmethod
    def _cleanup_due(w: WorkerRecord) -> bool:
        """An ended worker's workspace survived and removal has not been tried too often."""
        return bool(w.workspace) and w.state not in WORKER_ACTIVE and w.workspace_cleanup_failures < CLEANUP_ATTEMPTS

    def _clean_workspaces(self, run_id: str) -> list[str]:
        """Retry removal for ended workers whose workspace survived, and sweep
        workspaces no worker records (a crash between creation and dispatch)."""
        notes: list[str] = []
        state = self.state(run_id)
        for w in state.workers.values():
            if self._cleanup_due(w):
                self._release_workspace(run_id, w.task_id)
                after = self.state(run_id).workers[w.task_id]
                if after.workspace is None:
                    notes.append(f"worker {w.task_id}: leftover workspace removed")
                elif after.workspace_cleanup_failures >= CLEANUP_ATTEMPTS:
                    notes.append(
                        f"worker {w.task_id}: workspace {after.workspace} could not be removed after "
                        f"{CLEANUP_ATTEMPTS} attempts; delete it by hand (Daedalus will not retry)"
                    )
        keep = {w.workspace for w in self.state(run_id).workers.values() if w.workspace}
        try:
            swept = worktree.sweep(self.root, run_id, keep)
        except Exception as exc:  # noqa: BLE001 — cleanup never blocks recovery
            swept, notes = [], [*notes, f"workspace sweep failed: {exc}"]
        notes += [f"removed unrecorded workspace {p}" for p in swept]
        return notes

    # ================================================================ audit
    def contract_hash_of(self, run_id: str) -> str:
        return sha256_hex(self._contract_path(run_id).read_bytes())
