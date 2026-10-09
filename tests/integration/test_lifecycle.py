"""End-to-end: scripted Builder and reviewers through the lifecycle and the Claude Code shim."""

from __future__ import annotations

import json

import pytest

from conftest import HUMAN, fix
from daedalus.adapters.base import AgentResult, ScriptedStep
from daedalus.adapters.inline import InlineAdapter
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.config import set_gate
from daedalus.core.errors import CapabilityError
from daedalus.core.state_machine import Disposition
from daedalus.harness import claude_code
from daedalus.orchestration.lifecycle import run_task

DONE = {"status": "done", "summary": "fixed"}


@pytest.fixture(autouse=True)
def not_a_launched_session(monkeypatch):
    """These tests describe a harness session. When the suite itself runs inside a
    Daedalus-launched agent, DAEDALUS_AGENT is inherited and the hooks would stand down."""
    monkeypatch.delenv("DAEDALUS_AGENT", raising=False)
CLEAN = {"summary": "looks right", "findings": []}


def test_builder_fixes_and_run_is_accepted(ari, repo):
    adapter = SimulatedAdapter({"brunel": [ScriptedStep(edits={"app.txt": "ok\n"}, structured=DONE, cost=0.5)]})
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter)
    assert report.decision.disposition is Disposition.ACCEPTED
    assert report.rounds == 1
    assert ari.state(report.run_id).cost_used == pytest.approx(0.5)


def test_failing_check_is_fed_back_to_builder(ari, repo):
    seen = []

    def second(req):
        seen.append(req.prompt)
        fix(repo)
        return AgentResult("COMPLETED", structured=DONE)

    adapter = SimulatedAdapter(
        {"brunel": [ScriptedStep(edits={"app.txt": "still bad\n"}, structured=DONE), second]}
    )
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter)
    assert report.decision.disposition is Disposition.ACCEPTED
    assert report.rounds == 2
    assert "`tests` failed" in seen[0]


def test_review_mode_launches_reviewer_and_blocks_on_blocker(ari, repo):
    finding = {"title": "no test", "evidence": "app.txt", "severity": "BLOCKER", "required_change": "add one"}
    adapter = SimulatedAdapter(
        {
            "brunel": [ScriptedStep(edits={"app.txt": "ok\n"}, structured=DONE)] * 2,
            "aristotle": [
                ScriptedStep(structured={"summary": "missing test", "findings": [finding]}),
                ScriptedStep(structured={"summary": "fixed", "findings": [], "resolves": ["F1"]}),
            ],
        }
    )
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter, mode="review")
    assert report.decision.disposition is Disposition.ACCEPTED
    assert report.rounds == 2
    state = ari.state(report.run_id)
    assert all(r.source == "launched" for r in state.reviews)
    assert any("add one" in r.prompt for r in adapter.requests if r.role == "brunel")


def test_reviewer_that_edits_files_is_discarded(ari, repo):
    adapter = SimulatedAdapter(
        {
            "brunel": [ScriptedStep(edits={"app.txt": "ok\n"}, structured=DONE)],
            "aristotle": [ScriptedStep(edits={"app.txt": "ok sneaky\n"}, structured=CLEAN)],
        }
    )
    report = run_task(ari, {"objective": "o"}, actor=HUMAN, adapter=adapter, mode="review", max_rounds=1)
    assert report.decision.disposition is not Disposition.ACCEPTED
    assert ari.state(report.run_id).failed_reviews


def test_builder_blocked_raises_blocker_and_stops(ari, repo):
    adapter = SimulatedAdapter(
        {"brunel": [ScriptedStep(structured={"status": "blocked", "blocker": "which API?"})]}
    )
    report = run_task(ari, {"objective": "o"}, actor=HUMAN, adapter=adapter)
    assert report.decision.disposition is Disposition.BLOCKED
    assert any("which API?" in r.message for r in report.decision.reasons)


def test_inline_adapter_cannot_drive_a_run(ari):
    with pytest.raises(CapabilityError):
        run_task(ari, {"objective": "o"}, actor=HUMAN, adapter=InlineAdapter())


def test_independent_review_needs_launch_identity(ari, repo):
    from dataclasses import replace

    from daedalus.orchestration.lifecycle import launch_review

    base = SimulatedAdapter().capabilities
    adapter = SimulatedAdapter({"aristotle": [ScriptedStep(structured=CLEAN)]}, replace(base, identity="self_reported"))
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    with pytest.raises(CapabilityError):
        launch_review(ari, rid, "aristotle", adapter)


# ------------------------------------------------------------ Claude Code shim
def payload(repo):
    return {"cwd": str(repo), "hook_event_name": "Stop"}


def test_stop_hook_blocks_until_checks_pass_then_accepts(ari, repo):
    rid = ari.start({"objective": "make app ok"}, actor="agent:harness")
    out = claude_code.stop_hook(payload(repo), ari=ari)
    assert out["decision"] == "block"
    assert "`tests` failed" in out["reason"]
    fix(repo)
    out = claude_code.stop_hook(payload(repo), ari=ari)
    assert "decision" not in out
    assert "ACCEPTED" in out["systemMessage"]
    assert ari.state(rid).disposition is Disposition.ACCEPTED


def test_stop_hook_allows_stop_when_only_humans_can_act(ari, repo):
    ari.start({"objective": "o", "approval_requirements": ["deploy"]}, actor="agent:harness")
    fix(repo)
    out = claude_code.stop_hook(payload(repo), ari=ari)
    assert "decision" not in out
    assert "daedalus approve deploy" in out["systemMessage"]


def test_stop_hook_gives_up_after_cap(ari, repo):
    rid = ari.start({"objective": "o"}, actor="agent:harness")
    for _ in range(claude_code.DEFAULT_MAX_STOP_BLOCKS):
        assert claude_code.stop_hook(payload(repo), ari=ari).get("decision") == "block"
    out = claude_code.stop_hook(payload(repo), ari=ari)
    assert "decision" not in out and "gate blocks" in out["systemMessage"]
    assert ari.state(rid).disposition is None  # still open, never accepted


def test_stop_hook_is_inert_when_gate_off_or_no_run(ari, repo):
    assert claude_code.stop_hook(payload(repo), ari=ari) == {}
    ari.start({"objective": "o"}, actor="agent:harness")
    set_gate(repo, False)
    assert claude_code.stop_hook(payload(repo), ari=ari) == {}


def test_session_start_injects_contract(ari, repo):
    ari.start({"objective": "make app ok"}, actor="agent:harness")
    out = claude_code.session_start_hook(payload(repo), ari=ari)
    assert "make app ok" in out["hookSpecificOutput"]["additionalContext"]


def test_install_merges_hooks_idempotently(repo):
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    claude_code.install(repo)
    claude_code.install(repo)
    doc = json.loads(settings.read_text())
    cmds = [h["command"] for g in doc["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds.count("echo mine") == 1
    assert sum("daedalus hook stop" in c for c in cmds) == 1
    assert (repo / ".claude" / "skills" / "daedalus-gate" / "SKILL.md").exists()


def test_cli_status_and_verify(repo, capsys, monkeypatch):
    from daedalus import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["-C", str(repo), "start", "make app ok", "-c", "app says ok"]) == 0
    fix(repo)
    cli.main(["-C", str(repo), "verify"])
    assert cli.main(["-C", str(repo), "status"]) == 0
    out = capsys.readouterr().out
    assert "tests" in out and "PASS" in out
    # C1 has no check, so it needs a human attestation: finishing stays BLOCKED.
    assert cli.main(["-C", str(repo), "finish"]) == 1
    assert cli.main(["-C", str(repo), "attest", "C1", "pass", "--evidence", "x", "-y"]) == 2
    assert cli.main(["-C", str(repo), "audit"]) == 0
    assert "audit chain intact" in capsys.readouterr().out


def test_hooks_ignore_daedalus_launched_sessions(ari, repo, monkeypatch):
    rid = ari.start({"objective": "make app ok"}, actor="agent:harness")
    before = len(ari.store.events(rid))
    monkeypatch.setenv("DAEDALUS_AGENT", "1")
    assert claude_code.stop_hook(payload(repo), ari=ari) == {}
    assert claude_code.session_start_hook(payload(repo), ari=ari) == {}
    assert claude_code.run_hook("stop", json.dumps(payload(repo))) == ""
    assert claude_code.run_hook("session-start", json.dumps(payload(repo))) == ""
    assert len(ari.store.events(rid)) == before  # no verify, no stop block, no disposition
