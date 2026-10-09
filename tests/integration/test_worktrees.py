"""Worktree isolation (run f318fc00189d, spec §12, §14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import HUMAN
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core import run as ev
from daedalus.core.state_machine import WorkerState
from daedalus.orchestration.ariadne import Ariadne
from daedalus.repository import worktree
from daedalus.repository.candidate import file_sha256

SYS = "system:test"


def dispatch(ari, rid, owned=("src/**",), task="t"):
    ari.plan(rid, [{"task_id": task, "owned_paths": list(owned)}], actor=SYS)
    ari.dispatch(rid, task)
    w = ari.state(rid).workers[task]
    return Path(w.workspace), w


def test_workspace_is_outside_repo_and_seeded_byte_identical(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    # The current candidate has uncommitted changes, including CRLF bytes and an untracked file.
    (repo / "src" / "lib.py").write_bytes(b"x = 1\r\ny = 2\n")
    (repo / "src" / "draft.py").write_bytes(b"draft = True\n")
    (repo / "app.txt").unlink()
    ws, w = dispatch(ari, rid)
    assert repo.resolve() not in ws.resolve().parents and ws.is_dir()
    assert (ws / "src" / "lib.py").read_bytes() == b"x = 1\r\ny = 2\n"
    assert (ws / "src" / "draft.py").read_bytes() == b"draft = True\n"
    assert not (ws / "app.txt").exists()
    assert w.workspace_seed["src/lib.py"] == file_sha256(repo / "src" / "lib.py")
    assert w.workspace_seed["app.txt"] is None
    dispatched = [e for e in ari.store.events(rid) if e.type == ev.WORKER_TRANSITION and e.payload["to"] == "DISPATCHED"]
    assert dispatched[-1].payload["workspace"] == str(ws) and dispatched[-1].payload["workspace_seed"]
    ari.worker_finished(rid, "t", status="FAILED")
    assert not ws.exists() and not ws.parent.exists()  # empty run directory pruned too


def test_proposal_is_derived_from_the_workspace_not_the_report(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    (ws / "src" / "lib.py").write_bytes(b"x = 2\n")
    (ws / "src" / "added.py").write_bytes(b"new = 1\n")
    (ws / "src" / "sub").mkdir()
    (ws / "src" / "sub" / "deep.py").write_bytes(b"deep = 1\n")
    (ws / "app.txt").unlink()  # outside ownership, but must still be reported
    reported = {"summary": "done", "files": {"src/lib.py": "forged\n"}}
    w = ari.worker_finished(rid, "t", status="COMPLETED", proposal=reported)
    files = w.proposal["files"]
    assert files == {"src/lib.py": "x = 2\n", "src/added.py": "new = 1\n", "src/sub/deep.py": "deep = 1\n", "app.txt": None}
    assert w.proposal["summary"] == "done"


def test_workspace_edits_never_touch_the_repo_until_integration(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    before = ari.capture(ari.state(rid)).candidate_id
    original = (repo / "src" / "lib.py").read_bytes()
    ws, _ = dispatch(ari, rid)
    (ws / "src" / "lib.py").write_bytes(b"x = 2\r\nz = 3\n")  # mixed endings on purpose
    assert ari.capture(ari.state(rid)).candidate_id == before
    ari.worker_finished(rid, "t", status="COMPLETED")
    assert (repo / "src" / "lib.py").read_bytes() == original
    ari.integrate(rid, "t")
    assert (repo / "src" / "lib.py").read_bytes() == b"x = 2\r\nz = 3\n"  # bytes preserved, no newline translation


@pytest.mark.parametrize("ending", ["COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"])
def test_workspace_removed_when_the_session_ends(ari, repo, ending):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    ari.worker_finished(rid, "t", status=ending)
    assert not ws.exists()
    assert ari.state(rid).workers["t"].workspace is None


def test_run_cancellation_with_live_isolated_worker(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    (ws / "src" / "lib.py").write_bytes(b"late edit\n")
    ari.cancel(rid, actor=HUMAN, reason="stop")
    assert ws.exists()  # the worker is still running; cancellation waits for it
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert w.state is WorkerState.CANCELLED and not ws.exists()
    assert ari.state(rid).is_finished
    assert b"late edit" not in (repo / "src" / "lib.py").read_bytes()


def test_contract_change_rejects_isolated_worker_and_removes_workspace(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    ari.amend(rid, {"objective": "different"}, actor=HUMAN, rationale="pivot")
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert w.state is WorkerState.REJECTED_STALE and not ws.exists()


def test_failed_seeding_leaves_no_workspace(ari, repo, monkeypatch):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (repo / "src" / "lib.py").write_text("changed\n", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(worktree.shutil, "copyfile", boom)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    with pytest.raises(OSError):
        ari.dispatch(rid, "t")
    assert not worktree.workspace_path(repo, rid, "t").exists()
    assert ari.state(rid).workers["t"].state is WorkerState.PENDING


def test_failed_dispatch_logging_removes_the_workspace(ari, repo, monkeypatch):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    real = ari._append

    def flaky(run_id, type_, actor, payload):
        if type_ == ev.WORKER_TRANSITION and payload.get("to") == "DISPATCHED":
            raise RuntimeError("store unavailable")
        return real(run_id, type_, actor, payload)

    monkeypatch.setattr(ari, "_append", flaky)
    with pytest.raises(RuntimeError):
        ari.dispatch(rid, "t")
    assert not worktree.workspace_path(repo, rid, "t").exists()


def test_recovery_sweeps_unrecorded_workspaces(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    orphan = worktree.create(repo, rid, "ghost", ari.state(rid).contract.base_revision, (), ("src/**",))
    assert orphan.path.exists()
    notes = ari.recover(rid)
    assert not orphan.path.exists() and any("unrecorded workspace" in n for n in notes)


def test_failed_removal_is_logged_and_retried(ari, repo, monkeypatch):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    monkeypatch.setattr(worktree, "remove", lambda root, path: False)
    ari.worker_finished(rid, "t", status="FAILED")
    removal = [e for e in ari.store.events(rid) if e.type == ev.WORKSPACE_REMOVED][-1].payload
    assert removal["removed"] is False and removal["error"]
    assert ari.state(rid).workers["t"].workspace == str(ws)
    monkeypatch.undo()
    assert ari.needs_recovery(rid)
    ari.recover(rid)
    assert ari.state(rid).workers["t"].workspace is None and not ws.exists()


def test_cleanup_errors_never_abort_the_transition(ari, repo, monkeypatch):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)

    def explode(root, path):
        raise RuntimeError("TMP changed")

    monkeypatch.setattr(worktree, "remove", explode)
    w = ari.worker_finished(rid, "t", status="TIMED_OUT")
    assert w.state is WorkerState.TIMED_OUT
    monkeypatch.undo()
    ari.recover(rid)
    assert not ws.exists()


def test_symlinks_in_the_workspace_fail_the_worker(ari, repo, tmp_path):
    import os

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    secret = tmp_path / "secret.txt"
    secret.write_text("outside content", encoding="utf-8")
    try:
        os.symlink(secret, ws / "src" / "link.py")
    except (OSError, NotImplementedError):
        ari.worker_finished(rid, "t", status="FAILED")
        pytest.skip("symlinks need extra privileges on this platform")
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert w.state is WorkerState.FAILED and "symlink" in w.note


def test_excluded_paths_are_dropped_from_proposals(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    (ws / "src" / "__pycache__").mkdir()
    (ws / "src" / "__pycache__" / "lib.cpython.pyc").write_bytes(b"\x00binary")
    (ws / "src" / "lib.py").write_bytes(b"x = 2\n")
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert w.state is WorkerState.COMPLETED and set(w.proposal["files"]) == {"src/lib.py"}


def test_workspace_root_is_per_user(monkeypatch):
    monkeypatch.setattr(worktree.getpass, "getuser", lambda: "alice/../bob")
    root = worktree.worktree_root()
    assert root.name == f"{worktree.ROOT_PREFIX}-alice_.._bob"  # sanitized: no path separators
    root.rmdir()


def test_untraceable_worker_workspace_removed_on_recovery(repo, clock):
    a = Ariadne(repo, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(a, rid)
    a.close()
    b = Ariadne(repo, clock=clock, adapter=SimulatedAdapter())
    b.recover(rid)
    assert b.state(rid).workers["t"].state is WorkerState.FAILED
    assert not ws.exists()
    b.close()


def test_in_place_packages_get_no_workspace(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "b", "owned_paths": ["**"], "in_place": True}], actor=SYS)
    ari.dispatch(rid, "b")
    assert ari.state(rid).workers["b"].workspace is None


def test_remove_refuses_paths_outside_the_workspace_root(repo):
    from daedalus.core.errors import IntegrationError

    with pytest.raises(IntegrationError):
        worktree.remove(repo, repo)


def test_vanished_workspace_fails_the_worker(ari, repo):
    import shutil

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    shutil.rmtree(ws)
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert w.state is WorkerState.FAILED and "no usable proposal" in w.note


def test_changes_the_worker_commits_are_still_proposed(ari, repo):
    import subprocess

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid)
    (ws / "src" / "committed.py").write_bytes(b"c = 1\n")
    (ws / "app.txt").unlink()  # unowned deletion, committed
    git = ["git", "-C", str(ws), "-c", "user.email=w@x", "-c", "user.name=w"]
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "worker commit"], check=True, capture_output=True)
    (ws / "src" / "after.py").write_bytes(b"a = 1\n")  # plus an uncommitted change on top
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert set(w.proposal["files"]) == {"src/committed.py", "app.txt", "src/after.py"}
    assert w.proposal["files"]["app.txt"] is None


def test_unremovable_workspace_is_retried_a_bounded_number_of_times(ari, repo, monkeypatch):
    from daedalus.orchestration.ariadne import CLEANUP_ATTEMPTS

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    dispatch(ari, rid)
    monkeypatch.setattr(worktree, "remove", lambda root, path: False)
    ari.worker_finished(rid, "t", status="FAILED")  # attempt 1
    for _ in range(CLEANUP_ATTEMPTS - 1):
        assert ari.needs_recovery(rid)
        ari.recover(rid)
    assert ari.state(rid).workers["t"].workspace_cleanup_failures == CLEANUP_ATTEMPTS
    assert not ari.needs_recovery(rid)  # no more events on every hook call
    before = len(ari.store.events(rid))
    ari.recover(rid)
    assert len([e for e in ari.store.events(rid)[before:] if e.type == ev.WORKSPACE_REMOVED]) == 0
    monkeypatch.undo()
    worktree.remove(repo, Path(ari.state(rid).workers["t"].workspace))


def test_workspace_creation_runs_no_repository_hooks(ari, repo):
    hook = repo / ".git" / "hooks" / "post-checkout"
    marker = repo.parent / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    import subprocess

    control = repo.parent / "control-wt"  # control: a plain `worktree add` does run the hook
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(control), "HEAD"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(control)], capture_output=True)
    if not marker.exists():
        pytest.skip("this platform's git does not run shell hooks")
    marker.unlink()
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    dispatch(ari, rid)
    assert not marker.exists()
    ari.worker_finished(rid, "t", status="FAILED")


def test_hooks_path_lives_in_the_private_workspace_root(monkeypatch):
    import subprocess

    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(worktree.subprocess, "run", spy)
    worktree._git(Path("."), "--version", check=False)
    hooks = next(c for c in seen[-1] if c.startswith("core.hooksPath="))
    assert Path(hooks.split("=", 1)[1]).parent == worktree.worktree_root()
    assert "core.fsmonitor=false" in seen[-1]


def test_hidden_index_entries_fail_the_worker(ari, repo):
    import subprocess

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ws, _ = dispatch(ari, rid, owned=("src/a/**",))
    subprocess.run(["git", "-C", str(ws), "update-index", "--skip-worktree", "app.txt"], check=True)
    (ws / "app.txt").write_bytes(b"sneaky unowned edit\n")
    w = ari.worker_finished(rid, "t", status="COMPLETED")
    assert w.state is WorkerState.FAILED and "hidden" in w.note


def test_rollback_failure_keeps_the_original_error(ari, repo, monkeypatch):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.plan(rid, [{"task_id": "t", "owned_paths": ["src/**"]}], actor=SYS)
    real = ari._append

    def flaky(run_id, type_, actor, payload):
        if type_ == ev.WORKER_TRANSITION and payload.get("to") == "DISPATCHED":
            raise RuntimeError("store unavailable")
        return real(run_id, type_, actor, payload)

    monkeypatch.setattr(ari, "_append", flaky)
    real_remove = worktree.remove

    def bad_remove(root, path):
        raise ValueError("cleanup broke")

    monkeypatch.setattr(worktree, "remove", bad_remove)
    with pytest.raises(RuntimeError, match="store unavailable"):
        ari.dispatch(rid, "t")
    real_remove(repo, worktree.workspace_path(repo, rid, "t"))
