"""Bounded fan-out (run 8ea56a7320bc, spec §14, §18)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from conftest import HUMAN, make_repo
from daedalus.adapters.base import Adapter, AgentRequest, AgentResult
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.state_machine import Disposition, WorkerState
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import run_task

TWO_WORKERS = {
    "policy": {"default_budget": {"max_attempts": 40, "max_wall_seconds": 86400, "max_cost": 50.0, "max_workers": 2}}
}
SPEC = {"objective": "build a, b, then fix the app", "budget": {"max_workers": 2}}


class ScriptedBrunel(Adapter):
    """Plans, then builds each package by task id; records concurrency and cwds."""

    name = "scripted"
    capabilities = SimulatedAdapter().capabilities

    def __init__(self, plan, work, rendezvous=(), hold=None):
        # Packages in `rendezvous` wait (up to 5s) until two sessions are live at once:
        # deterministic overlap when the scheduler runs them concurrently, a timeout when it doesn't.
        # Packages in `hold` stay in flight until their event is set (up to 10s).
        self.plan, self.work, self.rendezvous = plan, work, set(rendezvous)
        self.hold: dict[str, threading.Event] = hold or {}
        self.lock = threading.Condition()
        self.active = self.peak = 0
        self.cwds: dict[str, Path] = {}
        self.seen: dict[str, set[str]] = {}
        self.calls: list[str] = []

    def run_agent(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req.task_id)
        if req.task_id.startswith("plan-"):
            return AgentResult("COMPLETED", structured=self.plan, session_id=req.task_id)
        key = req.task_id.split("-", 1)[1] if req.task_id.startswith("r1-") else req.task_id
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.lock.notify_all()
            if key in self.rendezvous:
                self.lock.wait_for(lambda: self.active >= 2, timeout=5)
        if key in self.hold:
            self.hold[key].wait(timeout=10)
        try:
            time.sleep(0.05)
            self.cwds[req.task_id] = Path(req.cwd)
            self.seen[req.task_id] = {p.relative_to(req.cwd).as_posix() for p in Path(req.cwd).rglob("*.py")}
            action = self.work.get(key, {})
            if action == "fail":
                return AgentResult("FAILED", error="worker crashed", session_id=req.task_id)
            for rel, content in action.items():
                target = Path(req.cwd) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode())
            return AgentResult("COMPLETED", structured={"status": "done", "summary": key}, session_id=req.task_id)
        finally:
            with self.lock:
                self.active -= 1


PLAN = {
    "packages": [
        {"id": "a", "description": "module a", "owned_paths": ["src/a/**"]},
        {"id": "b", "description": "module b", "owned_paths": ["src/b/**"]},
        {"id": "c", "description": "use a in the app", "owned_paths": ["app.txt"], "depends_on": ["a"]},
    ]
}
WORK = {"a": {"src/a/x.py": "x = 1\n"}, "b": {"src/b/y.py": "y = 1\n"}, "c": {"app.txt": "ok\n"}}


@pytest.fixture
def fan_repo(tmp_path):
    return make_repo(tmp_path / "r", TWO_WORKERS)


@pytest.fixture
def fari(fan_repo, clock):
    a = Ariadne(fan_repo, clock=clock)
    yield a
    a.close()


def test_independent_packages_run_concurrently_in_their_own_worktrees(fari, fan_repo, monkeypatch):
    writers = set()
    real = fari.store.append

    def spy(*args, **kw):
        writers.add(threading.current_thread() is threading.main_thread())
        return real(*args, **kw)

    monkeypatch.setattr(fari.store, "append", spy)
    agent = ScriptedBrunel(PLAN, WORK, rendezvous={"a", "b"})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    assert report.decision.disposition is Disposition.ACCEPTED, report.decision.to_dict()
    assert report.rounds == 1
    assert agent.peak == 2  # a and b together; never more than the cap
    cwds = [agent.cwds[t] for t in ("r1-a", "r1-b", "r1-c")]
    assert len(set(cwds)) == 3 and all(fan_repo.resolve() not in c.resolve().parents for c in cwds)
    assert "src/a/x.py" in agent.seen["r1-c"]  # c started after a integrated, from the updated candidate
    assert (fan_repo / "src" / "a" / "x.py").exists() and (fan_repo / "src" / "b" / "y.py").exists()
    assert writers == {True}  # every audit-log write happened on the orchestrator thread
    assert all(not c.exists() for c in cwds)


def test_never_more_than_the_cap(fari):
    plan = {"packages": [{"id": n, "owned_paths": [f"src/{n}/**"]} for n in "pqrs"]
            + [{"id": "app", "owned_paths": ["app.txt"]}]}
    work = {n: {f"src/{n}/m.py": "m = 1\n"} for n in "pqrs"} | {"app": {"app.txt": "ok\n"}}
    agent = ScriptedBrunel(plan, work, rendezvous=set("pqrs"))
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    assert report.decision.disposition is Disposition.ACCEPTED
    assert agent.peak == 2


def test_failed_package_blocks_its_dependents_and_feeds_back(fari, fan_repo):
    seen_feedback = []

    class Fixer(ScriptedBrunel):
        def run_agent(self, req):
            if req.task_id == "build-2":
                seen_feedback.append(req.prompt)
                (Path(req.cwd) / "app.txt").write_bytes(b"ok\n")
                return AgentResult("COMPLETED", structured={"status": "done"}, session_id="fix")
            return super().run_agent(req)

    agent = Fixer(PLAN, {**WORK, "a": "fail"})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    state = fari.state(report.run_id)
    assert state.workers["r1-a"].state is WorkerState.FAILED
    assert state.workers["r1-c"].state is WorkerState.CANCELLED and "r1-c" not in agent.calls
    assert state.workers["r1-b"].integrated
    assert report.rounds == 2 and report.decision.disposition is Disposition.ACCEPTED
    assert "r1-a" in seen_feedback[0] and "r1-c" in seen_feedback[0]


@pytest.mark.parametrize(
    "plan",
    [
        None,  # no structured plan at all
        {"packages": []},
        {"packages": [{"id": "a", "owned_paths": ["src/**"]}, {"id": "b", "owned_paths": ["src/lib.py"]}]},
        {"packages": [{"id": "only", "owned_paths": ["**"]}]},  # one package is the baseline
    ],
)
def test_unusable_plans_fall_back_to_one_in_place_builder(fari, fan_repo, plan):
    agent = ScriptedBrunel(plan, {"build-1": {"app.txt": "ok\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    assert report.decision.disposition is Disposition.ACCEPTED
    assert agent.cwds["build-1"] == fan_repo.resolve() or agent.cwds["build-1"] == fan_repo
    assert not any(t.startswith("r1-") for t in fari.state(report.run_id).workers)


def test_default_cap_keeps_the_single_builder_path(ari, repo):
    agent = ScriptedBrunel(PLAN, {"build-1": {"app.txt": "ok\n"}})
    report = run_task(ari, {"objective": "o"}, actor=HUMAN, adapter=agent)
    assert report.decision.disposition is Disposition.ACCEPTED
    assert agent.calls == ["build-1"]  # no planning request with the default cap of one


def test_default_policy_caps_workers_at_one():
    import yaml

    from daedalus.core.policy import default_policy_text

    assert yaml.safe_load(default_policy_text())["default_budget"]["max_workers"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "x/../../other", "owned_paths": ["src/x/**"]},
        {"id": "..", "owned_paths": ["src/x/**"]},
        {"id": "a b", "owned_paths": ["src/x/**"]},
        {"id": {"nested": 1}, "owned_paths": ["src/x/**"]},
        {"id": "x", "owned_paths": "src/**"},
        {"id": "x", "owned_paths": []},
        {"id": "x", "owned_paths": ["src/x/**"], "depends_on": "a"},
        {"id": "x", "owned_paths": ["src/x/**"], "depends_on": ["../a"]},
    ],
)
def test_malformed_or_unsafe_package_ids_reject_the_plan(bad):
    from daedalus.orchestration.lifecycle import _plan_packages

    assert _plan_packages({"packages": [{"id": "a", "owned_paths": ["src/a/**"]}, bad]}, 1) is None


def test_workspace_path_refuses_task_ids_that_escape(repo):
    from daedalus.core.errors import IntegrationError
    from daedalus.repository import worktree

    with pytest.raises(IntegrationError):
        worktree.workspace_path(repo, "run", "x/../../elsewhere")


def test_dispatch_failure_fails_one_package_and_leaves_nothing_active(fari, fan_repo, monkeypatch):
    from daedalus.core.errors import IntegrationError
    from daedalus.repository import worktree

    real = worktree.create

    def flaky(root, run_id, task_id, *a, **k):
        if task_id == "r1-b":
            raise IntegrationError("seed failed")
        return real(root, run_id, task_id, *a, **k)

    monkeypatch.setattr(worktree, "create", flaky)
    agent = ScriptedBrunel(PLAN, {**WORK, "build-2": {"src/b/y.py": "y = 1\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    state = fari.state(report.run_id)
    assert state.workers["r1-b"].state is WorkerState.CANCELLED
    assert state.workers["r1-a"].integrated and state.workers["r1-c"].integrated
    assert not state.active_workers and not any(w.state is WorkerState.PENDING for w in state.workers.values())
    assert report.decision.disposition is Disposition.ACCEPTED


def test_unexpected_error_drains_running_sessions(fari, fan_repo, monkeypatch):
    rid = fari.start(SPEC, actor=HUMAN)
    release_b = threading.Event()
    # b stays in flight until a's integration blows up, so only the abort drain can record it.
    agent = ScriptedBrunel(PLAN, WORK, hold={"b": release_b})
    real_integrate = fari.integrate

    def boom(run_id, task_id):
        if task_id == "r1-a":
            release_b.set()
            raise RuntimeError("disk on fire")
        return real_integrate(run_id, task_id)

    monkeypatch.setattr(fari, "integrate", boom)
    from daedalus.orchestration.lifecycle import fan_out_round

    with pytest.raises(RuntimeError, match="disk on fire"):
        fan_out_round(fari, rid, agent, 1)
    state = fari.state(rid)
    assert not state.active_workers
    assert not any(w.state is WorkerState.PENDING for w in state.workers.values())
    assert all(w.workspace is None for w in state.workers.values())
    assert not state.workers["r1-b"].integrated  # an aborted round never advances the authoritative tree
    assert not (fan_repo / "src" / "b" / "y.py").exists()


def test_transitively_blocked_dependents_are_all_cancelled(fari, fan_repo):
    plan = {
        "packages": [
            {"id": "a", "owned_paths": ["src/a/**"]},
            {"id": "b", "owned_paths": ["src/b/**"]},
            {"id": "x", "owned_paths": ["src/x/**"], "depends_on": ["y"]},
            {"id": "y", "owned_paths": ["src/y/**"], "depends_on": ["a"]},
        ]
    }
    agent = ScriptedBrunel(plan, {"a": "fail", "b": {"src/b/m.py": "m = 1\n"}, "build-2": {"app.txt": "ok\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    state = fari.state(report.run_id)
    assert state.workers["r1-x"].state is WorkerState.CANCELLED and state.workers["r1-y"].state is WorkerState.CANCELLED
    assert report.decision.disposition is Disposition.ACCEPTED  # round 2's in-place Builder was not blocked


def test_blocked_worker_report_is_a_failure(fari, fan_repo):
    class Blocking(ScriptedBrunel):
        def run_agent(self, req):
            if req.task_id == "r1-b":
                return AgentResult("COMPLETED", structured={"status": "blocked", "blocker": "need API key"})
            return super().run_agent(req)

    agent = Blocking(PLAN, {**WORK, "build-2": {"src/b/y.py": "y = 1\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    b = fari.state(report.run_id).workers["r1-b"]
    assert b.state is WorkerState.FAILED and "need API key" in b.note and not b.integrated


def test_planner_that_edits_files_is_ignored(fari, fan_repo):
    class EditingPlanner(ScriptedBrunel):
        def run_agent(self, req):
            if req.task_id.startswith("plan-"):
                (Path(req.cwd) / "app.txt").write_bytes(b"planner meddled\n")
            return super().run_agent(req)

    agent = EditingPlanner(PLAN, {"build-1": {"app.txt": "ok\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    assert not any(t.startswith("r1-") for t in fari.state(report.run_id).workers)
    assert report.decision.disposition is Disposition.ACCEPTED


def test_crashed_planner_falls_back_to_one_builder(fari, fan_repo):
    class CrashingPlanner(ScriptedBrunel):
        def run_agent(self, req):
            if req.task_id.startswith("plan-"):
                raise RuntimeError("planner exploded")
            return super().run_agent(req)

    agent = CrashingPlanner(PLAN, {"build-1": {"app.txt": "ok\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    assert report.decision.disposition is Disposition.ACCEPTED


def test_parallel_package_without_workspace_never_runs(fari, fan_repo, monkeypatch):
    import dataclasses

    real = fari.dispatch

    def no_workspace(run_id, task_id, **kw):
        w = real(run_id, task_id, **kw)
        return dataclasses.replace(w, workspace=None) if task_id == "r1-b" else w

    monkeypatch.setattr(fari, "dispatch", no_workspace)
    agent = ScriptedBrunel(PLAN, {**WORK, "build-2": {"src/b/y.py": "y = 1\n"}})
    report = run_task(fari, SPEC, actor=HUMAN, adapter=agent)
    assert "r1-b" not in agent.calls
    assert fari.state(report.run_id).workers["r1-b"].state is WorkerState.FAILED
