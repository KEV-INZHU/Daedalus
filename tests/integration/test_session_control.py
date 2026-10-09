"""Launched sessions never outlive what supervises them (run 10, live baseline).

Found live: a Builder kept editing the working tree for ~20s after `daedalus cancel`,
and after the orchestrator process was killed. These drive the command adapter
against a fake agent (the current interpreter) that heartbeats into a file, and
the lifecycle against a cancel arriving from another process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import HUMAN
from daedalus.adapters.base import AgentRequest, AgentResult
from daedalus.adapters.command import CommandAdapter
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.state_machine import Disposition, WorkerState
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import left_open_note, run_task

windows_only = pytest.mark.skipif(os.name != "nt", reason="process-tree containment uses a Windows job object")

# Reads its prompt, then heartbeats, from itself and (with "tree") from a child it starts. Both
# give up after LIFETIME_S, so a containment failure cannot leave them running after the suite.
LIFETIME_S = 120
AGENT = textwrap.dedent(
    """
    import subprocess, sys, time
    sys.stdin.read()
    beat, mode = sys.argv[1], sys.argv[2]
    loop = (
        "import sys,time\\nend = time.monotonic() + LIFETIME_S\\n"
        "while time.monotonic() < end:\\n    open(sys.argv[1], 'a').write('.')\\n    time.sleep(0.1)\\n"
    )
    if mode == "tree":
        subprocess.Popen([sys.executable, "-c", loop, beat + ".child"])
    end = time.monotonic() + LIFETIME_S
    while time.monotonic() < end:
        open(beat, "a").write(".")
        time.sleep(0.1)
    """
).replace("LIFETIME_S", str(LIFETIME_S))


def agent(tmp_path: Path, mode: str = "solo") -> tuple[CommandAdapter, Path]:
    script = tmp_path / "agent.py"
    script.write_text(AGENT, encoding="utf-8")
    beat = tmp_path / "beat"
    return CommandAdapter("fake", [sys.executable, str(script), str(beat), mode]), beat


def stopped(path: Path, settle: float = 1.0) -> bool:
    """True if nothing writes to `path` any more."""
    before = path.stat().st_size if path.exists() else 0
    time.sleep(settle)
    return (path.stat().st_size if path.exists() else 0) == before


def wait_for(path: Path, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert path.exists(), f"{path.name} never appeared"


def request(tmp_path: Path, **kw) -> AgentRequest:
    return AgentRequest("r", "t", "brunel", "prompt", tmp_path, **{"timeout_s": 60, **kw})


def test_should_stop_ends_a_running_session(tmp_path):
    adapter, beat = agent(tmp_path)
    t0 = time.monotonic()
    res = adapter.run_agent(request(tmp_path, should_stop=lambda: beat.exists()))
    assert res.status == "CANCELLED" and "cancelled" in (res.error or "")
    assert time.monotonic() - t0 < 30
    assert stopped(beat)


def test_a_failing_stop_check_does_not_end_the_session(tmp_path):
    def broken() -> bool:
        raise RuntimeError("store unreadable")

    adapter, beat = agent(tmp_path)
    res = adapter.run_agent(request(tmp_path, timeout_s=3, should_stop=broken))
    assert res.status == "TIMED_OUT"  # still bounded by its deadline
    assert stopped(beat)


def test_orchestrator_interrupt_kills_the_session(tmp_path):
    adapter, beat = agent(tmp_path)

    def interrupt() -> bool:
        if beat.exists():
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        adapter.run_agent(request(tmp_path, should_stop=interrupt))
    assert stopped(beat)


@windows_only
def test_timeout_kills_the_whole_session_tree(tmp_path):
    adapter, beat = agent(tmp_path, "tree")
    res = adapter.run_agent(request(tmp_path, timeout_s=4))
    assert res.status == "TIMED_OUT"
    child = Path(str(beat) + ".child")
    assert child.exists(), "the fake agent's child never started"
    assert stopped(beat) and stopped(child)


@windows_only
def test_session_tree_dies_with_its_orchestrator(tmp_path):
    adapter_argv = agent(tmp_path, "tree")[0].argv
    beat = tmp_path / "beat"
    orchestrator = tmp_path / "orchestrator.py"
    orchestrator.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            from daedalus.adapters.base import AgentRequest
            from daedalus.adapters.command import CommandAdapter
            CommandAdapter("fake", {adapter_argv!r}).run_agent(
                AgentRequest("r", "t", "brunel", "prompt", Path({str(tmp_path)!r}), 600)
            )
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen([sys.executable, str(orchestrator)], env=env)
    try:
        child = Path(str(beat) + ".child")
        wait_for(child)
        proc.kill()  # TerminateProcess: no cleanup code runs in the orchestrator
        proc.wait(timeout=10)
        assert stopped(beat, settle=2.0) and stopped(child, settle=2.0)
    finally:
        proc.kill()


def test_stop_check_sees_a_cancel_recorded_by_another_process(ari, repo):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    check = ari.stop_check(rid)
    assert check() is False
    other = Ariadne(repo)  # `daedalus cancel` from a second terminal
    try:
        other.cancel(rid, actor=HUMAN, reason="stop")
    finally:
        other.close()
    assert check() is True


def test_builder_that_never_launches_leaves_the_run_blocked(ari, repo):
    # Run 10 live: an unrecognized model fails every launch. Never accepted, and the
    # note no longer sends the user looking for [human] items that do not exist.
    failed = AgentResult("FAILED", error="[claude-code:unrecognized_model]")
    adapter = SimulatedAdapter({"brunel": [lambda req: failed] * 2})
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter, max_rounds=2)
    assert report.decision.disposition is Disposition.BLOCKED
    assert not report.decision.acceptable
    state = ari.state(report.run_id)
    assert [w.state for w in state.workers.values()] == [WorkerState.FAILED, WorkerState.FAILED]
    assert not state.evidence  # nothing was verified on behalf of a Builder that never ran
    assert any("[agent] items remain" in n for n in report.notes)
    assert not any("[human]" in n for n in report.notes)


@pytest.mark.parametrize(
    ("who", "says"),
    [({"human", "agent"}, "[human]"), ({"agent", "system"}, "[agent]"), ({"system"}, "reconcile")],
)
def test_left_open_note_names_who_can_act(who, says):
    decision = SimpleNamespace(reasons=[SimpleNamespace(fixable_by=w) for w in sorted(who)])
    assert says in left_open_note(decision, 2)


def test_cancel_during_a_build_stops_the_builder_and_the_rounds(ari, repo):
    seen = []

    def builder(req: AgentRequest) -> AgentResult:
        other = Ariadne(repo)
        try:
            other.cancel(req.run_id, actor=HUMAN, reason="stop")
        finally:
            other.close()
        seen.append(req.should_stop is not None and req.should_stop())
        return AgentResult("CANCELLED", error="stopped")

    adapter = SimulatedAdapter({"brunel": [builder, builder]})
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter, max_rounds=3)
    assert seen == [True]  # the session was told to stop, and no second round started
    assert report.rounds == 1
    assert report.decision.disposition is Disposition.CANCELLED
    assert ari.state(report.run_id).workers["build-1"].state is WorkerState.CANCELLED
