"""Reviewer calibration: how far to trust a reviewer's findings, measured from the audit log.

Superforecasting habits that fit code review are built in here:

- **Probabilities, not labels.** Each finding carries `confidence`, the reviewer's probability that it
  is real and needs a change, and each review carries `residual_risk`, its probability that a
  MAJOR-or-worse defect it did not report remains.
- **Keep score.** Outcomes come from what happened later. A finding is scored as real when a later
  review confirmed a fix, when the arbiter upheld it, or when a person judged it `valid`. It is scored
  as a false alarm when the arbiter overruled it or a person judged it `invalid`. A review's residual
  risk is scored against whether its accepted run escaped (`Fixes-Run:`).
- **Brier score and its parts.** The Brier score is the mean squared gap between forecast and
  outcome (0 is perfect; always saying the base rate scores base_rate x (1 - base_rate)). Murphy's
  decomposition splits it into reliability (how far stated probabilities sit from observed
  frequencies: lower is better), resolution (how well forecasts separate real from false: higher is
  better) and uncertainty (how hard the outcomes were to call).
- **Outside view first.** `track_record` turns the score into a short base-rate note for the next
  review prompt from the same perspective, so a reviewer that over-reports learns how often its
  findings held up and which kinds were false alarms.

Everything is a read-only fold over recorded events: nothing here changes a disposition.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from daedalus.audit.store import EventStore
from daedalus.core.run import Finding, RunState, replay
from daedalus.core.state_machine import Disposition

BINS = 10
MIN_SAMPLE = 15  # fewer resolved forecasts than this and a score says little; reports say so
MIN_TRACK = 5  # resolved findings a perspective needs before its record goes into prompts


def finding_outcome(state: RunState, f: Finding) -> tuple[int | None, str]:
    """(1 real | 0 false alarm | None unknown, why). A person's judgment wins, then the arbiter's
    ruling, then a confirmed fix: the builder changed the code and a fresh review of a later candidate
    agreed it was fixed."""
    if f.judgment:
        return (1, "judged valid") if f.judgment.get("verdict") == "valid" else (0, "judged invalid")
    if f.ruling:
        d = f.ruling.get("decision")
        if d == "uphold":
            return 1, "upheld by the arbiter"
        if d == "overrule":
            return 0, "overruled by the arbiter"
    if f.resolved_in and f.resolved_in != f.candidate_id and (f.resolution or "").startswith("resolved by "):
        return 1, "fix confirmed by a later review"
    return None, "unresolved"


@dataclass(frozen=True)
class Forecast:
    p: float
    outcome: int
    kind: str  # finding | residual
    run_id: str
    perspective: str
    model: str
    severity: str = ""
    ref: str = ""  # finding id, or review id for a residual forecast
    title: str = ""
    why: str = ""


def forecasts(states: Iterable[RunState], escaped: set[str] | frozenset[str] = frozenset()) -> tuple[
        list[Forecast], dict[str, int]]:
    """Every scored forecast, plus counts of what could not be scored (no probability given, or
    no outcome known yet)."""
    out: list[Forecast] = []
    gaps = {"findings_without_confidence": 0, "findings_unresolved": 0, "reviews_without_residual": 0}
    for s in states:
        model_of = {r.review_id: r.model or "unknown" for r in s.reviews}
        for f in s.findings.values():
            o, why = finding_outcome(s, f)
            if f.confidence is None:
                gaps["findings_without_confidence"] += 1
                continue
            if o is None:
                gaps["findings_unresolved"] += 1
                continue
            out.append(Forecast(f.confidence, o, "finding", s.run_id, f.perspective,
                                model_of.get(f.review_id, "unknown"), f.severity.value, f.finding_id, f.title, why))
        if s.disposition is not Disposition.ACCEPTED:
            continue
        for r in s.reviews:
            if r.candidate_id != s.accepted_candidate_id:
                continue  # only a review of what shipped forecasts what shipped
            if r.residual_risk is None:
                gaps["reviews_without_residual"] += 1
                continue
            hit = int(s.run_id in escaped)
            out.append(Forecast(r.residual_risk, hit, "residual", s.run_id, r.perspective, r.model or "unknown",
                                ref=r.review_id, why="escaped" if hit else "no fix recorded yet"))
    return out, gaps


@dataclass(frozen=True)
class Score:
    n: int
    brier: float | None
    base_rate: float | None  # observed frequency of outcome 1
    mean_forecast: float | None
    reference: float | None  # Brier of always forecasting the base rate
    skill: float | None  # 1 - brier / reference: above 0 beats the base rate
    reliability: float | None
    resolution: float | None
    uncertainty: float | None
    bins: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def score(items: Iterable[tuple[float, int]]) -> Score:
    pairs = [(float(p), int(o)) for p, o in items]
    n = len(pairs)
    if not n:
        return Score(0, None, None, None, None, None, None, None, None)
    brier = sum((p - o) ** 2 for p, o in pairs) / n
    base = sum(o for _, o in pairs) / n
    mean_p = sum(p for p, _ in pairs) / n
    unc = base * (1 - base)
    groups: dict[int, list[tuple[float, int]]] = {}
    for p, o in pairs:
        groups.setdefault(min(int(p * BINS), BINS - 1), []).append((p, o))
    rel = res = 0.0
    bins = []
    for k in sorted(groups):
        g = groups[k]
        pk = sum(p for p, _ in g) / len(g)
        ok = sum(o for _, o in g) / len(g)
        rel += len(g) * (pk - ok) ** 2
        res += len(g) * (ok - base) ** 2
        bins.append({"range": f"{k / BINS:.1f}-{(k + 1) / BINS:.1f}", "n": len(g), "mean_forecast": round(pk, 3),
                     "observed": round(ok, 3)})
    return Score(n, _r(brier), _r(base), _r(mean_p), _r(unc), _r(1 - brier / unc) if unc else None,
                 _r(rel / n), _r(res / n), _r(unc), bins)


def report(fs: list[Forecast], gaps: dict[str, int], by: str = "perspective") -> dict[str, Any]:
    def grouped(kind: str) -> dict[str, Any]:
        mine = [f for f in fs if f.kind == kind]
        keys = sorted({getattr(f, by) or "-" for f in mine})
        return {
            "all": score((f.p, f.outcome) for f in mine).to_dict(),
            "by": {k: score((f.p, f.outcome) for f in mine if (getattr(f, by) or "-") == k).to_dict() for k in keys},
        }

    false_alarms = [f for f in fs if f.kind == "finding" and f.outcome == 0]
    false_alarms.sort(key=lambda f: -f.p)
    return {
        "grouped_by": by,
        "findings": grouped("finding"),
        "residual": grouped("residual"),
        "unscored": gaps,
        "min_sample": MIN_SAMPLE,
        "confident_false_alarms": [
            {"run": f.run_id, "finding": f.ref, "confidence": f.p, "perspective": f.perspective, "title": f.title,
             "why": f.why} for f in false_alarms[:10]
        ],
    }


def load_states(store: EventStore, limit: int | None = None) -> tuple[list[RunState], dict[str, str]]:
    states: list[RunState] = []
    errors: dict[str, str] = {}
    ids = store.run_ids()
    for rid in ids[-limit:] if limit else ids:
        try:
            states.append(replay(store.events(rid)))
        except Exception as exc:  # noqa: BLE001 — read-only: an unreadable run is reported, never fatal
            errors[rid] = f"{type(exc).__name__}: {exc}"
    return states, errors


def track_record(states: Iterable[RunState], perspective: str, exclude_run: str | None = None) -> str:
    """The outside view for the next review from `perspective`, as a short prompt section, or ""
    while there are too few resolved findings to say anything."""
    rows: list[tuple[Finding, int, str]] = []
    for s in states:
        if s.run_id == exclude_run:
            continue
        for f in s.findings.values():
            if f.perspective != perspective:
                continue
            o, why = finding_outcome(s, f)
            if o is not None:
                rows.append((f, o, why))
    if len(rows) < MIN_TRACK:
        return ""
    held = sum(o for _, o, _ in rows)
    lines = ["## Your track record in this repository (the outside view: start from it)", "",
             f"Of your last {len(rows)} findings with a known outcome, {held} held up and {len(rows) - held} "
             f"were false alarms ({held / len(rows):.0%} held up). \"Held up\" means fixed when asked and the fix "
             "confirmed, upheld by the arbiter, or judged valid by a person, so it flatters a little: a builder "
             "may fix what was not wrong."]
    stated = [f.confidence for f, _, _ in rows if f.confidence is not None]
    scored = [(f.confidence, o) for f, o, _ in rows if f.confidence is not None]
    if len(stated) >= MIN_TRACK:
        mean_p = sum(stated) / len(stated)
        hit = sum(o for _, o in scored) / len(scored)
        gap = mean_p - hit
        verdict = ("overconfident" if gap > 0.05 else "underconfident" if gap < -0.05 else "well calibrated")
        lines.append(f"Your stated confidence averaged {mean_p:.0%} against {hit:.0%} that held up: {verdict}"
                     + (f" by {abs(gap):.0%}." if verdict != "well calibrated" else "."))
    by_sev: dict[str, list[int]] = {}
    for f, o, _ in rows:
        by_sev.setdefault(f.severity.value, []).append(o)
    lines.append("By severity: " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(by_sev.items())) + ".")
    misses = [f for f, o, _ in rows if o == 0][-3:]
    if misses:
        from daedalus.orchestration.prompts import quote_data

        lines.append("Recent false alarms (quoted titles; check that a new finding is not the same mistake):")
        lines += [f"- {quote_data(f.title[:160])}" + (f" (judged: {quote_data(f.judgment.get('note', '')[:160])})"
                                                   if f.judgment and f.judgment.get("note") else "")
                  for f in misses]
    return "\n".join(lines)
