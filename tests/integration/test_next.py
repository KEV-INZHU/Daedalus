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
    step = next_step(ari, rid)
    assert "answer finding" not in step["action"]  # not sent back to F1


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


def test_an_answered_finding_awaits_re_review(repo, ari):
    from daedalus.cli.next import next_step as ns

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.record_candidate(rid, actor=BUILDER)
    blocking = {**FINDING, "severity": "BLOCKER"}
    ari.submit_review(
        rid, reviewer="agent:reviewer:aristotle:x", perspective="aristotle", findings=[blocking]
    )
    ari.verify(rid)
    ari.answer_finding(rid, "F1", actor=BUILDER, note="fixed")
    step = ns(ari, rid)
    assert step["who"] == "system" and "awaits re-review" in step["action"]


def test_an_explicit_run_that_does_not_resolve_is_an_error(repo, ari):
    from daedalus.cli import main

    assert main(["-C", str(repo), "next", "--run", "nosuchrun"]) == 2
