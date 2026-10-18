"""The lifecycle (spec §11, §14, §17): build -> verify -> review -> finish.

Baseline (max_workers == 1, the policy default): one Builder edits the working
tree in place, the Proof Gate verifies the candidate, Daedalus launches any
reviews the risk tier or mode requires, and Ariadne derives the disposition.
Only reasons tagged agent-fixable are fed back to the Builder; anything needing
a human (approvals, attestations, policy problems) ends the loop with the run
left open and BLOCKED, so the human can act and `daedalus finish` later.

Bounded fan-out (max_workers > 1, opted into by policy): the first round asks
Brunel for a dependency graph of work packages. Ariadne validates it; packages
run concurrently in isolated worktrees up to the cap and integrate in
dependency order; then the same verify/review loop follows, with in-place
Builder rounds fixing whatever remains. An invalid, missing or single-package
plan falls back to the baseline. Only agent sessions run on worker threads:
every Ariadne call (and so every audit-log write) stays on the orchestrator
thread.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daedalus.adapters.base import Adapter, AgentRequest, AgentResult, require
from daedalus.core.acceptance import Decision
from daedalus.core.errors import BudgetExhausted, ContractError, DaedalusError, IntegrationError
from daedalus.core.state_machine import Disposition, ExecutionState, WorkerState
from daedalus.orchestration import prompts
from daedalus.orchestration.ariadne import SYSTEM, Ariadne
from daedalus.repository import snapshot
from daedalus.repository.candidate import diff_text

Notify = Callable[[str], None]

REVIEW_TIMEOUT_S = 1800.0
DRAIN_GRACE_S = 30.0  # how long an aborting fan-out round waits for each in-flight session
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
def check_commands(state: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The run's required checks a Builder may run (as command prefixes), and the ids of those it
    may not (no safe prefix, or `policy: {builder_runs_checks: false}`)."""
    granted, skipped = [], []
    for cid in state.contract.required_checks:
        d = state.policy.checks.get(cid)
        p = prompts.check_prefix(d.command) if (d is not None and state.policy.builder_runs_checks) else ""
        if p:
            granted.append(p)
        else:
            skipped.append(cid)
    return tuple(dict.fromkeys(granted)), tuple(skipped)


def delta_ok(state: Any) -> bool:
    """Whether re-reviews may be scoped to the change (`rereview: delta`, the default)."""
    return state.policy.rereview == "delta"


def _delta_basis(ari: Ariadne, state: Any, perspective: str, cand: Any, run_dir: Path):
    """(reviewed candidate, inter-diff, touched paths) for a delta review, or None when this
    perspective has no satisfied review in this contract version to extend, or the change
    since it cannot be proven from snapshots."""
    from daedalus.core.acceptance import review_satisfied

    for r in reversed(state.reviews):
        if r.perspective != perspective or r.contract_version != state.contract.contract_version:
            continue
        if r.candidate_id == cand.candidate_id or r.candidate_id not in state.candidates:
            continue
        # Extend only a review that holds under the strictest rule (launched, unexposed), so the
        # chain stays valid whatever tier the candidate ends up at.
        if not review_satisfied(state, perspective, r.candidate_id, True):
            continue
        old = state.candidates[r.candidate_id]["manifest"]
        try:
            inter, touched = snapshot.inter_diff(ari.root, run_dir, r.candidate_id, old, cand.manifest)
        except snapshot.NoDelta:
            return None
        return r.candidate_id, inter, touched
    return None


def refuse_unaffordable(state: Any, adapter: Adapter, what: str) -> None:
    """Refuse a launch that would be billed in cash when the run's cash budget is spent.

    The budget charges spend after the fact; checking first means a run is never billed
    for a session whose cost alone would reject it."""
    expected = getattr(adapter, "expected_cost_basis", None)  # adapters predating it cannot tell
    if expected is None or expected() != "cash":
        return
    budget = state.contract.budget
    # The next session is assumed to cost as much as the dearest billed one so far in this run.
    # A billing source the adapter cannot see before launch is still only caught afterwards.
    estimate = max((float(x.get("cost") or 0) for x in state.sessions if x.get("basis") == "cash"), default=0.0)
    if state.cash_unknown or state.cash_used + estimate > budget.max_cash or state.cash_used >= budget.max_cash:
        raise BudgetExhausted(
            f"{what} would be billed in cash, and this run's cash budget cannot cover it "
            f"({state.cash_used:.2f} spent of {budget.max_cash:.2f}; the last billed session cost up to "
            f"{estimate:.2f}). To allow billed reviews, raise "
            "`max_cash` in .daedalus.yml (`policy: {default_budget: {max_cash: 5}}`) and start a new run"
        )


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
    refuse_unaffordable(state, adapter, f"a {perspective} review")
    cand = ari.record_candidate(run_id, actor=SYSTEM, authored=False, label=f"review {perspective}")
    decision = ari.evaluate(run_id)
    prior = [
        {"id": f.finding_id, "severity": f.severity.value, "title": f.title, "required_change": f.required_change}
        for f in state.open_findings
        if f.perspective == perspective
    ]
    checks = {k: (v[0].value, v[1]) for k, v in decision.check_states.items()}
    run_dir = ari.state_dir / "runs" / run_id
    delta = _delta_basis(ari, state, perspective, cand, run_dir) if delta_ok(state) else None
    if delta is not None:
        old_id, inter, touched = delta
        prompt = prompts.delta_review_prompt(perspective, state.contract, inter, touched, old_id, checks, prior)
        notify(f"launching {perspective} delta review of candidate {cand.short} "
               f"({len(touched)} file(s) changed since {old_id[:12]})")
    else:
        again = any(r.perspective == perspective and r.contract_version == state.contract.contract_version
                    for r in state.reviews)
        prompt = prompts.review_prompt(perspective, state.contract, diff_text(ari.root, cand), checks, prior,
                                       first_pass=not again)
        notify(f"launching {perspective} review of candidate {cand.short}")
    snapshot.save(ari.root, run_dir, cand.candidate_id, cand.manifest)  # best effort: else the next is full
    res = adapter.run_agent(
        AgentRequest(
            run_id, f"review-{perspective}", perspective, prompt, ari.root, REVIEW_TIMEOUT_S, read_only=True,
            should_stop=ari.stop_check(run_id),
        )
    )
    ari.record_session(run_id, f"review-{perspective}", perspective, res)

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
            scope="delta" if delta is not None else "full",
            delta_from=delta[0] if delta is not None else None,
        )
    except (ContractError, DaedalusError) as exc:
        failed(f"invalid review result: {exc}")
        return None
    n = len(body.get("findings", []))
    notify(f"{perspective} review {rid}: {n} finding(s)")
    return rid


def arbitrate(
    ari: Ariadne, run_id: str, finding_id: str, adapter: Adapter, notify: Notify = _quiet
) -> str | None:
    """Launch Plato on a disputed finding. Returns `uphold`/`overrule`, or None when the
    session failed or returned nothing valid (recorded as a failed arbitration)."""
    state = ari._running(run_id)
    require(
        adapter.capabilities,
        {"launch_agent": True, "structured_results": True, "identity": "launched"},
        state.policy.allowed_degradations,
    )
    refuse_unaffordable(state, adapter, "an arbitration")
    f = state.findings.get(finding_id)
    if f is None or f.resolved_by is not None or not f.disputes:
        raise ContractError(f"{finding_id} is not an open, disputed finding")
    cand = ari.record_candidate(run_id, actor=SYSTEM, authored=False, label=f"arbitrate {finding_id}")
    if f.ruling and f.ruling.get("candidate_id") == cand.candidate_id:
        raise ContractError(
            f"{finding_id} was already ruled `{f.ruling.get('decision')}` on this candidate; change the code "
            "or have a human resolve it"
        )
    decision = ari.evaluate(run_id)

    def brief(x: Any) -> dict[str, str]:
        return {
            "id": x.finding_id, "severity": x.severity.value, "perspective": x.perspective, "title": x.title,
            "evidence": x.evidence, "required_change": x.required_change,
        }

    prompt = prompts.arbitration_prompt(
        state.contract,
        brief(f),
        f.disputes,
        [brief(x) for x in state.findings.values() if x.finding_id != finding_id and x.resolved_by is None],
        {k: (v[0].value, v[1]) for k, v in decision.check_states.items()},
        diff_text(ari.root, cand),
    )
    notify(f"Plato is arbitrating {finding_id}")
    res = adapter.run_agent(
        AgentRequest(
            run_id, f"arbitrate-{finding_id}", "plato", prompt, ari.root, REVIEW_TIMEOUT_S, read_only=True,
            should_stop=ari.stop_check(run_id),
        )
    )
    ari.record_session(run_id, f"arbitrate-{finding_id}", "plato", res)

    def failed(why: str) -> None:
        ari.record_failed_arbitration(run_id, finding_id, why)
        notify(f"arbitration of {finding_id} failed: {why}")

    if res.status != "COMPLETED":
        failed(res.error or res.status)
        return None
    if ari.capture(ari.state(run_id)).candidate_id != cand.candidate_id:
        failed("the working tree changed during a read-only arbitration")
        return None
    arbiter = f"agent:plato:{(res.session_id or 'session')[:8]}"
    try:
        resolved = ari.record_arbitration(
            run_id, finding_id, arbiter=arbiter, ruling=res.structured or {}, candidate_id=cand.candidate_id
        )
    except (ContractError, DaedalusError) as exc:
        failed(f"invalid ruling: {exc}")
        return None
    ruling = ari.state(run_id).findings[finding_id].ruling or {}
    notify(f"Plato ruled `{ruling.get('decision')}` on {finding_id}" + (" (resolved by policy)" if resolved else ""))
    return ruling.get("decision")


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
    if task_id in state.workers:  # a resumed run starts counting rounds again
        task_id = f"build-{round_no}.{len(state.workers) + 1}"
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
    runnable, skipped = check_commands(state)
    try:
        res = adapter.run_agent(
            AgentRequest(
                run_id,
                task_id,
                "brunel",
                prompts.builder_prompt(state.contract, feedback, round_no, runnable, skipped),
                ari.root,
                BUILD_TIMEOUT_S,
                should_stop=ari.stop_check(run_id),
                run_commands=runnable,
            )
        )
    except Exception as exc:
        ari.worker_finished(run_id, task_id, status="FAILED", note=f"adapter error: {exc}")
        raise
    ari.record_session(run_id, task_id, "brunel", res)
    status = res.status if res.status in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED") else "FAILED"
    rec = ari.worker_finished(
        run_id,
        task_id,
        status=status,
        proposal={"summary": (res.structured or {}).get("summary", ""), "output_tail": res.output[-2000:]},
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


PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _plan_packages(body: Any, round_no: int, taken: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]] | None:
    """Brunel's plan as Ariadne work packages, ids namespaced by round.

    None for anything malformed. Ids become directory names, so they must match
    PACKAGE_ID (no separators, no `..`); lists must really be lists of strings.
    """
    if not isinstance(body, dict) or not isinstance(body.get("packages"), list) or not body["packages"]:
        return None

    def str_list(v: Any) -> list[str] | None:
        return v if isinstance(v, list) and all(isinstance(x, str) for x in v) else None

    out = []
    for p in body["packages"]:
        if not isinstance(p, dict):
            return None
        pid, owned, deps = p.get("id"), str_list(p.get("owned_paths")), str_list(p.get("depends_on", []))
        if not isinstance(pid, str) or not PACKAGE_ID.match(pid) or ".." in pid or not owned or deps is None:
            return None
        if not all(d and PACKAGE_ID.match(d) for d in deps):
            return None
        out.append(
            {
                "task_id": f"r{round_no}-{pid}",
                "description": str(p.get("description", "")),
                "owned_paths": owned,
                "depends_on": [f"r{round_no}-{d}" for d in deps],
                "deadline_s": BUILD_TIMEOUT_S,
            }
        )
    # A resumed run counts rounds from 1 again: move to a fresh namespace rather than reuse ids.
    ns, n = f"r{round_no}", 2
    while any(p["task_id"].replace(f"r{round_no}-", f"{ns}-", 1) in taken for p in out):
        ns, n = f"r{round_no}.{n}", n + 1
    if ns != f"r{round_no}":
        for p in out:
            p["task_id"] = p["task_id"].replace(f"r{round_no}-", f"{ns}-", 1)
            p["depends_on"] = [d.replace(f"r{round_no}-", f"{ns}-", 1) for d in p["depends_on"]]
    return out


def fan_out_round(
    ari: Ariadne, run_id: str, adapter: Adapter, round_no: int, notify: Notify = _quiet
) -> dict[str, Any] | None:
    """Plan, validate, run and integrate parallel packages.

    Returns None when there is nothing to parallelize (an unusable, rejected or
    single-package plan, or a planner that edited files): the caller then runs one
    in-place Builder. Otherwise returns the integrated packages and the failures to
    feed back. Every package ends in a recorded state, whatever goes wrong.
    """
    state = ari._running(run_id)
    cap = state.contract.budget.max_workers
    notify(f"round {round_no}: Brunel is planning up to {cap} parallel packages")
    before = ari.capture(state).candidate_id
    plan_req = AgentRequest(
        run_id, f"plan-{round_no}", "brunel", prompts.plan_prompt(state.contract, cap), ari.root,
        REVIEW_TIMEOUT_S, read_only=True, should_stop=ari.stop_check(run_id),
    )
    try:
        res = adapter.run_agent(plan_req)
    except Exception as exc:  # noqa: BLE001 — planning is optional; a crashed planner means "no plan"
        notify(f"planner failed ({exc}); a single Builder works in place")
        return None
    ari.record_session(run_id, f"plan-{round_no}", "brunel", res)
    if ari.capture(ari.state(run_id)).candidate_id != before:
        notify("the planner changed the working tree; ignoring its plan")
        return None
    taken = set(ari.state(run_id).workers)
    packages = _plan_packages(res.structured, round_no, taken) if res.status == "COMPLETED" else None
    if packages is None or len(packages) == 1:
        notify("nothing to parallelize; a single Builder works in place")
        return None
    try:
        task_ids = ari.plan(run_id, packages, actor=SYSTEM)
    except IntegrationError as exc:
        notify(f"plan rejected ({exc}); a single Builder works in place")
        return None

    deps = {p["task_id"]: set(p["depends_on"]) for p in packages}
    pending, failed, integrated, failures = list(task_ids), set(), set(), []
    running: dict[Future[AgentResult], str] = {}

    def fail(tid: str, why: str) -> None:
        failed.add(tid)
        failures.append(f"{tid}: {why}")

    def finish(tid: str, out: AgentResult, *, integrate: bool = True) -> None:
        ok = ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED")
        body = out.structured or {}
        status = out.status if out.status in ok else "FAILED"
        blocked = status == "COMPLETED" and body.get("status") == "blocked"
        ari.record_session(run_id, tid, "brunel", out)
        rec = ari.worker_finished(
            run_id,
            tid,
            status="FAILED" if blocked else status,
            proposal={"summary": body.get("summary", ""), "output_tail": out.output[-2000:]},
            note=f"worker reported blocked: {body.get('blocker', '')}" if blocked else out.error or "",
            session_id=out.session_id,
        )
        if rec.state is not WorkerState.COMPLETED:
            fail(tid, f"{rec.state.value.lower()} ({rec.note})")
            return
        if not integrate:
            fail(tid, "completed, but the round was aborted before integration")
            return
        try:
            ari.integrate(run_id, tid)
            integrated.add(tid)
            notify(f"integrated {tid}")
        except IntegrationError as exc:
            fail(tid, str(exc))

    stop = ari.stop_check(run_id)
    runnable, skipped = check_commands(state)
    pool = ThreadPoolExecutor(max_workers=cap, thread_name_prefix="daedalus-worker")
    aborted = False
    try:
        while pending or running:
            progressed = True
            while progressed:  # cancel to a fixpoint: a cancellation can doom further dependents
                progressed = False
                for tid in list(pending):
                    if deps[tid] & failed:
                        pending.remove(tid)
                        ari._worker_transition(run_id, tid, WorkerState.CANCELLED, "a prerequisite failed")
                        fail(tid, "not started because a prerequisite failed")
                        progressed = True
            for tid in list(pending):
                if len(running) >= cap or not deps[tid] <= integrated:
                    continue
                pending.remove(tid)
                try:
                    w = ari.dispatch(run_id, tid, principal=f"agent:worker:{tid}")
                except DaedalusError as exc:  # this package cannot start; the others go on
                    ari._worker_transition(run_id, tid, WorkerState.CANCELLED, f"dispatch failed: {exc}")
                    fail(tid, f"dispatch failed: {exc}")
                    continue
                if not w.workspace:  # never let a parallel worker loose in the authoritative tree
                    finish(tid, AgentResult("FAILED", error="no isolated workspace"), integrate=False)
                    continue
                req = AgentRequest(
                    run_id, tid, "brunel", prompts.worker_prompt(state.contract, w.package, runnable, skipped),
                    Path(w.workspace), BUILD_TIMEOUT_S, should_stop=stop, run_commands=runnable,
                )
                running[pool.submit(adapter.run_agent, req)] = tid
                notify(f"dispatched {tid} ({len(running)}/{cap} running)")
            if not running:
                break  # nothing in flight and nothing dispatchable
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            # Plan order, not completion-set order: simultaneous finishes integrate (and
            # land in the audit log) the same way on every run.
            for fut in sorted(done, key=lambda f: task_ids.index(running[f])):
                tid = running.pop(fut)
                try:
                    out = fut.result()
                except Exception as exc:  # noqa: BLE001 — an adapter crash fails that package only
                    out = AgentResult("FAILED", error=f"adapter error: {exc}")
                finish(tid, out)
    except BaseException:
        aborted = True
        raise
    finally:
        # Whatever happened, no package is left active or pending. In-flight sessions
        # are drained into recorded outcomes (if the round is aborting: a bounded wait per
        # session and never integration; a session that outlives the wait keeps running
        # until its own timeout but is recorded CANCELLED), and what never started is
        # cancelled.
        for fut, tid in list(running.items()):
            try:
                out = fut.result(timeout=DRAIN_GRACE_S if aborted else None)
            except FutureTimeout:
                out = AgentResult("CANCELLED", error="round aborted; session did not stop in time")
            except Exception as exc:  # noqa: BLE001
                out = AgentResult("FAILED", error=f"adapter error: {exc}")
            try:
                finish(tid, out, integrate=not aborted)
            except Exception:  # noqa: BLE001 — keep draining; the original error propagates
                pass
        for tid in pending:
            if ari.state(run_id).workers[tid].state is WorkerState.PENDING:
                ari._worker_transition(run_id, tid, WorkerState.CANCELLED, "fan-out round ended")
                fail(tid, "never started")
        pool.shutdown(wait=not aborted, cancel_futures=aborted)
    return {"integrated": sorted(integrated), "failures": failures}


def _arbitrate_disputes(
    ari: Ariadne, run_id: str, built: dict[str, Any], adapter: Adapter, notify: Notify
) -> list[str]:
    """Record and arbitrate the disputes a Builder reported. Malformed entries are
    ignored, findings already ruled on for this candidate are skipped, and a refusal
    is noted rather than aborting the run."""
    notes: list[str] = []
    disputes = built.get("disputes")
    if not isinstance(disputes, list):
        return notes
    for d in disputes:
        if not isinstance(d, dict):
            continue
        fid, why = d.get("finding"), d.get("reason")
        if not isinstance(fid, str) or not isinstance(why, str) or not why.strip():
            continue
        f = ari.state(run_id).findings.get(fid)
        if f is None or f.resolved_by is not None:
            continue
        current = ari.capture(ari.state(run_id)).candidate_id
        if f.ruling and f.ruling.get("candidate_id") == current:
            notes.append(f"{fid}: already ruled `{f.ruling.get('decision')}` on this candidate; dispute ignored")
            continue
        try:
            ari.dispute(run_id, fid, actor=built["_principal"], reason=why)
            arbitrate(ari, run_id, fid, adapter, notify)
        except DaedalusError as exc:
            notes.append(f"arbitration of {fid} skipped: {exc}")
    return notes


def drive(
    ari: Ariadne, run_id: str, *, adapter: Adapter, max_rounds: int = 3, notify: Notify = _quiet
) -> RunReport:
    notes: list[str] = []
    feedback: list[str] = []
    rounds = 0
    accepted_before_followup: Any = None  # the acceptable candidate a follow-up round started from
    followup_round = 0
    for round_no in range(1, max_rounds + 1):
        if ari.state(run_id).execution is not ExecutionState.RUNNING:
            break  # cancelled (or finished) elsewhere: no further rounds
        if accepted_before_followup is not None and round_no > followup_round + 1:
            break  # the follow-up gets exactly one round; whatever it left is undone below
        rounds = round_no
        try:
            fan = None
            if round_no == 1 and ari.state(run_id).contract.budget.max_workers > 1:
                fan = fan_out_round(ari, run_id, adapter, round_no, notify)
            if fan is not None and fan["failures"]:
                feedback = ["parallel packages did not all land; finish the work in place:", *fan["failures"]]
                continue
            built = (
                {"_state": WorkerState.COMPLETED}
                if fan is not None
                else build_round(ari, run_id, adapter, round_no, feedback, notify)
            )
        except DaedalusError as exc:  # budget, concurrency, or capability refusal
            notes.append(str(exc))
            break
        if built.get("status") == "blocked":
            why = str(built.get("blocker") or built.get("summary") or "builder reported it cannot proceed")
            if accepted_before_followup is not None:  # an optional pass: end it, never block the run
                notes.append(f"the follow-up builder stopped: {why}")
                break
            ari.raise_blocker(run_id, why, actor=built["_principal"])
            notes.append(f"builder blocked: {why}")
            break
        if built["_state"] is WorkerState.COMPLETED and "_principal" in built:
            notes += _arbitrate_disputes(ari, run_id, built, adapter, notify)
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
            # One fix round for non-blocking findings. It never costs an acceptance: the acceptable
            # candidate is snapshotted first and restored if the run ends any other way.
            followup = ari.followup_candidates(run_id) if round_no < max_rounds else []
            cand = ari.capture(ari.state(run_id)) if followup else None
            if not followup or not snapshot.save(ari.root, _run_dir(ari, run_id), cand.candidate_id, cand.manifest):
                break
            accepted_before_followup, followup_round = cand, round_no
            ari.request_followup(run_id, [f.finding_id for f in followup], cand.candidate_id)
            notify(f"round {round_no}: acceptable; one follow-up round for {len(followup)} non-blocking finding(s)")
            feedback = followup_feedback(followup)
            continue
        feedback = [r.message for r in decision.agent_fixable if not r.code.startswith("review:")]
        if not feedback:
            break  # what remains is for a human (or a reviewer that keeps failing)
    if (
        accepted_before_followup is not None
        and ari.state(run_id).execution is ExecutionState.RUNNING
        and not ari.evaluate(run_id).acceptable
    ):
        try:
            notes += restore_followup(ari, run_id, notify)
        except DaedalusError as exc:
            notes.append(str(exc))
    decision = ari.finish(run_id)
    if decision.disposition is Disposition.BLOCKED:
        notes.append(left_open_note(decision, rounds))
    return RunReport(run_id, decision, rounds, notes)


def _run_dir(ari: Ariadne, run_id: str) -> Path:
    return ari.state_dir / "runs" / run_id


def restore_followup(ari: Ariadne, run_id: str, notify: Notify = _quiet) -> list[str]:
    """Undo a follow-up round: put back the candidate that was acceptable when it started.
    Identity is content, so its reviews hold again; checks are re-run (the latest evidence was
    for the follow-up) and findings a delta review raised against the undone change are moot."""
    state = ari._running(run_id)
    cid = state.followup_from
    if not cid or cid not in state.candidates:
        raise DaedalusError("this run has no follow-up round to undo")
    old = state.candidates[cid]["manifest"]
    current = ari.capture(state)
    if current.candidate_id == cid:
        return [f"candidate {cid[:12]} is already in place"]
    run_dir = _run_dir(ari, run_id)
    # Keep what is being undone, outside the snapshots (which are pruned at the end of the run).
    # The diff is for reading; the files (each hash-checked) are the exact content.
    kept = run_dir / f"followup-undone-{current.short}.diff"
    files = run_dir / f"followup-undone-{current.short}"
    if not snapshot.copy_files(ari.root, files, current.manifest):
        raise DaedalusError("the follow-up's files could not be kept byte-exact, so they were not undone")
    try:
        undone, _ = snapshot.inter_diff(ari.root, run_dir, cid, old, current.manifest)
        kept.write_text(undone, encoding="utf-8")
    except (snapshot.NoDelta, OSError) as exc:
        raise DaedalusError(f"the follow-up's changes could not be saved, so they were not undone ({exc})") from exc
    try:
        restored = snapshot.restore(ari.root, run_dir, cid, old, current.manifest)
    except snapshot.PartialRestore as exc:
        raise DaedalusError(f"the candidate before the follow-up ({cid[:12]}) could not be restored and "
                            f"the working tree may be partly rewritten: {', '.join(exc.paths)} "
                            f"(the follow-up's files are in {files / 'files'}, its changes in {kept})") from exc
    if not restored:
        raise DaedalusError(f"the candidate before the follow-up ({cid[:12]}) could not be restored; "
                            f"the working tree was left as it was (its changes are also in {kept})")
    if ari.capture(ari.state(run_id)).candidate_id != cid:
        raise DaedalusError(f"restoring {cid[:12]} did not reproduce it exactly; the undone changes are in {kept}")
    ari.record_revert(run_id, cid)
    ari.record_candidate(run_id, actor=SYSTEM, authored=False, label="restored after follow-up")
    notify(f"the follow-up round did not end acceptable; restored candidate {cid[:12]}")
    ari.verify(run_id)
    rel = kept.relative_to(ari.root).as_posix() if ari.root in kept.parents else str(kept)
    return [f"the follow-up round's changes were undone and candidate {cid[:12]} (acceptable before it) "
            f"was restored; the undone changes are kept in {rel} (the files themselves beside it)"]


def followup_feedback(findings: list[Any]) -> list[str]:
    head = (
        "The candidate is acceptable, but reviewers raised non-blocking findings. In this one follow-up "
        "round, fix each one that is real and cheap; dispute one that is mistaken (with evidence). "
        "Anything left open does not block acceptance."
    )
    return [head] + [
        f"{f.finding_id} [{f.severity.value}] {f.title}. {f.required_change}".strip() for f in findings
    ]


def left_open_note(decision: Decision, rounds: int) -> str:
    """What a BLOCKED run is waiting for, by who can act on it."""
    who = {r.fixable_by for r in decision.reasons}
    if "human" in who:
        return "run left open: resolve the [human] items, then `daedalus finish`"
    if "agent" in who:
        return (
            f"run left open after {rounds} round(s): the [agent] items remain; fix them "
            "(or `daedalus run` again with more rounds), then `daedalus finish`"
        )
    return (
        "run left open: the [system] items remain (a required review that could not run: check the adapter "
        "and budget, then `daedalus verify`; after an interruption, `daedalus reconcile`), then `daedalus finish`"
    )
