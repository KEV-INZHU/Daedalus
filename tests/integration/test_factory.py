"""The factory line: orders in, gated commits out, failures parked, humans asked only when needed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import HUMAN, git, make_repo
from daedalus.adapters.base import AgentResult, ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.factory.ledger import Ledger
from daedalus.factory.line import Line, factory_config, park_waiting, requeue
from daedalus.factory.orders import FactoryError, load_orders
from daedalus.orchestration.ariadne import Ariadne

DONE = {"status": "done", "summary": "did it"}


def out(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def add_orders(root: Path, orders: dict[str, dict], *, commit: bool = True) -> None:
    d = root / "factory" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    for oid, body in orders.items():
        (d / f"{oid}.yml").write_text(yaml.safe_dump(body), encoding="utf-8")
    if commit:
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "orders")


def build(edits: dict[str, str]) -> ScriptedStep:
    return ScriptedStep(edits=edits, structured=DONE, cost=0.25)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo", {"factory": {"andon": 2}})


@pytest.fixture
def ari(repo, clock):
    a = Ariadne(repo, clock=clock)
    yield a
    a.close()


def line(ari, adapter, **kw) -> Line:
    return Line(ari, adapter, actor=HUMAN, **kw)


def test_orders_ship_in_dependency_order_as_one_commit_each(ari, repo):
    add_orders(
        repo,
        {
            "a-lib": {"objective": "Bump lib", "depends_on": ["z-app"], "priority": 100},
            "z-app": {"objective": "Make the app ok", "acceptance_criteria": ["app.txt says ok"]},
        },
    )
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"}), build({"src/lib.py": "x = 2\n"})]})
    base = out(repo, "rev-parse", "HEAD")
    report = line(ari, adapter).run()

    assert report.shipped == ["z-app", "a-lib"]  # priority never jumps a dependency
    assert report.stopped == "no queued orders"
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    log = out(repo, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert log == ["Bump lib", "Make the app ok"]
    msg = out(repo, "log", "-1", "--format=%B", "HEAD~1")
    assert "- app.txt says ok" in msg and "Daedalus-Order: z-app" in msg and "Daedalus-Run: " in msg
    assert (repo / "src" / "lib.py").read_text() == "x = 2\n"
    assert out(repo, "branch", "--list", "factory/*") == ""  # shipped branches are cleaned up
    assert out(repo, "status", "--porcelain") == ""
    recs = Ledger(repo).records()
    assert recs["z-app"].status == "shipped" and recs["z-app"].first_pass
    assert recs["z-app"].cost == pytest.approx(0.25)
    assert report.cost == pytest.approx(0.5)


def test_shipped_commit_is_exactly_the_accepted_candidate(ari, repo):
    # A hook that rewrites files would make the commit differ from what was accepted.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho tampered > app.txt\ngit add app.txt\n")
    hook.chmod(0o755)
    add_orders(repo, {"fix": {"objective": "fix app"}})
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "ok\n", "new/file.txt": "hi\n"})]})
    report = line(ari, adapter).run()
    assert report.shipped == ["fix"]
    assert out(repo, "show", "HEAD:app.txt") == "ok"
    assert out(repo, "show", "HEAD:new/file.txt") == "hi"
    run = ari.state(Ledger(repo).records()["fix"].run_id)
    assert run.disposition.value == "ACCEPTED"


def test_failing_order_is_parked_with_its_work_kept_and_the_line_moves_on(ari, repo):
    add_orders(
        repo,
        {
            "a-broken": {"objective": "never passes", "rounds": 2, "priority": 5},
            "b-fix": {"objective": "fix app"},
        },
    )
    adapter = SimulatedAdapter(
        {"brunel": [build({"app.txt": "nope\n"}), build({"app.txt": "still nope\n"}), build({"app.txt": "ok\n"})]}
    )
    report = line(ari, adapter).run()
    assert report.parked == ["a-broken"] and report.shipped == ["b-fix"]
    rec = Ledger(repo).records()["a-broken"]
    assert rec.status == "parked" and any("tests" in r for r in rec.reasons)
    assert out(repo, "show", "factory/a-broken:app.txt") == "still nope"  # work in progress kept
    assert ari.state(rec.run_id).disposition.value == "CANCELLED"
    assert (repo / "app.txt").read_text() == "ok\n"


def test_andon_stops_the_line_after_consecutive_failures(ari, repo):
    add_orders(repo, {f"o{i}": {"objective": f"order {i}", "rounds": 1} for i in range(4)})
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": f"bad {i}\n"}) for i in range(4)]})
    report = line(ari, adapter).run()
    assert report.parked == ["o0", "o1"]
    assert report.stopped.startswith("andon: 2 orders in a row")
    assert Ledger(repo).records().get("o2") is None


def test_human_gate_halts_the_line_and_the_next_run_resumes_it(ari, repo):
    add_orders(
        repo,
        {
            "a-ui": {
                "objective": "fix app",
                "acceptance_criteria": [{"description": "looks right to a person", "checks": []}],
                "priority": 1,
            },
            "b-lib": {"objective": "lib", "depends_on": []},
        },
    )
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"}), build({"src/lib.py": "x = 3\n"})]})
    report = line(ari, adapter).run()
    assert report.waiting == "a-ui" and not report.shipped
    rid = Ledger(repo).records()["a-ui"].run_id
    assert ari.active_run_id() == rid
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "factory/a-ui"

    ari.attest(rid, "C1", "pass", actor=HUMAN, evidence="checked it")
    report = line(ari, adapter).run()
    assert report.shipped == ["a-ui", "b-lib"]
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert len(adapter.requests) == 2  # resuming a human-gated run does not rebuild it


def test_a_waiting_order_can_be_parked_by_a_human(ari, repo):
    add_orders(
        repo,
        {"ui": {"objective": "fix app", "acceptance_criteria": [{"description": "eyeball it", "checks": []}]}},
    )
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})
    assert line(ari, adapter).run().waiting == "ui"
    assert park_waiting(ari, "ui", actor=HUMAN) == "main"
    assert ari.active_run_id() is None
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert Ledger(repo).records()["ui"].status == "parked"


def test_retry_rebuilds_a_parked_order_from_its_branch(ari, repo):
    add_orders(repo, {"fix": {"objective": "fix app", "rounds": 1}})
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "almost\n"})]})
    assert line(ari, adapter).run().parked == ["fix"]
    with pytest.raises(FactoryError):
        requeue(ari, "nope", actor=HUMAN)
    requeue(ari, "fix", actor=HUMAN)
    seen = []

    def second(req):
        seen.append((req.cwd / "app.txt").read_text())
        (req.cwd / "app.txt").write_text("ok\n")
        return AgentResult("COMPLETED", structured=DONE)

    adapter = SimulatedAdapter({"brunel": [second]})
    report = line(ari, adapter).run()
    assert seen == ["almost\n"]  # the builder starts from the parked work
    assert report.shipped == ["fix"]
    assert Ledger(repo).records()["fix"].attempts == 2
    assert not Ledger(repo).records()["fix"].first_pass
    assert out(repo, "log", "-1", "--format=%s") == "fix app"  # one commit, WIP squashed away
    assert out(repo, "show", "HEAD:app.txt") == "ok"


def test_line_refuses_a_dirty_tree_and_uncommitted_orders(ari, repo):
    add_orders(repo, {"fix": {"objective": "fix app"}}, commit=False)
    with pytest.raises(FactoryError, match="uncommitted"):
        line(ari, SimulatedAdapter()).run()


def test_line_refuses_an_unrelated_open_run(ari, repo):
    add_orders(repo, {"fix": {"objective": "fix app"}})
    ari.start({"objective": "manual"}, actor=HUMAN)
    with pytest.raises(FactoryError, match="not linked to a factory order"):
        line(ari, SimulatedAdapter()).run()


def test_stop_request_and_limits_end_the_line_between_orders(ari, repo):
    add_orders(repo, {"a": {"objective": "fix app"}, "b": {"objective": "lib"}})
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"}), build({"src/lib.py": "y = 1\n"})]})
    report = line(ari, adapter, max_orders=1).run()
    assert report.shipped == ["a"] and report.stopped.startswith("reached --max-orders")
    Ledger(repo).request_stop()
    # a stop requested before a line starts is stale and is cleared
    report = line(ari, adapter, budget=0.0).run()
    assert report.stopped.startswith("line budget spent") and report.shipped == []


def test_bad_orders_are_reported_and_skipped(ari, repo):
    add_orders(
        repo,
        {
            "Bad_ID": {"objective": "x"},
            "loop-a": {"objective": "x", "depends_on": ["loop-b"]},
            "loop-b": {"objective": "x", "depends_on": ["loop-a"]},
            "orphan": {"objective": "x", "depends_on": ["ghost"]},
            "unknown-field": {"objective": "x", "colour": "red"},
            "fix": {"objective": "fix app"},
        },
    )
    orders, errors = load_orders(repo, "factory/orders", ari.config().policy)
    assert [o.order_id for o in orders] == ["fix"]
    assert set(errors) == {f"factory/orders/{n}.yml" for n in ("Bad_ID", "loop-a", "loop-b", "orphan", "unknown-field")}
    report = line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run()
    assert report.shipped == ["fix"] and len(report.notes) == 5


def test_blocked_dependency_is_explained(ari, repo):
    add_orders(repo, {"a": {"objective": "x", "rounds": 1}, "b": {"objective": "y", "depends_on": ["a"]}})
    report = line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "bad\n"})]})).run()
    assert report.parked == ["a"]
    assert "wait on dependencies that have not shipped: a" in report.stopped


def test_factory_config_validation(tmp_path):
    root = make_repo(tmp_path / "r", {"factory": {"branch_prefix": ""}})
    with pytest.raises(FactoryError, match="branch_prefix"):
        factory_config(root)
    root = make_repo(tmp_path / "s", {"factory": {"orders": "../elsewhere"}})
    with pytest.raises(FactoryError, match="inside the repository"):
        factory_config(root)
    root = make_repo(tmp_path / "t", {"factory": {"speed": 11}})
    with pytest.raises(FactoryError, match="unknown"):
        factory_config(root)
    assert factory_config(make_repo(tmp_path / "u")).orders_dir == "factory/orders"


def test_an_accepted_order_that_changes_nothing_is_not_counted_as_shipped(ari, repo):
    (repo / "app.txt").write_text("ok\n")
    git(repo, "commit", "-qam", "already ok")
    add_orders(repo, {"noop": {"objective": "do something"}})
    report = line(ari, SimulatedAdapter({"brunel": [build({})]})).run()
    assert report.shipped == [] and report.parked == ["noop"]
    assert "changed nothing" in Ledger(repo).records()["noop"].reasons[0]


# ------------------------------------------------------------------ planner
from daedalus.factory.planner import plan_orders


def planner(body, edits=None) -> SimulatedAdapter:
    return SimulatedAdapter({"vitruvius": [ScriptedStep(edits=edits or {}, structured=body, cost=0.1)]})


def test_planner_proposals_are_validated_like_hand_written_orders(ari, repo):
    add_orders(repo, {"existing": {"objective": "already queued"}})
    body = {
        "summary": "split",
        "orders": [
            {"id": "API Keys", "objective": "keys", "acceptance_criteria": ["a key is issued"], "scope": ["src/**"]},
            {"id": "limits", "objective": "limits", "depends_on": ["API Keys", "existing"]},
            {"id": "existing", "objective": "clashes with an existing id"},
            {"id": "orphan", "objective": "x", "depends_on": ["ghost"]},
            {"id": "needs-orphan", "objective": "x", "depends_on": ["orphan"]},
            {"id": "c1", "objective": "x", "depends_on": ["c2"]},
            {"id": "c2", "objective": "x", "depends_on": ["c1"]},
            {"id": "bad", "objective": "x", "risk_tier": "galactic"},
            "not a mapping",
        ],
    }
    adapter = planner(body)
    plan = plan_orders(ari, adapter, "rate limiting", max_orders=20)
    ids = [oid for oid, _ in plan.orders]
    assert ids == ["api-keys", "limits", "existing-2"]
    assert dict(plan.orders)["limits"]["depends_on"] == ["api-keys", "existing"]
    dropped = " | ".join(plan.dropped)
    for oid in ("orphan", "needs-orphan", "c1", "c2", "bad", "#9"):
        assert oid in dropped
    req = adapter.requests[0]
    assert req.read_only and req.role == "vitruvius" and "existing (queued): already queued" in req.prompt
    assert plan.cost == pytest.approx(0.1)


def test_planner_that_edits_files_is_discarded(ari, repo):
    with pytest.raises(FactoryError, match="changed the working tree"):
        plan_orders(ari, planner({"orders": []}, edits={"app.txt": "sneaky\n"}), "goal")


def test_planner_without_a_plan_fails_closed(ari, repo):
    with pytest.raises(FactoryError, match="no valid JSON plan"):
        plan_orders(ari, planner({"summary": "no orders key"}), "goal")


# ---------------------------------------------------------------------- cli
def test_cli_add_status_retry_and_stop(repo, capsys, monkeypatch):
    from daedalus import cli

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("DAEDALUS_AGENT", raising=False)
    d = str(repo)
    assert cli.main(["-C", d, "factory", "add", "Make the app OK", "-c", "app says ok", "--priority", "3"]) == 0
    assert "factory/orders/make-the-app-ok.yml" in capsys.readouterr().out
    assert cli.main(["-C", d, "factory", "add", "Make the app OK"]) == 2  # never overwrites
    assert "already exists" in capsys.readouterr().err
    body = yaml.safe_load((repo / "factory/orders/make-the-app-ok.yml").read_text())
    assert body == {"objective": "Make the app OK", "acceptance_criteria": ["app says ok"], "priority": 3}
    assert cli.main(["-C", d, "factory", "status"]) == 0
    text = capsys.readouterr().out
    assert "make-the-app-ok  ready" in text and "next: make-the-app-ok" in text
    assert cli.main(["-C", d, "factory", "status", "--json"]) == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert data["next"] == "make-the-app-ok" and data["orders"][0]["status"] == "ready"
    assert cli.main(["-C", d, "factory", "retry", "make-the-app-ok"]) == 2
    assert cli.main(["-C", d, "factory", "stop"]) == 0
    assert Ledger(repo).take_stop()
    # the inline adapter cannot launch agents: the line refuses to start
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "order")
    assert cli.main(["-C", d, "factory", "run"]) == 2
    assert "launch_agent" in capsys.readouterr().err


def test_an_interrupted_order_is_resumed_by_the_next_line(ari, repo):
    add_orders(repo, {"fix": {"objective": "fix app"}})

    def crash(req):
        (req.cwd / "app.txt").write_text("half\n")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        line(ari, SimulatedAdapter({"brunel": [crash]})).run()
    rec = Ledger(repo).records()["fix"]
    assert rec.status == "building" and ari.active_run_id() == rec.run_id

    git(repo, "stash", "-u")  # someone switched away; the line must not guess where the work is
    git(repo, "checkout", "-q", "main")
    with pytest.raises(FactoryError, match="git checkout factory/fix"):
        line(ari, SimulatedAdapter()).run()
    git(repo, "checkout", "-q", "factory/fix")
    git(repo, "stash", "pop")

    report = line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run()
    assert report.shipped == ["fix"]
    assert out(repo, "show", "main:app.txt") == "ok"
    state = ari.state(rec.run_id)
    assert state.disposition.value == "ACCEPTED"
    workers = {k: w.state.value for k, w in state.workers.items()}
    assert workers == {"build-1": "FAILED", "build-1.2": "COMPLETED"}  # the crashed session is not presumed done


def test_escape_rate_from_fixes_order_trailers(ari, repo, capsys):
    import json

    from daedalus import cli

    add_orders(repo, {"one": {"objective": "one"}, "two": {"objective": "two"}})
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "ok\n", "one.txt": "1\n"}), build({"app.txt": "ok\n", "two.txt": "2\n"})]})
    line(ari, adapter).run()
    assert {r.status for r in Ledger(repo).records().values()} == {"shipped"}
    d = str(repo)

    def status() -> dict:
        assert cli.main(["-C", d, "factory", "status", "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    assert status()["summary"]["escaped"] == 0  # nothing names them yet
    (repo / "fix.txt").write_text("fix\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix it\n\nFixes-Order: one\nFixes-Order: never-shipped")
    data = status()
    assert data["summary"]["escaped_orders"] == ["one"] and data["summary"]["escape_rate"] == 0.5
    assert {o["id"]: o["escaped"] for o in data["orders"]} == {"one": True, "two": False}
    assert cli.main(["-C", d, "factory", "status"]) == 0
    assert "escaped 1 of 2 shipped · escape rate 50%" in capsys.readouterr().out


def test_only_one_line_runs_at_a_time(ari, repo):
    import json as _json
    import os

    add_orders(repo, {"fix": {"objective": "fix app"}})
    lock = repo / ".daedalus" / "factory" / "line.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(_json.dumps({"pid": os.getpid(), "host": __import__("socket").gethostname()}))
    with pytest.raises(FactoryError, match="a factory line is running"):
        line(ari, SimulatedAdapter()).run()
    lock.unlink()
    lock.write_text(_json.dumps({"pid": 2**22 + 12345, "host": __import__("socket").gethostname()}))  # dead holder
    assert line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run().shipped == ["fix"]
    assert not lock.exists()


def test_order_title_is_a_clean_commit_subject():
    from daedalus.factory.orders import WorkOrder

    assert WorkOrder("a", "p", {"objective": "Fix the app. Then more\ndetail."}).title == "Fix the app"
    long = WorkOrder("b", "p", {"objective": "word " * 30}).title
    assert len(long) <= 72 and long.endswith("word…")


def test_retry_of_a_branch_that_conflicts_with_the_integration_branch_is_parked(ari, repo):
    add_orders(repo, {"a": {"objective": "first try", "rounds": 1, "priority": 1}, "b": {"objective": "fix app"}})
    adapter = SimulatedAdapter({"brunel": [build({"app.txt": "conflicting\n"}), build({"app.txt": "ok\n"})]})
    report = line(ari, adapter).run()
    assert report.parked == ["a"] and report.shipped == ["b"]
    requeue(ari, "a", actor=HUMAN)
    report = line(ari, SimulatedAdapter()).run()
    assert report.parked == ["a"]
    assert "conflicts with main" in Ledger(repo).records()["a"].reasons[0]
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and out(repo, "status", "--porcelain") == ""


# ------------------------------------------- outcomes keep work; ship refuses drift
def test_a_rejected_order_keeps_its_work_on_its_branch(ari, repo):
    # Over budget with a failing check: the run is REJECTED.
    add_orders(repo, {"pricey": {"objective": "x", "budget": {"max_cost": 0.1}, "rounds": 2}})
    report = line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "costly\n"})] * 2})).run()
    rec = Ledger(repo).records()["pricey"]
    assert rec.status == "rejected" and report.parked == ["pricey"]
    assert ari.state(rec.run_id).disposition.value == "REJECTED"
    assert out(repo, "show", "factory/pricey:app.txt") == "costly"
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and out(repo, "status", "--porcelain") == ""


def test_a_cancelled_order_keeps_its_work_and_stops_the_line(ari, repo):
    add_orders(repo, {"a": {"objective": "x", "priority": 1}, "b": {"objective": "y"}})

    def cancelled_mid_build(req):
        (req.cwd / "app.txt").write_text("half done\n")
        ari.cancel(req.run_id, actor=HUMAN, reason="changed my mind")
        return AgentResult("CANCELLED", error="cancelled")

    report = line(ari, SimulatedAdapter({"brunel": [cancelled_mid_build]})).run()
    rec = Ledger(repo).records()["a"]
    assert rec.status == "cancelled" and report.stopped == "order a was cancelled"
    assert Ledger(repo).records().get("b") is None
    assert out(repo, "show", "factory/a:app.txt") == "half done"
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and out(repo, "status", "--porcelain") == ""


def human_gated(repo) -> None:
    add_orders(repo, {"ui": {"objective": "fix app", "acceptance_criteria": [{"description": "eyeball", "checks": []}]}})


def test_nothing_ships_if_the_integration_branch_moved_while_waiting(ari, repo):
    human_gated(repo)
    assert line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run().waiting == "ui"
    rid = Ledger(repo).records()["ui"].run_id
    git(repo, "stash", "-u")
    git(repo, "checkout", "-q", "main")
    (repo / "src" / "lib.py").write_text("x = 99\n")
    git(repo, "commit", "-qam", "someone else's change")
    main_head = out(repo, "rev-parse", "main")
    git(repo, "checkout", "-q", "factory/ui")
    git(repo, "stash", "pop")
    ari.attest(rid, "C1", "pass", actor=HUMAN, evidence="looked")
    report = line(ari, SimulatedAdapter()).run()
    assert report.parked == ["ui"] and "moved since the order started" in Ledger(repo).records()["ui"].reasons[0]
    assert out(repo, "rev-parse", "main") == main_head  # main untouched
    assert out(repo, "show", "factory/ui:app.txt") == "ok"  # the accepted work is kept


def test_an_order_a_human_finished_is_shipped_by_the_next_line(ari, repo):
    human_gated(repo)
    assert line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run().waiting == "ui"
    rid = Ledger(repo).records()["ui"].run_id
    ari.attest(rid, "C1", "pass", actor=HUMAN, evidence="looked")
    assert ari.finish(rid).disposition.value == "ACCEPTED"  # `daedalus finish`, as the stop hook suggests
    report = line(ari, SimulatedAdapter()).run()
    assert report.shipped == ["ui"] and out(repo, "show", "main:app.txt") == "ok"


def test_nothing_ships_if_the_tree_changed_after_acceptance(ari, repo):
    human_gated(repo)
    assert line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run().waiting == "ui"
    rid = Ledger(repo).records()["ui"].run_id
    ari.attest(rid, "C1", "pass", actor=HUMAN, evidence="looked")
    ari.finish(rid)
    (repo / "app.txt").write_text("ok, plus an unreviewed edit\n")
    main_head = out(repo, "rev-parse", "main")
    report = line(ari, SimulatedAdapter()).run()
    assert report.parked == ["ui"] and "changed after acceptance" in Ledger(repo).records()["ui"].reasons[0]
    assert out(repo, "rev-parse", "main") == main_head


def test_park_refuses_while_a_line_is_running(ari, repo):
    import json as _json
    import os
    import socket

    human_gated(repo)
    assert line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run().waiting == "ui"
    lock = repo / ".daedalus" / "factory" / "line.lock"
    lock.write_text(_json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
    rid = Ledger(repo).records()["ui"].run_id
    with pytest.raises(FactoryError, match="a factory line is running"):
        park_waiting(ari, "ui", actor=HUMAN)
    assert ari.active_run_id() == rid and out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "factory/ui"


def test_a_torn_ledger_write_never_swallows_the_next_record(repo):
    led = Ledger(repo)
    led.append("order.requeued", order="a")
    with open(led.path, "a", encoding="utf-8") as f:
        f.write('{"type": "order.req')  # a crash mid-write
    led.append("order.requeued", order="b")
    assert [e["order"] for e in led.events()] == ["a", "b"]
    with open(led.path, "a", encoding="utf-8") as f:
        f.write('{"ts": 1729.')  # cut mid-number
    led.append("order.requeued", order="c")
    with open(led.path, "a", encoding="utf-8") as f:
        f.write('{"branch": nu')  # cut mid-literal
    led.append("order.requeued", order="d")
    assert [e["order"] for e in led.events()] == ["a", "b", "c", "d"] and led.skipped == 3


@pytest.mark.parametrize("how", ["dirty-file", "commit"])
def test_planner_changes_are_caught_however_they_are_made(ari, repo, how):
    (repo / "app.txt").write_text("already dirty\n")

    def sneaky(req):
        if how == "dirty-file":
            (req.cwd / "app.txt").write_text("edited again\n")
        else:
            git(req.cwd, "add", "-A")
            git(req.cwd, "commit", "-qm", "planner commit")
        return AgentResult("COMPLETED", structured={"orders": []})

    with pytest.raises(FactoryError, match="changed the working tree"):
        plan_orders(ari, SimulatedAdapter({"vitruvius": [sneaky]}), "goal")


def test_a_retried_order_ships_only_work_its_run_reviewed(ari, repo):
    add_orders(repo, {"fix": {"objective": "fix app", "rounds": 1}})
    wip = build({"app.txt": "almost\n", "src/extra.py": "y = 1\n"})
    assert line(ari, SimulatedAdapter({"brunel": [wip]})).run().parked == ["fix"]
    requeue(ari, "fix", actor=HUMAN)
    report = line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run()
    assert report.shipped == ["fix"]
    run = ari.state(Ledger(repo).records()["fix"].run_id)
    assert run.contract.base_revision == Ledger(repo).records()["fix"].into_head  # based on what it ships onto
    accepted = set(run.candidates[run.finalized_candidate_id]["manifest"]["files"])
    shipped = set(out(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines())
    assert shipped == accepted == {"app.txt", "src/extra.py"}  # the earlier WIP was in the reviewed candidate


def test_a_torn_record_that_ends_in_a_brace_is_still_recognised(repo):
    led = Ledger(repo)
    led.append("order.requeued", order="a")
    with open(led.path, "a", encoding="utf-8") as f:
        f.write('{"x": {"y": 1}')  # truncated, yet ends with '}'
    led.append("order.requeued", order="b")
    assert [e["order"] for e in led.events()] == ["a", "b"]


def test_the_shipped_commit_never_carries_what_the_builder_staged_outside_the_candidate(ari, repo):
    add_orders(repo, {"fix": {"objective": "fix app"}})

    def sneaky(req):
        (req.cwd / "app.txt").write_text("ok\n")
        local = req.cwd / ".claude" / "settings.local.json"  # excluded from the candidate by default
        local.parent.mkdir(exist_ok=True)
        local.write_text('{"hooks": {}}')
        git(req.cwd, "add", "-f", ".claude/settings.local.json")
        git(req.cwd, "commit", "-qm", "builder commits an unreviewed path")
        return AgentResult("COMPLETED", structured=DONE)

    report = line(ari, SimulatedAdapter({"brunel": [sneaky]})).run()
    assert report.shipped == ["fix"]
    files = set(out(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines())
    assert files == {"app.txt"}  # .claude/settings.local.json was not reviewed, so it did not ship


def test_park_from_the_integration_branch_refuses_to_strand_the_work(ari, repo):
    human_gated(repo)
    assert line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]})).run().waiting == "ui"
    git(repo, "checkout", "-q", "main")  # carries the uncommitted work along
    with pytest.raises(FactoryError, match="git checkout factory/ui"):
        park_waiting(ari, "ui", actor=HUMAN)
    git(repo, "checkout", "-q", "factory/ui")
    park_waiting(ari, "ui", actor=HUMAN)
    assert out(repo, "show", "factory/ui:app.txt") == "ok"


def test_fanout_ids_get_a_fresh_namespace_on_a_resumed_run():
    from daedalus.orchestration.lifecycle import _plan_packages

    body = {"packages": [{"id": "a", "owned_paths": ["a/**"]}, {"id": "b", "owned_paths": ["b/**"], "depends_on": ["a"]}]}
    first = _plan_packages(body, 1)
    again = _plan_packages(body, 1, {p["task_id"] for p in first})
    assert [p["task_id"] for p in again] == ["r1.2-a", "r1.2-b"] and again[1]["depends_on"] == ["r1.2-a"]


def test_ledger_skips_valid_json_that_is_not_a_record(repo):
    led = Ledger(repo)
    led.append("order.requeued", order="a")
    with open(led.path, "a", encoding="utf-8") as f:
        f.write('12\n{"order": "a"}\n')
    led.append("order.requeued", order="b")
    assert [e["order"] for e in led.events()] == ["a", "b"] and led.skipped == 2
    assert led.records()["b"].status == "queued"
