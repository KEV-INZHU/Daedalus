"""The single-worker lifecycle (spec §11, §17): build -> verify -> review -> finish.

This is the Phase 1 baseline. One Builder edits the working tree in place,
the Proof Gate verifies the candidate, Daedalus launches any reviews the risk
tier or mode requires, and Ariadne derives the disposition. Only reasons
tagged agent-fixable are fed back to the Builder; anything needing a human
(approvals, attestations, policy problems) ends the loop with the run left
open and BLOCKED, so the human can act and `daedalus finish` later.

Fan-out is not here on purpose: max_workers stays 1 until the baseline is
measured to be reliable (spec §14, §20.11).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from daedalus.adapters.base import Adapter, AgentRequest, require
from daedalus.core.acceptance import Decision
from daedalus.core.errors import ContractError, DaedalusError
from daedalus.core.state_machine import Disposition, WorkerState
from daedalus.orchestration import prompts
from daedalus.orchestration.ariadne import SYSTEM, Ariadne
from daedalus.repository.candidate import diff_text

Notify = Callable[[str], None]

REVIEW_TIMEOUT_S = 1800.0
BUILD_TIMEOUT_S = 3600.0


@dataclass
class RunReport:
    run_id: str
    decision: Decision
    rounds: int
    notes: list[str] = field(default_factory=list)


def _quiet(_: str) -> None:
    pass


# ------------------------------------------------------------------- reviews
def launch_review(
    ari: Ariadne, run_id: str, perspective: str, adapter: Adapter, notify: Notify = _quiet
) -> str | None:
    """Launch one independent, read-only review. Returns the review id, or None
    when the reviewer failed or returned nothing valid (recorded as a failed review)."""
    state = ari._running(run_id)
    require(
        adapter.capabilities,
        {"launch_agent": True, "structured_results": True, "identity": "launched"},
        state.policy.allowed_degradations,
    )
    cand = ari.record_candidate(run_id, actor=SYSTEM, authored=False, label=f"review {perspective}")
    decision = ari.evaluate(run_id)
    prior = [
        {"id": f.finding_id, "severity": f.severity.value, "title": f.title, "required_change": f.required_change}
        for f in state.open_findings
        if f.perspective == perspective
    ]
    prompt = prompts.review_prompt(
        perspective,
        state.contract,
        diff_text(ari.root, cand),
        {k: (v[0].value, v[1]) for k, v in decision.check_states.items()},
        prior,
    )
    notify(f"launching {perspective} review of candidate {cand.short}")
    res = adapter.run_agent(
        AgentRequest(run_id, f"review-{perspective}", perspective, prompt, ari.root, REVIEW_TIMEOUT_S, read_only=True)
    )
    if res.cost:
        ari.charge(run_id, cost=res.cost, note=f"{perspective} review")

    def failed(why: str) -> None:
        ari.record_failed_review(run_id, perspective, why)
        notify(f"{perspective} review failed: {why}")

    if res.status != "COMPLETED":
        failed(res.error or res.status)
        return None
    if ari.capture(ari.state(run_id)).candidate_id != cand.candidate_id:
        failed("the working tree changed during a read-only review")
        return None
    body = res.structured
    if not isinstance(body, dict) or not isinstance(body.get("findings", []), list):
        failed("reviewer returned no valid JSON result block")
        return None
    reviewer = f"agent:reviewer:{perspective}:{(res.session_id or 'session')[:8]}"
    open_ids = {p["id"] for p in prior}
    try:
        rid = ari.submit_review(
            run_id,
            reviewer=reviewer,
            perspective=perspective,
            summary=str(body.get("summary", "")),
            findings=body.get("findings", []),
            resolves=[r for r in body.get("resolves", []) if r in open_ids],
            source="launched",
            exposed_to=(),
            candidate_id=cand.candidate_id,
        )
    except (ContractError, DaedalusError) as exc:
        failed(f"invalid review result: {exc}")
        return None
    n = len(body.get("findings", []))
    notify(f"{perspective} review {rid}: {n} finding(s)")
    return rid


def launch_required_reviews(
    ari: Ariadne, run_id: str, adapter: Adapter, notify: Notify = _quiet
) -> list[str]:
    """Launch every review the decision still needs, plus a re-review by each
    perspective with an open blocking finding (only that perspective resolves it)."""
    decision = ari.evaluate(run_id)
    state = ari.state(run_id)
    pending = [r.code.split(":", 1)[1] for r in decision.reasons if r.code.startswith("review:")]
    pending += [
        f.perspective
        for f in state.open_findings
        if f.severity.value in state.policy.blocking_severities and f.perspective not in pending
    ]
    pending = list(dict.fromkeys(pending))
    return [rid for p in pending if (rid := launch_review(ari, run_id, p, adapter, notify))]


# --------------------------------------------------------------------- build
def build_round(
    ari: Ariadne, run_id: str, adapter: Adapter, round_no: int, feedback: list[str], notify: Notify = _quiet
) -> dict[str, Any]:
    """Plan, dispatch and run one in-place Builder package. Returns the Builder's structured result."""
    state = ari._running(run_id)
    task_id = f"build-{round_no}"
    ari.plan(
        run_id,
        [
            {
                "task_id": task_id,
                "description": state.contract.objective,
                "owned_paths": list(state.contract.scope),
                "in_place": True,
                "deadline_s": BUILD_TIMEOUT_S,
            }
        ],
        actor=SYSTEM,
    )
    w = ari.dispatch(run_id, task_id, principal="agent:brunel")
    notify(f"round {round_no}: Brunel is building ({task_id})")
    try:
        res = adapter.run_agent(
            AgentRequest(
                run_id,
                task_id,
                "brunel",
                prompts.builder_prompt(state.contract, feedback, round_no),
                ari.root,
                BUILD_TIMEOUT_S,
            )
        )
    except Exception as exc:
        ari.worker_finished(run_id, task_id, status="FAILED", note=f"adapter error: {exc}")
        raise
    status = res.status if res.status in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED") else "FAILED"
    rec = ari.worker_finished(
        run_id,
        task_id,
        status=status,
        proposal={"summary": (res.structured or {}).get("summary", ""), "output_tail": res.output[-2000:]},
        cost=res.cost,
        note=res.error or "",
        session_id=res.session_id,
    )
    notify(f"round {round_no}: builder {rec.state.value.lower()}" + (f" ({res.error})" if res.error else ""))
    out = dict(res.structured or {})
    out["_state"] = rec.state
    out["_principal"] = w.principal
    return out


# ---------------------------------------------------------------------- loop
def run_task(
    ari: Ariadne,
    spec: dict[str, Any],
    *,
    actor: str,
    adapter: Adapter,
    mode: str | None = None,
    max_rounds: int = 3,
    notify: Notify = _quiet,
) -> RunReport:
    policy = ari.config().policy
    require(adapter.capabilities, {"launch_agent": True}, policy.allowed_degradations)
    run_id = ari.start(spec, actor=actor, mode=mode)
    notify(f"run {run_id} started")
    return drive(ari, run_id, adapter=adapter, max_rounds=max_rounds, notify=notify)


def drive(
    ari: Ariadne, run_id: str, *, adapter: Adapter, max_rounds: int = 3, notify: Notify = _quiet
) -> RunReport:
    notes: list[str] = []
    feedback: list[str] = []
    rounds = 0
    for round_no in range(1, max_rounds + 1):
        rounds = round_no
        try:
            built = build_round(ari, run_id, adapter, round_no, feedback, notify)
        except DaedalusError as exc:  # budget, concurrency, or capability refusal
            notes.append(str(exc))
            break
        if built.get("status") == "blocked":
            why = str(built.get("blocker") or built.get("summary") or "builder reported it cannot proceed")
            ari.raise_blocker(run_id, why, actor=built["_principal"])
            notes.append(f"builder blocked: {why}")
            break
        if built["_state"] is not WorkerState.COMPLETED:
            feedback = [f"the previous build attempt ended {built['_state'].value}; try again"]
            continue

        notify("verifying candidate")
        ari.verify(run_id)
        decision = ari.evaluate(run_id)
        if any(r.code.startswith("check:") and r.fixable_by == "agent" for r in decision.reasons):
            feedback = [r.message for r in decision.agent_fixable]
            continue

        launch_required_reviews(ari, run_id, adapter, notify)
        decision = ari.evaluate(run_id)
        if decision.acceptable:
            break
        feedback = [r.message for r in decision.agent_fixable if not r.code.startswith("review:")]
        if not feedback:
            break  # what remains is for a human (or a reviewer that keeps failing)
    decision = ari.finish(run_id)
    if decision.disposition is Disposition.BLOCKED:
        notes.append("run left open: resolve the [human] items, then `daedalus finish`")
    return RunReport(run_id, decision, rounds, notes)
