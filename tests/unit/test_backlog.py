"""Phase 1 follow-ups: console encoding, policy-aware metrics, the agent marker."""

from __future__ import annotations

import io
import sys

from conftest import HUMAN, fix, make_repo
from daedalus.adapters.base import AGENT_MARKER
from daedalus.core import run as ev
from daedalus.core.metrics import collect, run_metrics
from daedalus.orchestration.ariadne import Ariadne


def test_cli_output_is_utf8_when_stdout_is_not(monkeypatch):
    from daedalus import cli

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    cli.utf8_output()
    sys.stdout.write("run · ok")
    sys.stdout.flush()
    assert raw.getvalue() == "run · ok".encode()


def test_first_pass_uses_pinned_blocking_severities(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"policy": {"blocking_severities": ["BLOCKER", "MAJOR"]}})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(root)
    a.verify(rid)
    a.submit_review(
        rid,
        reviewer="agent:rev",
        perspective="aristotle",
        findings=[{"title": "t", "evidence": "e", "severity": "MAJOR"}],
        source="launched",
    )
    a.resolve_finding(rid, "F1", actor="agent:rev", note="fixed")
    a.finish(rid)
    m = run_metrics(a.store.events(rid))
    assert m.disposition == "ACCEPTED" and m.blocking_findings == 1 and not m.first_pass
    a.close()


def test_any_unreadable_run_is_isolated(ari):
    good = ari.start({"objective": "o"}, actor=HUMAN)
    ari.cancel(good, actor=HUMAN)
    ari.store.append("malformed", ev.RUN_CREATED, "human:x", {"mode": "gate"}, 0.0)  # KeyError on replay
    runs, errors = collect(ari.store)
    assert [m.run_id for m in runs] == [good]
    assert errors["malformed"].startswith("KeyError")


def test_agent_marker_is_shared(monkeypatch):
    from daedalus import cli
    from daedalus.adapters.command import agent_env
    from daedalus.harness.claude_code import launched_by_daedalus

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv(AGENT_MARKER, raising=False)
    assert not cli.in_agent_session() and not launched_by_daedalus()
    assert agent_env()[AGENT_MARKER] == "1"
    monkeypatch.setenv(AGENT_MARKER, "1")
    assert cli.in_agent_session() and launched_by_daedalus()


def test_review_reason_uses_correct_article(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN, mode="review")
    msg = next(r.message for r in ari.evaluate(rid).reasons if r.code == "review:aristotle")
    assert "requires an aristotle review" in msg


def test_run_with_wrongly_typed_payload_is_isolated(ari):
    good = ari.start({"objective": "o"}, actor=HUMAN)
    ari.cancel(good, actor=HUMAN)
    ari.store.append("typed", ev.RUN_CREATED, "human:x", {"mode": "gate", "policy": "x", "policy_hash": "h",
                                                          "contract": {}}, 0.0)
    runs, errors = collect(ari.store)
    assert [m.run_id for m in runs] == [good] and "typed" in errors


def test_hook_reads_stdin_as_utf8(repo, monkeypatch, capsys):
    import json

    from daedalus import cli

    payload = json.dumps({"cwd": str(repo), "note": "café"}, ensure_ascii=False).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252"))
    seen = {}
    monkeypatch.setattr("daedalus.harness.claude_code.run_hook", lambda name, text: seen.setdefault("t", text) and "")
    assert cli.main(["hook", "stop"]) == 0
    assert "café" in seen["t"]
