"""Crash recovery: nothing is presumed to have succeeded (spec §13)."""

from __future__ import annotations

import pytest

from conftest import HUMAN, fix
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core import run as ev
from daedalus.core.errors import AuthorizationError
from daedalus.core.state_machine import CheckState, Disposition, WorkerState
from daedalus.orchestration.ariadne import Ariadne


def test_check_interrupted_by_crash_is_error_not_pass(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    cid = ari.capture(ari.state(rid)).candidate_id
    ari._append(
        rid,
        ev.CHECK_STARTED,
        "system:proof-gate",
        {"key": "tests#x", "check_id": "tests", "attempt": 1, "candidate_id": cid, "started_at": 0.0},
    )
    assert "checks_in_flight" in {r.code for r in ari.evaluate(rid).reasons}
    assert ari.needs_recovery(rid)
    notes = ari.recover(rid)
    assert any("interrupted" in n for n in notes)
    d = ari.evaluate(rid)
    assert d.check_states["tests"][0] is CheckState.ERROR
    assert d.disposition is not Disposition.ACCEPTED


def test_untraceable_worker_is_failed_after_restart(repo, clock):
    a = Ariadne(repo, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    a.plan(rid, [{"task_id": "t1", "owned_paths": ["src/**"]}], actor="system:test")
    a.dispatch(rid, "t1")
    a.close()  # orchestrator "dies" with the worker dispatched
    b = Ariadne(repo, clock=clock, adapter=SimulatedAdapter())
    assert b.needs_recovery(rid)
    b.recover(rid)
    assert b.state(rid).workers["t1"].state is WorkerState.FAILED
    b.close()


def test_restricted_action_with_unknown_outcome_requires_reconciliation(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    assert ari.finish(rid).disposition is Disposition.ACCEPTED
    ari.approve(rid, "merge", actor=HUMAN, single_use=False)

    def crash() -> int:
        raise KeyboardInterrupt  # BaseException: the process dies mid-action

    with pytest.raises(KeyboardInterrupt):
        ari.execute_action(rid, "merge", actor=HUMAN, runner=crash)
    ari.lock.release()  # the dead process's lock (released here; stale-lock takeover is tested below)
    ari.recover(rid)
    assert ari.state(rid).unknown_actions
    with pytest.raises(AuthorizationError, match="unknown outcome"):
        ari.execute_action(rid, "merge", actor=HUMAN, runner=lambda: 0)
    (aid,) = ari.state(rid).unknown_actions
    with pytest.raises(AuthorizationError):
        ari.reconcile_action(rid, aid, occurred=False, actor="agent:builder")
    ari.reconcile_action(rid, aid, occurred=False, actor=HUMAN)
    assert ari.execute_action(rid, "merge", actor=HUMAN, runner=lambda: 0)["ok"]


def test_stale_lock_is_taken_over(repo, clock):
    a = Ariadne(repo, clock=clock)
    a.lock.path.write_text('{"pid": 999999999, "host": "%s"}' % __import__("socket").gethostname())
    with a.lock:
        assert a.lock.took_over_stale
    a.close()


def test_live_lock_is_respected(repo, clock):
    import os
    import socket

    from daedalus.core.errors import DaedalusError

    a = Ariadne(repo, clock=clock)
    a.lock.path.write_text('{"pid": %d, "host": "%s"}' % (os.getppid(), socket.gethostname()))
    with pytest.raises(DaedalusError, match="holds the orchestrator lock"):
        a.lock.acquire()
    a.lock.path.unlink()
    a.close()
