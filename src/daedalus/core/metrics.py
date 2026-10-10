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
from daedalus.core.state_machine import Disposition

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
    input_tokens: int = 0  # uncached + cache read + cache creation, over those sessions
    output_tokens: int = 0

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
        return {**asdict(self), "first_pass": self.first_pass}


def _tokens(session: dict[str, Any], key: str) -> int:
    try:
        return max(int(session.get(key) or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


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
    )


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
    )


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
