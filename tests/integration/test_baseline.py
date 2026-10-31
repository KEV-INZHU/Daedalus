"""A required check that fails is run once on the base revision, so a failure the change did not
cause is labelled as such. Advice only: the check must still pass."""

from __future__ import annotations

import sys

from conftest import HUMAN, fix, make_repo
from daedalus.adapters.base import ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core import run as ev
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import launch_review

NOTE = "also fails on a clean checkout of the base revision"


def _base_events(a: Ariadne, rid: str):
    return [e for e in a.store.events(rid) if e.type == ev.BASE_CHECKED]


def test_a_failure_already_on_the_base_is_labelled_and_checked_once(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review"})  # app.txt is "bad" on the base: the check fails there
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    (root / "src" / "lib.py").write_text("x = 2\n")  # an unrelated change; the check still fails
    a.verify(rid)
    d = a.evaluate(rid)
    msg = next(r.message for r in d.reasons if r.code == "check:tests")
    assert NOTE in msg and "It must still pass before acceptance" in msg and "base log:" in msg
    assert not d.acceptable  # advice, not a waiver
    a.verify(rid)
    assert len(_base_events(a, rid)) == 1  # same base and check: not run again
    rev = SimulatedAdapter({"aristotle": [ScriptedStep(structured={"summary": "s", "findings": []})]})
    launch_review(a, rid, "aristotle", rev)
    assert NOTE in rev.requests[0].prompt
    a.close()


def test_a_failure_the_change_caused_has_no_label(tmp_path, clock):
    root = make_repo(tmp_path / "r")
    fix(root)  # the base passes once app.txt says ok
    from conftest import git

    git(root, "commit", "-qam", "passing base")
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    (root / "app.txt").write_text("bad again\n")
    a.verify(rid)
    assert [e.payload["result"] for e in _base_events(a, rid)] == ["PASS"]
    msg = next(r.message for r in a.evaluate(rid).reasons if r.code == "check:tests")
    assert NOTE not in msg
    a.close()


def test_the_base_runs_its_own_code_not_the_working_trees(tmp_path, clock):
    check = [sys.executable, "-c", "import lib, sys; sys.exit(0 if lib.x == 1 else 1)"]
    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": check, "timeout_s": 60}}})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    (root / "src" / "lib.py").write_text("x = 2\n")
    # The candidate's check imports src/lib.py through PYTHONPATH only if the check sets it; here the
    # working tree's check runs from the repository root, where `lib` is not importable at all.
    a.verify(rid)
    (e,) = _base_events(a, rid)
    assert e.payload["result"] == "PASS"  # the base checkout's src/lib.py (x = 1) was the one imported
    a.close()


def test_compare_base_false_turns_it_off(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"compare_base": False})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    a.verify(rid)
    assert not _base_events(a, rid)
    a.close()


def test_runs_that_share_a_base_reuse_the_result(tmp_path, clock, monkeypatch):
    from daedalus.verification import baseline

    root = make_repo(tmp_path / "r")
    a = Ariadne(root, clock=clock)
    first = a.start({"objective": "o"}, actor=HUMAN)
    a.verify(first)
    a.cancel(first, actor=HUMAN)
    calls = []
    real = baseline.run_check
    monkeypatch.setattr(baseline, "run_check", lambda *x, **k: calls.append(1) or real(*x, **k))
    second = a.start({"objective": "o2"}, actor=HUMAN)
    a.verify(second)
    assert not calls and [e.payload["result"] for e in _base_events(a, second)] == ["FAIL"]
    a.close()


def test_a_bad_compare_base_setting_is_refused(tmp_path):
    import pytest

    from daedalus.core.config import load_config
    from daedalus.core.errors import PolicyError

    root = make_repo(tmp_path / "r", {"compare_base": "yes"})
    with pytest.raises(PolicyError, match="compare_base"):
        load_config(root)


def test_a_flaky_base_failure_is_not_recorded_as_failing(tmp_path, clock, monkeypatch):
    from daedalus.verification import baseline

    root = make_repo(tmp_path / "r")
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    real, n = baseline.run_check, []

    def flaky(defn, cwd, *x, **k):
        n.append(1)
        out = real(defn, cwd, *x, **k)
        if len(n) == 2:  # the repeat on the base passes
            from dataclasses import replace

            return replace(out, result="PASS", exit_code=0)
        return out

    monkeypatch.setattr(baseline, "run_check", flaky)
    a.verify(rid)
    (e,) = _base_events(a, rid)
    assert e.payload["result"] == "ERROR" and "flaky" in e.payload["error"]
    assert not list((a.state_dir / "baseline").glob("*.json"))  # nothing cached
    assert NOTE not in next(r.message for r in a.evaluate(rid).reasons if r.code == "check:tests")
    a.close()
