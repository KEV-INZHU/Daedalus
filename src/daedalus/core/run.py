"""Run state as a pure fold over the event log.

Nothing about a run is stored except its events. `replay` rebuilds the state
and re-checks every execution and worker transition against the legal tables:
a log that encodes an illegal transition is treated as corrupt rather than
silently repaired.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from daedalus.audit.store import Event
from daedalus.core.authorization import ApprovalRecord
from daedalus.core.contracts import TaskContract, contract_from_dict
from daedalus.core.errors import AuditIntegrityError, IllegalTransition
from daedalus.core.evidence import EvidenceRecord
from daedalus.core.policy import Policy, policy_from_dict
from daedalus.core.state_machine import (
    WORKER_ACTIVE,
    Disposition,
    ExecutionState,
    Severity,
    WorkerState,
    check_execution_transition,
    check_worker_transition,
)

# Event types. The audit log is the authoritative record; these are its words.
RUN_CREATED = "run.created"
EXECUTION = "execution.transition"
CONTRACT_AMENDED = "contract.amended"
CANDIDATE_RECORDED = "candidate.recorded"
CANDIDATE_FINALIZED = "candidate.finalized"
CHECK_STARTED = "check.started"
EVIDENCE_RECORDED = "evidence.recorded"
EVIDENCE_INVALIDATED = "evidence.invalidated"
REVIEW_RECORDED = "review.recorded"
REVIEW_FAILED = "review.failed"
FINDING_RESOLVED = "finding.resolved"
BLOCKER_RAISED = "blocker.raised"
BLOCKER_RESOLVED = "blocker.resolved"
CRITERION_ATTESTED = "criterion.attested"
APPROVAL_RECORDED = "approval.recorded"
RISK_EXCEPTION = "risk.exception"
VIOLATION_RECORDED = "violation.recorded"
VIOLATION_RESOLVED = "violation.resolved"
ACTION_INTENT = "action.intent"
ACTION_COMPLETED = "action.completed"
ACTION_UNKNOWN = "action.unknown"
ACTION_RECONCILED = "action.reconciled"
PLAN_VALIDATED = "plan.validated"
WORKER_CREATED = "worker.created"
WORKER_TRANSITION = "worker.transition"
WORKER_INTEGRATED = "worker.integrated"
WORKSPACE_REMOVED = "worker.workspace_removed"
COST_CHARGED = "budget.charged"
CANCEL_REQUESTED = "cancel.requested"
UNRESOLVABLE = "run.unresolvable"
DISPOSITION = "disposition.recorded"
HARNESS_STOP_BLOCKED = "harness.stop_blocked"
RECOVERY = "recovery.performed"


@dataclass
class Finding:
    finding_id: str
    review_id: str
    perspective: str
    reviewer: str
    candidate_id: str
    title: str
    evidence: str
    severity: Severity
    required_change: str
    verification: str
    resolved_by: str | None = None
    resolution: str | None = None


@dataclass
class Review:
    review_id: str
    perspective: str
    reviewer: str
    candidate_id: str
    contract_version: int
    source: str  # launched | submitted
    exposed_to: tuple[str, ...]
    summary: str
    finding_ids: tuple[str, ...]
    resolves: tuple[str, ...]


@dataclass
class WorkerRecord:
    task_id: str
    package: dict[str, Any]
    contract_version: int
    base_candidate_id: str | None
    state: WorkerState = WorkerState.PENDING
    attempts: int = 0
    session_id: str | None = None
    principal: str | None = None
    proposal: dict[str, Any] | None = None
    integrated: bool = False
    note: str = ""
    workspace: str | None = None  # isolated worktree path, while one exists
    workspace_seed: dict[str, str | None] | None = None  # rel path -> sha256 as seeded
    workspace_cleanup_failures: int = 0


@dataclass
class RunState:
    run_id: str
    created_at: float
    created_by: str
    mode: str
    policy: Policy
    policy_hash: str
    contract: TaskContract
    contract_file: str | None
    contract_file_hash: str | None
    execution: ExecutionState = ExecutionState.QUEUED
    contract_history: list[dict[str, Any]] = field(default_factory=list)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_candidate_id: str | None = None
    finalized_candidate_id: str | None = None
    candidate_authors: set[str] = field(default_factory=set)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    invalidated: dict[str, str] = field(default_factory=dict)
    checks_in_flight: dict[str, dict[str, Any]] = field(default_factory=dict)
    reviews: list[Review] = field(default_factory=list)
    failed_reviews: list[dict[str, Any]] = field(default_factory=list)
    findings: dict[str, Finding] = field(default_factory=dict)
    blockers: dict[str, dict[str, Any]] = field(default_factory=dict)
    attestations: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[ApprovalRecord] = field(default_factory=list)
    consumed_approvals: set[str] = field(default_factory=set)
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_exceptions: list[dict[str, Any]] = field(default_factory=list)
    violations: dict[str, dict[str, Any]] = field(default_factory=dict)
    workers: dict[str, WorkerRecord] = field(default_factory=dict)
    plan_versions: int = 0
    attempts_used: int = 0
    cost_used: float = 0.0
    unresolvable: str | None = None
    cancel_requested: bool = False
    disposition: Disposition | None = None
    disposition_reasons: list[dict[str, Any]] = field(default_factory=list)
    accepted_candidate_id: str | None = None
    stop_blocks: int = 0
    last_seq: int = 0

    # ----------------------------------------------------------- derived views
    @property
    def active_workers(self) -> list[WorkerRecord]:
        return [w for w in self.workers.values() if w.state in WORKER_ACTIVE]

    @property
    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings.values() if f.resolved_by is None]

    @property
    def open_blockers(self) -> dict[str, dict[str, Any]]:
        return {k: b for k, b in self.blockers.items() if b.get("resolved_by") is None}

    @property
    def open_violations(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.violations.items() if v.get("resolved_by") is None}

    @property
    def unknown_actions(self) -> dict[str, dict[str, Any]]:
        """Restricted actions whose outcome is not known (intent without completion)."""
        return {k: a for k, a in self.actions.items() if a["status"] in ("intent", "unknown")}

    @property
    def is_finished(self) -> bool:
        return self.execution is ExecutionState.FINISHED


# --------------------------------------------------------------------- reducer
def _created(ev: Event) -> RunState:
    p = ev.payload
    return RunState(
        run_id=ev.run_id,
        created_at=ev.ts,
        created_by=ev.actor,
        mode=p["mode"],
        policy=policy_from_dict(p["policy"]),
        policy_hash=p["policy_hash"],
        contract=contract_from_dict(p["contract"]),
        contract_file=p.get("contract_file"),
        contract_file_hash=p.get("contract_file_hash"),
        contract_history=[p["contract"]],
    )


def _apply(s: RunState, ev: Event) -> None:
    t, p = ev.type, ev.payload
    if t == EXECUTION:
        target = ExecutionState(p["to"])
        check_execution_transition(s.execution, target)
        s.execution = target
    elif t == CONTRACT_AMENDED:
        s.contract = contract_from_dict(p["contract"])
        s.contract_history.append(p["contract"])
        s.contract_file_hash = p.get("contract_file_hash", s.contract_file_hash)
        for wid in p.get("superseded_workers", ()):
            _worker_to(s, wid, WorkerState.SUPERSEDED, "contract amended")
    elif t == CANDIDATE_RECORDED:
        cid = p["candidate_id"]
        s.candidates.setdefault(cid, {"manifest": p["manifest"], "by": ev.actor, "at": ev.ts})
        s.current_candidate_id = cid
        if p.get("authored", False):
            s.candidate_authors.add(ev.actor)
    elif t == CANDIDATE_FINALIZED:
        s.finalized_candidate_id = p["candidate_id"]
    elif t == CHECK_STARTED:
        s.checks_in_flight[p["key"]] = p
        s.attempts_used += 1
    elif t == EVIDENCE_RECORDED:
        rec = EvidenceRecord.from_dict(p["record"])
        s.evidence.append(rec)
        if p.get("key"):
            s.checks_in_flight.pop(p["key"], None)
    elif t == EVIDENCE_INVALIDATED:
        s.invalidated[p["evidence_id"]] = p["reason"]
    elif t == REVIEW_RECORDED:
        r = p["review"]
        review = Review(
            review_id=r["review_id"],
            perspective=r["perspective"],
            reviewer=ev.actor,
            candidate_id=r["candidate_id"],
            contract_version=r["contract_version"],
            source=r["source"],
            exposed_to=tuple(r.get("exposed_to", ())),
            summary=r.get("summary", ""),
            finding_ids=tuple(f["finding_id"] for f in r.get("findings", ())),
            resolves=tuple(r.get("resolves", ())),
        )
        s.reviews.append(review)
        for f in r.get("findings", ()):
            s.findings[f["finding_id"]] = Finding(
                finding_id=f["finding_id"],
                review_id=review.review_id,
                perspective=review.perspective,
                reviewer=ev.actor,
                candidate_id=review.candidate_id,
                title=f["title"],
                evidence=f["evidence"],
                severity=Severity(f["severity"]),
                required_change=f.get("required_change", ""),
                verification=f.get("verification", ""),
            )
        for fid in review.resolves:
            if fid in s.findings and s.findings[fid].resolved_by is None:
                s.findings[fid].resolved_by = ev.actor
                s.findings[fid].resolution = f"resolved by {review.perspective} review {review.review_id}"
    elif t == REVIEW_FAILED:
        s.failed_reviews.append({**p, "at": ev.ts})
    elif t == FINDING_RESOLVED:
        f = s.findings[p["finding_id"]]
        f.resolved_by, f.resolution = ev.actor, p.get("note", "")
    elif t == BLOCKER_RAISED:
        s.blockers[p["blocker_id"]] = {
            "description": p["description"],
            "raised_by": ev.actor,
            "resolved_by": None,
        }
    elif t == BLOCKER_RESOLVED:
        s.blockers[p["blocker_id"]]["resolved_by"] = ev.actor
    elif t == CRITERION_ATTESTED:
        s.attestations.append({**p, "by": ev.actor, "at": ev.ts})
    elif t == APPROVAL_RECORDED:
        s.approvals.append(ApprovalRecord.from_dict(p["approval"]))
    elif t == RISK_EXCEPTION:
        s.risk_exceptions.append({**p, "by": ev.actor, "at": ev.ts})
    elif t == VIOLATION_RECORDED:
        s.violations[p["violation_id"]] = {**p, "resolved_by": None, "at": ev.ts}
    elif t == VIOLATION_RESOLVED:
        s.violations[p["violation_id"]]["resolved_by"] = ev.actor
    elif t == ACTION_INTENT:
        s.actions[p["action_id"]] = {**p, "status": "intent", "by": ev.actor}
        if p.get("single_use") and p.get("approval_id"):
            s.consumed_approvals.add(p["approval_id"])
    elif t == ACTION_COMPLETED:
        s.actions[p["action_id"]].update(status="completed" if p["ok"] else "failed", result=p)
    elif t == ACTION_UNKNOWN:
        s.actions[p["action_id"]]["status"] = "unknown"
    elif t == ACTION_RECONCILED:
        s.actions[p["action_id"]].update(
            status="completed" if p["occurred"] else "not_executed", reconciled_by=ev.actor
        )
    elif t == PLAN_VALIDATED:
        s.plan_versions += 1
    elif t == WORKER_CREATED:
        pkg = p["package"]
        s.workers[pkg["task_id"]] = WorkerRecord(
            task_id=pkg["task_id"],
            package=pkg,
            contract_version=p["contract_version"],
            base_candidate_id=p.get("base_candidate_id"),
        )
    elif t == WORKER_TRANSITION:
        w = s.workers[p["task_id"]]
        target = WorkerState(p["to"])
        _worker_to(s, p["task_id"], target, p.get("reason", ""))
        if target is WorkerState.DISPATCHED:
            w.attempts += 1
            s.attempts_used += 1
            w.principal = p.get("principal")
            w.base_candidate_id = p.get("base_candidate_id", w.base_candidate_id)
        if p.get("session_id"):
            w.session_id = p["session_id"]
        if "proposal" in p:
            w.proposal = p["proposal"]
        if p.get("workspace"):
            w.workspace = p["workspace"]
            w.workspace_seed = p.get("workspace_seed") or {}
    elif t == WORKSPACE_REMOVED:
        if p.get("removed", True):
            s.workers[p["task_id"]].workspace = None
        else:
            s.workers[p["task_id"]].workspace_cleanup_failures += 1
    elif t == WORKER_INTEGRATED:
        s.workers[p["task_id"]].integrated = True
    elif t == COST_CHARGED:
        s.cost_used += float(p.get("cost", 0.0))
        s.attempts_used += int(p.get("attempts", 0))
    elif t == CANCEL_REQUESTED:
        s.cancel_requested = True
    elif t == UNRESOLVABLE:
        s.unresolvable = p["reason"]
    elif t == DISPOSITION:
        s.disposition = Disposition(p["disposition"])
        s.disposition_reasons = list(p.get("reasons", ()))
        if s.disposition is Disposition.ACCEPTED:
            s.accepted_candidate_id = p["candidate_id"]
    elif t == HARNESS_STOP_BLOCKED:
        s.stop_blocks += 1
    elif t == RECOVERY:
        pass  # the individual corrective events carry the state changes
    else:
        raise AuditIntegrityError(f"unknown event type {t!r} at seq {ev.seq}")


def _worker_to(s: RunState, task_id: str, target: WorkerState, note: str) -> None:
    w = s.workers[task_id]
    check_worker_transition(w.state, target)
    w.state = target
    w.note = note


def replay(events: Iterable[Event]) -> RunState:
    state: RunState | None = None
    for ev in events:
        if state is None:
            if ev.type != RUN_CREATED:
                raise AuditIntegrityError(f"run {ev.run_id} does not begin with {RUN_CREATED}")
            state = _created(ev)
        else:
            try:
                _apply(state, ev)
            except IllegalTransition as exc:
                raise AuditIntegrityError(f"illegal transition in audit log at seq {ev.seq}: {exc}") from exc
            except KeyError as exc:
                raise AuditIntegrityError(f"event at seq {ev.seq} references unknown {exc}") from exc
        state.last_seq = ev.seq
    if state is None:
        raise AuditIntegrityError("no events for run")
    return state
