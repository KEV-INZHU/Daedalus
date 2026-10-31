"""The report's data model and `build_report`.

Everything is read from `Ariadne.state`, `evaluate` and `core.metrics`; nothing is re-run. Rows hold
plain strings, numbers, lists and dicts only, so a report survives a JSON round trip unchanged. Text
that an agent or reviewer wrote (titles, notes, summaries) is stored raw: escaping is the renderers' job.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from daedalus.core import run as ev
from daedalus.core.acceptance import is_moot
from daedalus.core.errors import AuditIntegrityError, DaedalusError
from daedalus.core.metrics import run_metrics

SCHEMA = 1
FOOTER = "Generated locally from `.daedalus/daedalus.db`; CI re-verifies checks independently."
SEVERITIES = ("BLOCKER", "MAJOR", "MINOR", "ADVISORY")
HUMAN_PREFIX = "human:"


@dataclass
class CriterionRow:
    criterion_id: str
    description: str
    checks: list[str] = field(default_factory=list)


@dataclass
class CheckRow:
    check_id: str
    command: list[str]
    result: str  # PASS | FAIL | ERROR, as recorded
    state: str  # the check's state now (PASS, STALE, ...) on the last record of each check; "" on earlier ones
    duration_s: float
    started_at: float
    candidate_id: str
    verifier_id: str
    mandatory: bool
    reused: bool = False
    reused_from: dict[str, Any] | None = None  # {run_id, seq, hash, duration_s, cost, basis} of the source


@dataclass
class ReviewRow:
    review_id: str
    perspective: str
    scope: str  # full | delta
    reviewer: str
    model: str  # "" when the session did not report one
    candidate_id: str
    reused: bool
    counts: dict[str, int] = field(default_factory=dict)  # severity -> number of findings
    reused_from: dict[str, Any] | None = None
    summary: str = ""


@dataclass
class FindingRow:
    finding_id: str
    title: str
    severity: str
    status: str  # open | resolved | disputed | moot
    perspective: str
    review_id: str
    resolution: str = ""
    resolved_by: str = ""
    disputes: int = 0
    ruling: str = ""  # an arbitration ruling's decision, if any


@dataclass
class SignalRow:
    kind: str
    path: str
    detail: str


@dataclass
class HumanAct:
    kind: str  # approval | attestation | risk_exception | resolution
    actor: str
    at: float
    detail: str


@dataclass
class ReasonRow:
    code: str
    message: str
    fixable_by: str


@dataclass
class TimelineRow:
    at: float
    actor: str
    type: str
    summary: str


@dataclass
class Cost:
    total: float = 0.0
    cash: float = 0.0
    api_equivalent: float = 0.0  # total - cash: an estimate, not money billed
    cache_hits: int = 0
    saved_total: float = 0.0
    saved_cash: float = 0.0
    saved_seconds: float = 0.0
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cash_unknown: bool = False  # a billed session ended without reporting its cost


@dataclass
class Audit:
    verified: bool
    head_hash: str
    events: int  # events in the whole log
    run_events: int
    error: str = ""


@dataclass
class Report:
    run_id: str
    generated_at: float
    objective: str
    mode: str
    scope: list[str]
    criteria: list[CriterionRow]
    contract_version: int
    base_revision: str
    candidate_id: str
    tier: str
    tier_reason: str
    disposition: str  # ACCEPTED | READY | BLOCKED | REJECTED | CANCELLED
    final: bool  # a disposition was recorded in the log
    reasons: list[ReasonRow]
    check_states: dict[str, str]
    checks: list[CheckRow]
    reviews: list[ReviewRow]
    findings: list[FindingRow]
    blocking_open: int
    tamper_signals: list[SignalRow]
    human_acts: list[HumanAct]
    cost: Cost
    audit: Audit
    timeline: list[TimelineRow]
    schema: int = SCHEMA
    tamper_known: bool = True  # False: the signals are not in the log and the live tree is not this candidate

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Report:
        nested: dict[str, Any] = {
            "criteria": CriterionRow, "checks": CheckRow, "reviews": ReviewRow, "findings": FindingRow,
            "tamper_signals": SignalRow, "human_acts": HumanAct, "reasons": ReasonRow, "timeline": TimelineRow,
        }
        kw: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in d:
                continue  # a field added later keeps its default; a required one fails loudly below
            v = d[f.name]
            if f.name in nested:
                v = [nested[f.name](**row) for row in v]
            elif f.name == "cost":
                v = Cost(**v)
            elif f.name == "audit":
                v = Audit(**v)
            kw[f.name] = v
        return cls(**kw)


# ------------------------------------------------------------------- build
def build_report(ari: Any, run_id: str) -> Report:
    """Fold one run's recorded events into a Report. Reads only; nothing is re-run or written."""
    state = ari.state(run_id)
    events = ari.store.events(run_id)
    try:
        decision = ari.evaluate(run_id)
    except (DaedalusError, OSError):  # e.g. the repository is gone: the log alone still tells most of the story
        decision = None
    recorded = next((e for e in reversed(events) if e.type == ev.DISPOSITION), None)

    final = state.disposition is not None
    if final:
        disposition = state.disposition.value
        reasons = list(state.disposition_reasons)
    elif decision is not None:
        disposition = "READY" if decision.acceptable else decision.disposition.value
        reasons = [r.to_dict() for r in decision.reasons]
    else:
        disposition, reasons = "BLOCKED", []

    candidate_id = (
        state.accepted_candidate_id
        or (recorded.payload.get("candidate_id") if recorded else None)
        or (decision.candidate_id if decision else None)
        or state.current_candidate_id
        or ""
    )
    # The live tree answers for the reported candidate only while the run is open and still on it. After
    # the disposition (or once the tree has moved) tamper signals and risk reasons would describe
    # something else, and the log records neither signals nor reasons, so they are left out.
    if decision is not None and not final and decision.candidate_id == candidate_id:
        live = decision
    else:
        live = None
    tier, tier_reason = _tier(state, live, recorded)
    check_states = {k: v[0].value for k, v in decision.check_states.items()} if decision else {}
    if final and recorded and isinstance(recorded.payload.get("checks"), dict):
        check_states = dict(recorded.payload["checks"])  # what was true when the disposition was recorded

    findings = _findings(state, candidate_id)
    metrics = run_metrics(events)
    signals = live.integrity.signals if live and live.integrity else ()
    tamper_known = live is not None or not state.policy.test_integrity.enabled
    return Report(
        run_id=run_id,
        generated_at=ari.clock(),
        objective=state.contract.objective,
        mode=state.mode,
        scope=list(state.contract.scope),
        criteria=[CriterionRow(c.criterion_id, c.description, list(c.checks))
                  for c in state.contract.acceptance_criteria],
        contract_version=state.contract.contract_version,
        base_revision=state.contract.base_revision or "",
        candidate_id=candidate_id,
        tier=tier,
        tier_reason=tier_reason,
        disposition=disposition,
        final=final,
        reasons=[ReasonRow(r.get("code", ""), r.get("message", ""), r.get("fixable_by", "")) for r in reasons],
        check_states=check_states,
        checks=_checks(state, events, check_states),
        reviews=_reviews(state, events),
        findings=findings,
        blocking_open=sum(
            1 for f in findings if f.status in ("open", "disputed") and f.severity in state.policy.blocking_severities
        ),
        tamper_signals=[SignalRow(s.kind, s.path, s.detail) for s in signals],
        human_acts=_human_acts(state, events),
        cost=Cost(
            total=metrics.cost, cash=metrics.cash, api_equivalent=round(metrics.cost - metrics.cash, 4),
            cache_hits=metrics.cache_hits, saved_total=metrics.cache_cost_saved,
            saved_cash=metrics.cache_cash_saved, saved_seconds=metrics.cache_seconds_saved,
            sessions=metrics.sessions, input_tokens=metrics.input_tokens, output_tokens=metrics.output_tokens,
            cash_unknown=state.cash_unknown,
        ),
        audit=_audit(ari, len(events)),
        timeline=[TimelineRow(e.ts, e.actor, e.type, _brief(e)) for e in events],
        tamper_known=tamper_known,
    )


def _tier(state: Any, decision: Any, recorded: Any) -> tuple[str, str]:
    live = decision.risk.effective_tier if decision else ""
    tier = (recorded.payload.get("risk_tier") if recorded else None) or live or state.contract.risk_tier
    if decision and live == tier:
        return tier, decision.risk.explain()
    return tier, "recorded when the run finished" if recorded else "declared by the contract"


def _checks(state: Any, events: list[Any], check_states: dict[str, str]) -> list[CheckRow]:
    reused = {
        e.payload["record"]["evidence_id"]: e.payload["reused_from"]
        for e in events if e.type == ev.EVIDENCE_RECORDED and isinstance(e.payload.get("reused_from"), dict)
    }
    last = {r.check_id: r.evidence_id for r in state.evidence}
    rows = []
    for r in state.evidence:
        src = reused.get(r.evidence_id)
        rows.append(CheckRow(
            check_id=r.check_id, command=list(r.command), result=r.result,
            state=check_states.get(r.check_id, "") if last[r.check_id] == r.evidence_id else "",
            duration_s=round(max(r.ended_at - r.started_at, 0.0), 3), started_at=r.started_at,
            candidate_id=r.candidate_id, verifier_id=r.verifier_id, mandatory=r.mandatory,
            reused=src is not None, reused_from=src,
        ))
    return rows


def _reviews(state: Any, events: list[Any]) -> list[ReviewRow]:
    payloads = {e.payload["review"]["review_id"]: e.payload["review"] for e in events if e.type == ev.REVIEW_RECORDED}
    # A launched review's reviewer id ends with the first 8 characters of its session id; the session
    # usage record carries the model. Matching on that id (not on order) keeps the attribution right when
    # a launched session failed or produced no review.
    models: dict[tuple[str, str], str] = {}
    for s_ in state.sessions:
        task, sid = str(s_.get("task_id", "")), str(s_.get("session_id") or "")
        if task.startswith("review-") and sid and "dropped" not in s_ and not s_.get("cost_unknown"):
            models.setdefault((task[len("review-"):], sid[:8]), str(s_.get("model") or ""))
    rows = []
    for r in state.reviews:
        raw = payloads.get(r.review_id, {})
        src = raw.get("reused_from") if isinstance(raw.get("reused_from"), dict) else None
        model = ""
        if src is None and r.source == "launched":
            # The session id recorded with the review, when there is one: a reviewer's name need not
            # end with it (a registered coordination name ends with its own uuid).
            sid = raw.get("session_id") if isinstance(raw.get("session_id"), str) else None
            model = models.get((r.perspective, sid[:8] if sid else r.reviewer.rsplit(":", 1)[-1]), "")
        counts: dict[str, int] = {}
        for f in raw.get("findings", ()):
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        rows.append(ReviewRow(
            review_id=r.review_id, perspective=r.perspective, scope=r.scope, reviewer=r.reviewer, model=model,
            candidate_id=r.candidate_id, reused=src is not None,
            counts={k: counts[k] for k in SEVERITIES if k in counts}, reused_from=src, summary=r.summary,
        ))
    return rows


def _findings(state: Any, candidate_id: str) -> list[FindingRow]:
    rows = []
    for f in state.findings.values():
        if f.resolved_by is not None:
            status = "resolved"
        elif is_moot(state, f, candidate_id):
            status = "moot"
        elif f.disputes:
            status = "disputed"
        else:
            status = "open"
        rows.append(FindingRow(
            finding_id=f.finding_id, title=f.title, severity=f.severity.value, status=status,
            perspective=f.perspective, review_id=f.review_id, resolution=f.resolution or "",
            resolved_by=f.resolved_by or "", disputes=len(f.disputes),
            ruling=str((f.ruling or {}).get("decision", "")),
        ))
    return rows


def _human_acts(state: Any, events: list[Any]) -> list[HumanAct]:
    acts = [
        HumanAct("approval", a.approver, a.granted_at,
                 f"{a.decision} {a.action}" + (f": {a.rationale}" if a.rationale else ""))
        for a in state.approvals
    ]
    acts += [
        HumanAct("attestation", a["by"], a["at"],
                 f"{a['criterion_id']} {a['status']}" + (f": {a['evidence']}" if a.get("evidence") else ""))
        for a in state.attestations
    ]
    acts += [
        HumanAct("risk_exception", x["by"], x["at"],
                 f"tier {x['tier']}" + (f": {x['rationale']}" if x.get("rationale") else ""))
        for x in state.risk_exceptions
    ]
    acts += [
        HumanAct("resolution", e.actor, e.ts, f"{e.payload['finding_id']}: {e.payload.get('note', '')}".rstrip(": "))
        for e in events if e.type == ev.FINDING_RESOLVED and e.actor.startswith(HUMAN_PREFIX)
    ]
    return sorted(acts, key=lambda a: (a.at, a.kind))


def _audit(ari: Any, run_events: int) -> Audit:
    everything = ari.store.events()
    head = everything[-1].hash if everything else ""
    try:
        ari.store.verify_chain()
        return Audit(True, head, len(everything), run_events)
    except AuditIntegrityError as exc:
        return Audit(False, head, len(everything), run_events, str(exc))


def _brief(e: Any) -> str:
    """A short, structural summary of one event. Free text from agents is left out of the timeline."""
    p, t = e.payload, e.type
    try:
        if t == ev.EXECUTION:
            return f"{p['from']} -> {p['to']}"
        if t == ev.EVIDENCE_RECORDED:
            r = p["record"]
            return f"{r['check_id']} {r['result']}" + (" (reused)" if p.get("reused_from") else "")
        if t == ev.CANDIDATE_RECORDED:
            return p["candidate_id"][:12]
        if t == ev.REVIEW_RECORDED:
            r = p["review"]
            return f"{r['perspective']} {r.get('scope', 'full')} review, {len(r.get('findings', ()))} finding(s)"
        if t == ev.DISPOSITION:
            return str(p["disposition"])
        if t == ev.APPROVAL_RECORDED:
            return f"{p['approval']['decision']} {p['approval']['action']}"
        if t in (ev.FINDING_RESOLVED, ev.FINDING_DISPUTED):
            return str(p.get("finding_id", ""))
        if t == ev.RISK_EXCEPTION:
            return f"tier {p.get('tier', '')}"
        if t == ev.CRITERION_ATTESTED:
            return f"{p.get('criterion_id', '')} {p.get('status', '')}"
    except (KeyError, TypeError):
        pass
    return ""


# ------------------------------------------------- shared by both renderers
def strip_controls(text: str) -> str:
    """Drop control characters (C0, DEL, C1 and the bidi overrides) except tab and newline: a report
    printed to a terminal must not replay escape sequences an agent wrote into a finding."""
    return "".join(
        ch for ch in text
        if ch in "\t\n" or (unicodedata.category(ch) != "Cc" and ch not in _BIDI)
    )


_BIDI = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def utc(ts: float) -> str:
    """Timestamps are shown in UTC so a report reads the same wherever it is opened."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z"


def money(x: float) -> str:
    return f"${x:.2f}"


def headline_parts(r: Report) -> list[str]:
    """The facts of the one-line headline, shared by both renderers (the first is the disposition)."""
    states = r.check_states
    if not states:
        checks = "no checks"
    elif all(v == "PASS" for v in states.values()):
        checks = "tests PASS" if list(states) == ["tests"] else "checks PASS"
    else:
        checks = "checks: " + ", ".join(f"{k} {v}" for k, v in states.items() if v != "PASS")
    n = len(r.reviews)
    cost = money(r.cost.total) + (" (cash unknown)" if r.cost.cash_unknown else "")
    return [
        r.disposition, f"tier {r.tier}", checks, f"{n} review{'' if n == 1 else 's'} ({r.blocking_open} blocking)",
        cost, "chain verified" if r.audit.verified else "chain NOT verified",
    ]


def risk_line(r: Report) -> str:
    """`tier high: path floor ...`; the explanation from core.risk already starts with the tier."""
    return r.tier_reason if r.tier_reason.startswith("tier ") else f"tier {r.tier}: {r.tier_reason}"


ICONS = {"ACCEPTED": "✅", "READY": "🟢", "BLOCKED": "🟡", "REJECTED": "❌", "CANCELLED": "⚪"}
