"""Worker lifecycle: plans, ownership, cancellation and stale results (spec §12, §14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import HUMAN
from daedalus.core.errors import DaedalusError, IntegrationError
from daedalus.core.state_machine import Disposition, WorkerState

SYS = "system:test"


def test_overlapping_ownership_is_rejected_before_dispatch(ari):
    rid = ari.start({"objective": "o", "budget": {"max_workers": 1}}, actor=HUMAN)
    with pytest.raises(IntegrationError, match="ownership overlap"):
        ari.plan(
            rid,
            [{"task_id": "a", "owned_paths": ["src/**"]}, {"task_id": "b", "owned_paths": ["src/lib.py"]}],
            actor=SYS,
        )


def test_interface_version_mismatch_is_rejected(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    with pytest.raises(IntegrationError, match="consumes"):
        ari.plan(
            rid,
            [
                {"task_id": "a", "owned_paths": ["src/a/**"], "provides": ["api"]},
                {"task_id": "b", "owned_paths": ["src/b/**"], "consumes": {"api": "2"}},
            ],
            actor=SYS,
            interfaces={"api": "1"},
        )


def test_dependency_must_integrate_before_dispatch(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(
        rid,
        [
            {"task_id": "a", "owned_paths": ["src/a/**"]},
            {"task_id": "b", "owned_paths": ["src/b/**"], "depends_on": ["a"]},
        ],
        actor=SYS,
    )
    with pytest.raises(DaedalusError, match="prerequisite a"):
        ari.dispatch(rid, "b")


def test_worker_result_after_cancellation_cannot_integrate(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    ari.dispatch(rid, "t")
    ari.cancel(rid, actor=HUMAN, reason="changed my mind")
    w = ari.worker_finished(
        rid, "t", status="COMPLETED", proposal={"files": {"src/lib.py": "x = 9\n"}, "base_hashes": {}}
    )
    assert w.state is WorkerState.CANCELLED
    with pytest.raises(IntegrationError):
        ari.integrate(rid, "t")
    assert (repo / "src" / "lib.py").read_text(encoding="utf-8") == "x = 1\n"
    assert ari.state(rid).disposition is Disposition.CANCELLED


def test_late_result_from_terminal_worker_is_rejected(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    ari.dispatch(rid, "t")
    ari.worker_finished(rid, "t", status="TIMED_OUT")
    with pytest.raises(IntegrationError, match="late result"):
        ari.worker_finished(rid, "t", status="COMPLETED", proposal={})


def _edit_workspace(ari, rid, task_id, files):
    """Act as the worker: edit the isolated workspace, not the repository."""
    ws = Path(ari.state(rid).workers[task_id].workspace)
    for rel, content in files.items():
        target = ws / rel
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
    return ws


def test_integration_respects_ownership(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/a/**"]}], actor=SYS)
    ari.dispatch(rid, "t")
    _edit_workspace(ari, rid, "t", {"src/a/new.py": "ok = 1\n", "src/lib.py": "x = 2\n"})
    ari.worker_finished(rid, "t", status="COMPLETED")
    assert set(ari.state(rid).workers["t"].proposal["files"]) == {"src/a/new.py", "src/lib.py"}
    with pytest.raises(IntegrationError, match="outside its ownership"):
        ari.integrate(rid, "t")
    assert ari.state(rid).workers["t"].state is WorkerState.REJECTED_STALE
    assert (repo / "src" / "lib.py").read_text(encoding="utf-8") == "x = 1\n"


def test_integration_rejects_stale_base(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    ari.dispatch(rid, "t")
    _edit_workspace(ari, rid, "t", {"src/lib.py": "x = 2\n"})
    (repo / "src" / "lib.py").write_text("x = 'moved on'\n", encoding="utf-8")
    ari.worker_finished(rid, "t", status="COMPLETED")
    with pytest.raises(IntegrationError, match="changed since"):
        ari.integrate(rid, "t")


def test_integration_creates_new_candidate(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    before = ari.state(rid).current_candidate_id
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    ari.dispatch(rid, "t")
    _edit_workspace(ari, rid, "t", {"src/new.py": "y = 1\n"})
    ari.worker_finished(rid, "t", status="COMPLETED")
    cand = ari.integrate(rid, "t")
    assert cand.candidate_id != before
    assert (repo / "src" / "new.py").exists()


def test_contract_change_rejects_running_worker_result(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    ari.dispatch(rid, "t")
    ari.amend(rid, {"objective": "new goal"}, actor=HUMAN, rationale="pivot")
    w = ari.worker_finished(rid, "t", status="COMPLETED", proposal={})
    assert w.state is WorkerState.REJECTED_STALE


def test_concurrency_limit_is_enforced(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(
        rid,
        [{"task_id": "a", "owned_paths": ["src/a/**"]}, {"task_id": "b", "owned_paths": ["src/b/**"]}],
        actor=SYS,
    )
    ari.dispatch(rid, "a")
    with pytest.raises(DaedalusError, match="concurrency limit"):
        ari.dispatch(rid, "b")
    ari.worker_finished(rid, "a", status="FAILED")  # releases a's workspace
