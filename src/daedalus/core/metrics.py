"""Measurements derived from the audit log (spec §17, §19).

Metrics are a read-only fold over recorded events: they never write to the
log and never consult live state, so the same log always yields the same
numbers. They exist to judge the single-worker baseline before fan-out is
enabled, and to keep later automation honest: optimize verified outcomes at
acceptable cost, not first-pass acceptance or finding counts alone.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from daedalus.audit.store import Event, EventStore
from daedalus.core import run as ev
from daedalus.core.run import replay
from daedalus.core.state_machine import WORKER_ACTIVE, Disposition, WorkerState

# Events that record a human *decision*. Derived events a command emits as a
# consequence (finalizing the candidate, recording the disposition) are not
# separate interventions.
HUMAN_DECISIONS = frozenset(
    {
        ev.CRITERION_ATTESTED,
        ev.APPROVAL_RECORDED,
        ev.CONTRACT_AMENDED,
        ev.RISK_EXCEPTION,
        ev.FINDING_RESOLVED,
        ev.BLOCKER_RESOLVED,
        ev.VIOLATION_RESOLVED,
        ev.UNRESOLVABLE,
        ev.CANCEL_REQUESTED,
        ev.ACTION_RECONCILED,
    }
)


@dataclass(frozen=True)
class RunMetrics:
    run_id: str
    objective: str
    mode: str
    disposition: str  # ACCEPTED | REJECTED | CANCELLED, or "open" (no final disposition yet)
    wall_seconds: float | None  # creation to final disposition; None while open
    cost: float
    attempts: int
    candidates_verified: int  # distinct candidates that received evidence
    check_failures: int  # mandatory evidence that was not PASS
    reviews: int
    failed_reviews: int
    findings: dict[str, int] = field(default_factory=dict)
    blocking_findings: int = 0  # findings at a severity the run's pinned policy treats as blocking
    stop_blocks: int = 0
    human_interventions: int = 0  # human decisions (HUMAN_DECISIONS), not every human-attributed event
    recoveries: int = 0
    disputes: int = 0
    arbitrations: int = 0
    cash: float = 0.0  # part of `cost` actually billed; the rest is API-equivalent estimate
    sessions: int = 0  # launched sessions that reported usage
    unknown_cost_sessions: int = 0  # stopped (timed out, cancelled) before reporting a cost
    delta_reviews: int = 0  # re-reviews scoped to the change since the last review
    late_findings: int = 0  # every finding raised by a full re-review (after the perspective's first full one)
    followup_rounds: int = 0  # one-time fix rounds offered for non-blocking findings
    input_tokens: int = 0  # uncached + cache read + cache creation, over those sessions
    output_tokens: int = 0
    # Evidence reused from earlier runs (`reused_from` events): what it saved, by the source's own record.
    cache_check_hits: int = 0
    cache_review_hits: int = 0
    cache_seconds_saved: float = 0.0
    cache_cost_saved: float = 0.0
    cache_cash_saved: float = 0.0  # part of cache_cost_saved that was billed money
    # Review spend by perspective: {perspective: {"cost": charged for its launched sessions, "findings": raised}}.
    review_spend: dict[str, dict[str, float]] = field(default_factory=dict)
    # Parallel packages (fan-out): what running them side by side saved, and what integrating them cost.
    parallel_workers: int = 0
    parallel_serial_s: float = 0.0  # the workers' sessions end to end, as if run one after another
    parallel_span_s: float = 0.0  # first dispatch to last worker finished
    integration_s: float = 0.0  # from each worker finishing to its result being integrated
    integration_failures: int = 0  # completed workers whose result could not be integrated

    @property
    def parallel_saved_s(self) -> float:
        return round(self.parallel_serial_s - self.parallel_span_s, 3)

    @property
    def cache_hits(self) -> int:
        return self.cache_check_hits + self.cache_review_hits

    @property
    def finished(self) -> bool:
        return self.disposition in ("ACCEPTED", "REJECTED", "CANCELLED")

    @property
    def accepted(self) -> bool:
        return self.disposition == "ACCEPTED"

    @property
    def first_pass(self) -> bool:
        """Accepted on the first verified candidate with no failed check and no blocking finding."""
        return (
            self.accepted
            and self.candidates_verified <= 1
            and self.check_failures == 0
            and self.blocking_findings == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "first_pass": self.first_pass, "cache_hits": self.cache_hits,
                "parallel_saved_s": self.parallel_saved_s}


def _tokens(session: dict[str, Any], key: str) -> int:
    try:
        return max(int(session.get(key) or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _reuse_savings(events: list[Event]) -> dict[str, Any]:
    """Hits and savings from events that name the earlier evidence they reused. Savings are the
    source's recorded duration and cost, so they are only as honest as that record."""
    checks = reviews = 0
    seconds = cost = cash = 0.0
    for e in events:
        if e.type == ev.EVIDENCE_RECORDED:
            src = e.payload.get("reused_from")
        elif e.type == ev.REVIEW_RECORDED:
            src = e.payload.get("review", {}).get("reused_from")
        else:
            continue
        if not isinstance(src, dict):
            continue
        checks += e.type == ev.EVIDENCE_RECORDED
        reviews += e.type == ev.REVIEW_RECORDED
        try:
            s, c = max(float(src.get("duration_s") or 0), 0.0), max(float(src.get("cost") or 0), 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        seconds += s if s == s else 0.0  # NaN-safe
        cost += c if c == c else 0.0
        cash += c if src.get("basis") == "cash" and c == c else 0.0
    return {"cache_check_hits": checks, "cache_review_hits": reviews, "cache_seconds_saved": round(seconds, 3),
            "cache_cost_saved": round(cost, 4), "cache_cash_saved": round(cash, 4)}


def run_metrics(events: Iterable[Event]) -> RunMetrics:
    events = list(events)
    state = replay(events)
    end = next((e.ts for e in events if e.type == ev.DISPOSITION), None)
    humans = sum(1 for e in events if e.type in HUMAN_DECISIONS and e.actor.startswith("human:"))
    recoveries = sum(1 for e in events if e.type == ev.RECOVERY)
    mandatory = [e for e in state.evidence if e.mandatory]
    return RunMetrics(
        run_id=state.run_id,
        objective=state.contract.objective,
        mode=state.mode,
        disposition=state.disposition.value if state.disposition else "open",
        wall_seconds=None if end is None else end - state.created_at,
        cost=round(state.cost_used, 4),
        attempts=state.attempts_used,
        candidates_verified=len({e.candidate_id for e in state.evidence}),
        check_failures=sum(1 for e in mandatory if e.result != "PASS"),
        reviews=len(state.reviews),
        failed_reviews=len(state.failed_reviews),
        findings=dict(Counter(f.severity.value for f in state.findings.values())),
        blocking_findings=sum(
            1 for f in state.findings.values() if f.severity.value in state.policy.blocking_severities
        ),
        stop_blocks=state.stop_blocks,
        human_interventions=humans,
        recoveries=recoveries,
        disputes=sum(len(f.disputes) for f in state.findings.values()),
        arbitrations=sum(1 for e in events if e.type == ev.ARBITRATION_RECORDED),
        cash=round(state.cash_used, 4),
        sessions=sum(1 for x in state.sessions if "dropped" not in x and not x.get("cost_unknown")),
        input_tokens=sum(
            _tokens(x, k)
            for x in state.sessions
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        ),
        output_tokens=sum(_tokens(x, "output_tokens") for x in state.sessions),
        unknown_cost_sessions=sum(1 for x in state.sessions if x.get("cost_unknown")),
        delta_reviews=sum(1 for r in state.reviews if r.scope == "delta"),
        late_findings=_late_findings(state),
        followup_rounds=state.followup_rounds,
        review_spend=_review_spend(state, events),
        **_parallel(state, events),
        **_reuse_savings(events),
    )


def _review_spend(state: Any, events: list[Event]) -> dict[str, dict[str, float]]:
    """What each perspective's launched review sessions were charged, and the findings it raised.
    Reused reviews cost nothing here; plan reviews (advice, no findings) are left out."""
    out: dict[str, dict[str, float]] = {}
    for e in events:
        if e.type != ev.COST_CHARGED:
            continue
        task = str(e.payload.get("note", "")).rsplit(" ", 1)[-1]
        if task.startswith("review-"):
            row = out.setdefault(task[len("review-"):], {"cost": 0.0, "findings": 0})
            row["cost"] = round(row["cost"] + float(e.payload.get("cost", 0.0)), 4)
    for f in state.findings.values():
        out.setdefault(f.perspective, {"cost": 0.0, "findings": 0})["findings"] += 1
    return out


def _parallel(state: Any, events: list[Event]) -> dict[str, Any]:
    """Isolated (fan-out) packages only: in-place builders are not parallel work."""
    isolated = {t for t, w in state.workers.items() if not w.package.get("in_place")}
    start: dict[str, float] = {}
    end: dict[str, float] = {}
    integrated: dict[str, float] = {}
    for e in events:
        tid = e.payload.get("task_id")
        if tid not in isolated:
            continue
        if e.type == ev.WORKER_TRANSITION:
            to = e.payload.get("to")
            if to == WorkerState.DISPATCHED.value:
                start.setdefault(tid, e.ts)
            elif to not in (s.value for s in WORKER_ACTIVE) and to != WorkerState.PENDING.value and tid in start:
                end[tid] = e.ts
        elif e.type == ev.WORKER_INTEGRATED:
            integrated[tid] = e.ts
    ran = [t for t in start if t in end]
    if not ran:
        return {}
    serial = sum(end[t] - start[t] for t in ran)
    span = max(end[t] for t in ran) - min(start[t] for t in ran)
    done = [t for t in ran if state.workers[t].state is WorkerState.COMPLETED]
    return {
        "parallel_workers": len(ran),
        "parallel_serial_s": round(serial, 3),
        "parallel_span_s": round(span, 3),
        "integration_s": round(sum(integrated[t] - end[t] for t in done if t in integrated), 3),
        "integration_failures": sum(1 for t in done if t not in integrated),
    }


def _late_findings(state: Any) -> int:
    """Every finding raised by a *full* review of a perspective after that perspective's first
    full review in the run. A full re-review re-reads code an earlier one covered, so what it
    finds is the cost of reviews that do not find everything at once. Delta reviews see only
    new changes, so what they raise is not counted."""
    first: set[str] = set()
    late = 0
    for r in state.reviews:
        if r.scope == "delta":
            continue
        if r.perspective in first:
            late += len(r.finding_ids)
        first.add(r.perspective)
    return late


@dataclass(frozen=True)
class Aggregate:
    runs: int
    open: int
    accepted: int
    rejected: int
    cancelled: int
    acceptance_rate: float | None  # accepted / finished
    first_pass_rate: float | None  # first-pass accepted / finished
    median_latency_accepted_s: float | None
    cost_total: float  # every run, including open ones
    cost_per_accepted: float | None  # spend of finished runs (accepted, rejected, cancelled) per accepted change
    rework_per_accepted: float | None  # extra verified candidates per accepted change
    manual_interventions: int
    recoveries: int
    disputes: int = 0
    arbitrations: int = 0
    cash_total: float = 0.0  # billed money across every run; cost_total includes it
    reviews: int = 0
    delta_reviews: int = 0
    late_findings: int = 0
    reviews_per_accepted: float | None = None  # review sessions per accepted change (lower is cheaper)
    cache_hits: int = 0  # checks and reviews reused from earlier runs instead of re-run
    cache_check_hits: int = 0
    cache_review_hits: int = 0
    cache_seconds_saved: float = 0.0
    cache_cost_saved: float = 0.0
    cache_cash_saved: float = 0.0
    # {perspective: {"cost", "findings", "cost_per_finding"}} over every run; cost_per_finding is None
    # when a perspective found nothing (its spend bought no findings, which is information too).
    review_spend: dict[str, dict[str, float | None]] = field(default_factory=dict)
    parallel_runs: int = 0
    parallel_saved_s: float = 0.0  # serial minus span, over every fan-out round
    integration_s: float = 0.0
    integration_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def aggregate(metrics: Iterable[RunMetrics]) -> Aggregate:
    ms = list(metrics)
    finished = [m for m in ms if m.finished]
    accepted = [m for m in finished if m.accepted]
    count = Counter(m.disposition for m in finished)

    def rate(n: int) -> float | None:
        return round(n / len(finished), 4) if finished else None

    latencies = [m.wall_seconds for m in accepted if m.wall_seconds is not None]
    cost_total = round(sum(m.cost for m in ms), 4)
    return Aggregate(
        runs=len(ms),
        open=len(ms) - len(finished),
        accepted=len(accepted),
        rejected=count[Disposition.REJECTED.value],
        cancelled=count[Disposition.CANCELLED.value],
        acceptance_rate=rate(len(accepted)),
        first_pass_rate=rate(sum(1 for m in accepted if m.first_pass)),
        median_latency_accepted_s=statistics.median(latencies) if latencies else None,
        cost_total=cost_total,
        cost_per_accepted=round(sum(m.cost for m in finished) / len(accepted), 4) if accepted else None,
        rework_per_accepted=(
            round(sum(max(m.candidates_verified - 1, 0) for m in accepted) / len(accepted), 4) if accepted else None
        ),
        manual_interventions=sum(m.human_interventions for m in ms),
        recoveries=sum(m.recoveries for m in ms),
        disputes=sum(m.disputes for m in ms),
        arbitrations=sum(m.arbitrations for m in ms),
        cash_total=round(sum(m.cash for m in ms), 4),
        reviews=sum(m.reviews for m in ms),
        delta_reviews=sum(m.delta_reviews for m in ms),
        late_findings=sum(m.late_findings for m in ms),
        reviews_per_accepted=round(sum(m.reviews for m in accepted) / len(accepted), 4) if accepted else None,
        cache_hits=sum(m.cache_hits for m in ms),
        cache_check_hits=sum(m.cache_check_hits for m in ms),
        cache_review_hits=sum(m.cache_review_hits for m in ms),
        cache_seconds_saved=round(sum(m.cache_seconds_saved for m in ms), 3),
        cache_cost_saved=round(sum(m.cache_cost_saved for m in ms), 4),
        cache_cash_saved=round(sum(m.cache_cash_saved for m in ms), 4),
        review_spend=_spend_totals(ms),
        parallel_runs=sum(1 for m in ms if m.parallel_workers),
        parallel_saved_s=round(sum(m.parallel_saved_s for m in ms), 3),
        integration_s=round(sum(m.integration_s for m in ms), 3),
        integration_failures=sum(m.integration_failures for m in ms),
    )


def _spend_totals(ms: list[RunMetrics]) -> dict[str, dict[str, float | None]]:
    totals: dict[str, dict[str, float]] = {}
    for m in ms:
        for p, row in m.review_spend.items():
            t = totals.setdefault(p, {"cost": 0.0, "findings": 0})
            t["cost"] = round(t["cost"] + row["cost"], 4)
            t["findings"] += int(row["findings"])
    return {
        p: {**t, "cost_per_finding": round(t["cost"] / t["findings"], 4) if t["findings"] else None}
        for p, t in sorted(totals.items())
    }


def collect(store: EventStore) -> tuple[list[RunMetrics], dict[str, str]]:
    """Metrics for every run in the store. A run whose log cannot be replayed is
    reported as an error rather than hiding every other run's numbers."""
    runs: list[RunMetrics] = []
    errors: dict[str, str] = {}
    for rid in store.run_ids():
        try:
            runs.append(run_metrics(store.events(rid)))
        except Exception as exc:  # noqa: BLE001 — read-only: any unreadable run is reported, never fatal
            errors[rid] = f"{type(exc).__name__}: {exc}"
    return runs, errors
