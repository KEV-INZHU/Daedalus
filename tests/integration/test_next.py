"""`daedalus next`: the one thing the run needs now, and from whom."""

from __future__ import annotations

import json

from conftest import BUILDER, HUMAN, fix
from daedalus.cli.next import next_step

FINDING = {"severity": "MAJOR", "title": "off by one", "evidence": "a.py:3", "required_change": "fix it"}


def test_no_run_and_finished_run(repo, ari):
    assert next_step(ari, None)["action"].startswith("no run is open")
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.finish(rid)
    step = next_step(ari, rid)
    assert step["who"] == "nobody" and "ACCEPTED" in step["action"]


def test_the_agent_comes_first_then_people(repo, ari):
    rid = ari.start({"objective": "o"}, actor="agent:harness")
    step = next_step(ari, rid)
    assert step["who"] == "agent" and "daedalus verify" in step["action"]
    fix(repo)
    ari.verify(rid)
    ari.propose_contract(rid, objective="better", criteria=["it works"], actor="agent:harness")
    step = next_step(ari, rid)
    assert step["who"] == "agent" and "daedalus finish" in step["action"] and step["more"] == 1
    ari.record_candidate(rid, actor=BUILDER)
    ari.submit_review(rid, reviewer="agent:reviewer:aristotle:x", perspective="aristotle", findings=[FINDING])
    step = next_step(ari, rid)
    assert step["who"] == "agent" and "answer finding F1" in step["action"]
    ari.answer_finding(rid, "F1", actor=BUILDER, note="fixed the bound")
    assert "answer finding" not in next_step(ari, rid)["action"]


def test_only_people_left(repo, ari):
    rid = ari.start(
        {"objective": "o", "acceptance_criteria": [{"description": "looks right", "checks": []}]}, actor=HUMAN
    )
    fix(repo)
    ari.verify(rid)
    step = next_step(ari, rid)
    assert step["who"] == "human"


def test_cli(repo, ari, capsys):
    from daedalus.cli import main

    assert main(["-C", str(repo), "next"]) == 0
    assert capsys.readouterr().out.startswith("agent: no run is open")
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    assert main(["-C", str(repo), "next", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"] == rid and out["who"] == "agent"
