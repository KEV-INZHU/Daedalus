"""Reviewers forecast, outcomes score them, and the score comes back as the outside view."""

from __future__ import annotations

import json
import math

import pytest

from conftest import BUILDER, HUMAN, fix, make_repo
from daedalus.adapters.base import ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.calibration import (
    MIN_TRACK,
    finding_outcome,
    forecasts,
    load_states,
    report,
    score,
    track_record,
)
from daedalus.core.errors import AuthorizationError, ContractError
from daedalus.orchestration.ariadne import Ariadne, probability
from daedalus.orchestration.lifecycle import LITE_DEFAULT_MODEL, launch_review, lite_adapter

CLEAN = {"summary": "fine", "findings": [], "residual_risk": 0.1}


def finding(title="empty input unhandled", conf=0.8, **kw):
    return {"title": title, "evidence": "lib.py:1", "severity": "MAJOR", "required_change": "handle it",
            "confidence": conf, **kw}


def reviewer(*bodies, role="aristotle") -> SimulatedAdapter:
    return SimulatedAdapter({role: [ScriptedStep(structured=b) for b in bodies]})


@pytest.mark.parametrize("raw,want", [(0.8, 0.8), ("0.25", 0.25), ("80%", 0.8), (1, 1.0), (0, 0.0),
                                      (1.5, None), (-0.1, None), ("high", None), (True, None),
                                      (float("nan"), None), (None, None), ([0.5], None)])
def test_a_probability_is_kept_only_when_it_is_one(raw, want):
    assert probability(raw) == want


def test_forecasts_are_recorded_and_a_bad_one_never_fails_the_review(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(root)
    a.verify(rid)
    body = {"summary": "s", "residual_risk": "15%", "findings": [
        finding(against="the caller may never pass empty input", failing_test="def test_empty(): f('')",
                severity="BLOCKER"),
        finding("other", conf="very sure")]}
    launch_review(a, rid, "aristotle", reviewer(body))
    s = a.state(rid)
    f1, f2 = s.findings["F1"], s.findings["F2"]
    assert (f1.confidence, f1.against, f1.failing_test) == (0.8, "the caller may never pass empty input",
                                                            "def test_empty(): f('')")
    assert f2.confidence is None
    assert s.reviews[0].residual_risk == 0.15
    msg = next(r.message for r in a.evaluate(rid).reasons if r.code == "finding:F1")
    assert "reviewer's confidence 80%" in msg and "Test that fails until it is fixed: def test_empty()" in msg
    from daedalus.orchestration.lifecycle import followup_feedback

    assert "reviewer's confidence 80%" in "\n".join(followup_feedback([f1]))
    a.close()


def test_the_prompt_asks_for_forecasts_and_names_the_base_revision_trap():
    from daedalus.orchestration.prompts import REVIEW_OUTPUT

    assert "Brier score" in REVIEW_OUTPUT and '"confidence"' in REVIEW_OUTPUT and '"residual_risk"' in REVIEW_OUTPUT
    assert "already like that before this change" in REVIEW_OUTPUT


def _run_with_finding(a: Ariadne, root, body_first, body_second=None):
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(root)
    a.verify(rid)
    rev = reviewer(body_first, *( [body_second] if body_second else []))
    launch_review(a, rid, "aristotle", rev)
    return rid, rev


def test_outcomes_come_from_confirmed_fixes_rulings_and_judgments(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid, rev = _run_with_finding(a, root, {"summary": "s", "findings": [finding(), finding("b", 0.3)]},
                                 {"summary": "fixed", "findings": [], "resolves": ["F1"], "residual_risk": 0.05})
    s = a.state(rid)
    assert finding_outcome(s, s.findings["F1"]) == (None, "unresolved")
    (root / "src" / "lib.py").write_text("x = 2\n")  # the fix, then a fresh review confirms it
    launch_review(a, rid, "aristotle", rev)
    s = a.state(rid)
    assert finding_outcome(s, s.findings["F1"]) == (1, "fix confirmed by a later review")
    with pytest.raises(AuthorizationError, match="only a person"):
        a.judge_finding(rid, "F2", actor=BUILDER, verdict="invalid")
    with pytest.raises(ContractError, match="verdict"):
        a.judge_finding(rid, "F2", actor=HUMAN, verdict="maybe")
    a.judge_finding(rid, "F2", actor=HUMAN, verdict="invalid", note="already failing on main")
    s = a.state(rid)
    assert finding_outcome(s, s.findings["F2"]) == (0, "judged invalid")
    assert not s.findings["F2"].resolved_by  # a judgment scores; it resolves nothing
    a.close()


def test_a_judgment_can_be_recorded_after_the_run_is_finished(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid, _ = _run_with_finding(a, root, {"summary": "s", "findings": [{**finding(), "severity": "MINOR"}]})
    a.finish(rid)
    assert a.state(rid).is_finished
    a.judge_finding(rid, "F1", actor=HUMAN, verdict="valid")
    assert a.state(rid).findings["F1"].judgment["verdict"] == "valid"
    a.close()


def test_brier_score_and_murphy_decomposition():
    pairs = [(0.8, 1), (0.6, 0), (0.9, 1), (0.2, 0)]
    s = score(pairs)
    assert s.n == 4 and math.isclose(s.brier, 0.1125)
    assert s.base_rate == 0.5 and s.reference == 0.25 and math.isclose(s.skill, 0.55)
    # One forecast per bin: Brier = reliability - resolution + uncertainty exactly.
    assert math.isclose(s.brier, s.reliability - s.resolution + s.uncertainty, abs_tol=1e-4)
    assert [b["range"] for b in s.bins] == ["0.2-0.3", "0.6-0.7", "0.8-0.9", "0.9-1.0"]
    assert score([(1.0, 1)]).bins[0]["range"] == "0.9-1.0"  # 1.0 falls in the top bin
    assert score([]).brier is None


def test_residual_risk_is_scored_against_escapes(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid, _ = _run_with_finding(a, root, CLEAN)
    a.finish(rid)
    states, errors = load_states(a.store)
    assert not errors
    fs, gaps = forecasts(states, escaped={rid})
    res = [f for f in fs if f.kind == "residual"]
    assert [(f.p, f.outcome) for f in res] == [(0.1, 1)]
    fs, _ = forecasts(states)
    assert [(f.p, f.outcome) for f in fs if f.kind == "residual"] == [(0.1, 0)]
    a.close()


def _settled_runs(a: Ariadne, root, n: int, false_title="the test fails"):
    """n runs, each with one finding a person judged a false alarm."""
    for _ in range(n):
        rid, _ = _run_with_finding(a, root, {"summary": "s", "findings": [finding(false_title, 0.9)]})
        a.judge_finding(rid, "F1", actor=HUMAN, verdict="invalid", note="it was already failing on main")
        a.cancel(rid, actor=HUMAN) if not a.state(rid).is_finished else None
        (root / "app.txt").write_text("bad\n")


def test_the_track_record_goes_into_the_next_review_prompt(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    states, _ = load_states(a.store)
    assert track_record(states, "aristotle") == ""  # nothing to go on yet
    _settled_runs(a, root, MIN_TRACK)
    rid, rev = _run_with_finding(a, root, CLEAN)
    prompt = rev.requests[0].prompt
    assert "Your track record in this repository" in prompt
    assert f"Of your last {MIN_TRACK} findings with a known outcome, 0 held up" in prompt
    assert "overconfident by 90%" in prompt
    assert json.dumps("the test fails") in prompt and "already failing on main" in prompt
    a.close()


def test_report_lists_confident_false_alarms_and_gaps(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    _settled_runs(a, root, 2)
    _run_with_finding(a, root, {"summary": "s", "findings": [finding("open one", None)]})
    states, _ = load_states(a.store)
    rep = report(*forecasts(states))
    assert rep["findings"]["all"]["n"] == 2 and rep["findings"]["all"]["brier"] == pytest.approx(0.81)
    assert rep["unscored"]["findings_without_confidence"] == 1
    assert rep["confident_false_alarms"][0]["confidence"] == 0.9
    a.close()


def test_lite_mode_asks_three_questions_unless_the_tier_needs_a_full_review(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "lite"})
    a = Ariadne(root, clock=clock)
    rid, rev = _run_with_finding(a, root, CLEAN)
    p = rev.requests[0].prompt
    assert "a quick review, three questions" in p and "Inputs at the edges" not in p
    assert a.state(rid).mode == "lite"
    a.cancel(rid, actor=HUMAN)
    (root / "src" / "auth").mkdir()
    (root / "src" / "auth" / "login.py").write_text("x = 1\n")  # high tier: aristotle is required
    rid, rev = _run_with_finding(a, root, CLEAN)
    p = rev.requests[0].prompt
    assert "Inputs at the edges" in p and "three questions" not in p
    a.close()


class _Fake:
    def __init__(self, name):
        self.name, self.review_model = name, "sonnet"


def test_lite_reviews_run_on_the_lite_model(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "lite"})
    a = Ariadne(root, clock=clock)
    assert lite_adapter(a, _Fake("claude-code")).review_model == LITE_DEFAULT_MODEL
    other = _Fake("codex")
    assert lite_adapter(a, other) is other  # no lite model for it: unchanged
    a.close()
    root2 = make_repo(tmp_path / "r2", {"mode": "lite", "lite": {"model": "gpt-mini"}})
    b = Ariadne(root2, clock=clock)
    fake = _Fake("codex")
    swapped = lite_adapter(b, fake)
    assert swapped.review_model == "gpt-mini" and fake.review_model == "sonnet"  # a copy, not a mutation
    b.close()


@pytest.mark.parametrize("bad", [{"model": ""}, {"model": 3}, {"other": 1}, "haiku"])
def test_a_bad_lite_setting_is_refused(tmp_path, bad):
    from daedalus.core.config import load_config
    from daedalus.core.errors import PolicyError

    root = make_repo(tmp_path / "r", {"mode": "lite", "lite": bad})
    with pytest.raises(PolicyError, match="lite"):
        load_config(root)


def test_judge_and_calibration_from_the_command_line(tmp_path, clock, monkeypatch, capsys):
    from daedalus import cli
    from daedalus.cli import main

    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid, _ = _run_with_finding(a, root, {"summary": "s", "findings": [finding()]})
    a.close()
    monkeypatch.setenv("DAEDALUS_AGENT", "1")
    assert main(["-C", str(root), "findings", "--run", rid, "judge", "F1", "invalid"]) == 2  # agents cannot
    monkeypatch.delenv("DAEDALUS_AGENT")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    assert main(["-C", str(root), "findings", "--run", rid, "judge", "F1", "invalid", "was already failing", "-y"]) == 0
    capsys.readouterr()
    assert main(["-C", str(root), "findings", "--run", rid]) == 0
    assert "80% sure" in capsys.readouterr().out
    assert main(["-C", str(root), "calibration"]) == 0
    out = capsys.readouterr().out
    assert "Findings: is each one real?  (1 scored)" in out and "Brier 0.640" in out
    assert "under 15 these numbers say little" in out and "Most confident false alarms" in out
    assert main(["-C", str(root), "calibration", "--json", "--by", "severity"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["findings"]["by"]["MAJOR"]["n"] == 1


def test_judging_asks_for_confirmation_without_yes(tmp_path, clock, monkeypatch):
    from daedalus import cli
    from daedalus.cli import main

    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid, _ = _run_with_finding(a, root, {"summary": "s", "findings": [finding()]})
    a.close()
    monkeypatch.delenv("DAEDALUS_AGENT", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["-C", str(root), "findings", "--run", rid, "judge", "F1", "valid"]) != 0
    b = Ariadne(root, clock=clock)
    assert b.state(rid).findings["F1"].judgment is None
    b.close()


def test_the_track_record_is_replayed_only_when_the_log_grows(tmp_path, clock, monkeypatch):
    from daedalus.core import calibration
    from daedalus.orchestration import lifecycle

    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    calls = []
    real = calibration.load_states
    monkeypatch.setattr(calibration, "load_states", lambda *x, **k: calls.append(1) or real(*x, **k))
    lifecycle._TRACK_MEMO.clear()
    lifecycle.track_record_for(a, "aristotle", rid)
    lifecycle.track_record_for(a, "mozi", rid)
    assert len(calls) == 1
    a.verify(rid)  # appends events
    lifecycle.track_record_for(a, "aristotle", rid)
    assert len(calls) == 2
    a.close()
