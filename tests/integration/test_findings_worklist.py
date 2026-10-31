"""Review findings as a worklist: list them, answer each with a fix note or a dispute."""

from __future__ import annotations

import json

import pytest

from conftest import BUILDER, HUMAN
from daedalus.adapters.base import AgentResult
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.errors import ContractError
from daedalus.orchestration.lifecycle import WORKLIST_HINT, followup_feedback, launch_review

FINDING = {
    "severity": "MINOR",
    "title": "vague name",
    "evidence": "app.txt:1",
    "required_change": "rename it",
}


@pytest.fixture
def reviewed(repo, ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (repo / "app.txt").write_text("ok\n", encoding="utf-8")
    ari.record_candidate(rid, actor=BUILDER)
    ari.submit_review(
        rid, reviewer="agent:reviewer:aristotle:r1", perspective="aristotle", findings=[FINDING]
    )
    return rid


def test_an_answer_reaches_the_re_review_and_resolves_nothing(repo, ari, reviewed):
    ari.answer_finding(reviewed, "F1", actor=BUILDER, note="renamed it\nto `status`")
    f = ari.state(reviewed).findings["F1"]
    assert f.resolved_by is None and f.answers[0]["note"] == "renamed it to `status`"

    prompts = []

    def rereview(req):
        prompts.append(req.prompt)
        return AgentResult("COMPLETED", structured={"summary": "ok", "findings": [], "resolves": ["F1"]})

    launch_review(ari, reviewed, "aristotle", SimulatedAdapter({"aristotle": [rereview]}))
    assert 'The builder says it addressed this: "renamed it to \\u0060status\\u0060"' in prompts[0]
    assert "`status`" not in prompts[0]  # backticks escaped like every other quoted data
    assert ari.state(reviewed).findings["F1"].resolved_by is not None  # the reviewer resolved it


@pytest.mark.parametrize("note", ["", "   ", "x" * 1001])
def test_bad_answers_are_refused(ari, reviewed, note):
    with pytest.raises(ContractError):
        ari.answer_finding(reviewed, "F1", actor=BUILDER, note=note)


def test_a_resolved_finding_cannot_be_answered(ari, reviewed):
    ari.resolve_finding(reviewed, "F1", actor=HUMAN, note="fine")
    with pytest.raises(ContractError):
        ari.answer_finding(reviewed, "F1", actor=BUILDER, note="fixed")


def test_cli_lists_and_answers(repo, ari, reviewed, monkeypatch, capsys):
    from daedalus.cli import main

    monkeypatch.setenv("DAEDALUS_AGENT", "1")  # an agent session may use it
    r = str(repo)
    assert main(["-C", r, "findings"]) == 0
    out = capsys.readouterr().out
    assert "F1 [MINOR] vague name" in out and "; open)" in out and "change: rename it" in out
    assert main(["-C", r, "findings", "answer", "F1", "--fixed", "renamed"]) == 0
    assert main(["-C", r, "findings", "answer", "F1", "--fixed", "a", "--dispute", "b"]) == 2
    assert main(["-C", r, "findings", "answer", "F1"]) == 2
    capsys.readouterr()
    assert main(["-C", r, "findings", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["status"].startswith("answered") and row["answers"][0]["note"] == "renamed"
    assert main(["-C", r, "findings", "answer", "F1", "--dispute", "the name is the spec's"]) == 0
    assert ari.state(reviewed).findings["F1"].disputes
    ari.resolve_finding(reviewed, "F1", actor=HUMAN, note="ok")
    capsys.readouterr()
    assert main(["-C", r, "findings"]) == 0 and "no open findings" in capsys.readouterr().out
    assert main(["-C", r, "findings", "--all"]) == 0 and "resolved by" in capsys.readouterr().out


def test_feedback_points_at_the_worklist(ari, reviewed):
    lines = followup_feedback(ari.state(reviewed).open_findings)
    assert lines[-1] == WORKLIST_HINT and "daedalus findings answer" in WORKLIST_HINT
