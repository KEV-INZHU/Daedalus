"""Legal transitions, replay integrity and audit-chain tamper detection."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import HUMAN
from daedalus.core import run as ev
from daedalus.core.errors import AuditIntegrityError, IllegalTransition
from daedalus.core.state_machine import (
    ExecutionState,
    WorkerState,
    check_execution_transition,
    check_worker_transition,
)


def test_finished_is_terminal():
    for target in ExecutionState:
        with pytest.raises(IllegalTransition):
            check_execution_transition(ExecutionState.FINISHED, target)


def test_illegal_execution_transition_is_rejected():
    with pytest.raises(IllegalTransition):
        check_execution_transition(ExecutionState.CANCELLING, ExecutionState.RUNNING)


def test_terminal_worker_states_cannot_move():
    for s in (WorkerState.FAILED, WorkerState.CANCELLED, WorkerState.REJECTED_STALE):
        with pytest.raises(IllegalTransition):
            check_worker_transition(s, WorkerState.COMPLETED)


def test_ariadne_rejects_illegal_transition(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    with pytest.raises(IllegalTransition):
        ari._transition(ari.state(rid), ExecutionState.QUEUED, "system:test", "rewind")


def test_illegal_transition_in_log_is_corruption_not_repair(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    # Bypass Ariadne's validation and write a forbidden event directly.
    ari.store.append(rid, ev.EXECUTION, "system:rogue", {"from": "RUNNING", "to": "QUEUED"}, 0.0)
    with pytest.raises(AuditIntegrityError):
        ari.state(rid)


def test_finished_run_rejects_further_work(ari, repo):
    from conftest import fix

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.finish(rid)
    with pytest.raises(IllegalTransition):
        ari.raise_blocker(rid, "late", actor=HUMAN)
    with pytest.raises(IllegalTransition):
        ari.verify(rid)


def test_audit_tampering_is_detected(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.raise_blocker(rid, "real", actor=HUMAN)
    assert ari.store.verify_chain() > 0
    db = sqlite3.connect(str(repo / ".daedalus" / "daedalus.db"))
    db.execute(
        "UPDATE events SET payload = ? WHERE type = ?",
        ('{"blocker_id":"B1","description":"forged"}', ev.BLOCKER_RAISED),
    )
    db.commit()
    db.close()
    with pytest.raises(AuditIntegrityError):
        ari.store.verify_chain()


def test_actor_must_be_a_principal(ari):
    from daedalus.core.errors import AuthorizationError

    with pytest.raises(AuthorizationError):
        ari.start({"objective": "o"}, actor="kevin")


def test_one_open_run_at_a_time(ari):
    from daedalus.core.errors import DaedalusError

    ari.start({"objective": "o"}, actor=HUMAN)
    with pytest.raises(DaedalusError):
        ari.start({"objective": "o2"}, actor=HUMAN)
