"""Coordination wired into `daedalus run`'s launches (coordination/launch.py, on by default)."""

from __future__ import annotations

import pytest
from test_fanout import PLAN, SPEC, TWO_WORKERS, WORK, ScriptedBrunel

from conftest import HUMAN, make_repo
from daedalus.adapters import make_adapter
from daedalus.adapters.base import AgentRequest, AgentResult
from daedalus.adapters.command import CommandAdapter
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.coordination import Channel, FileBus
from daedalus.coordination.bus import BusError
from daedalus.coordination.launch import BUS_COMMAND, ENV_REPO
from daedalus.coordination.surface import ENV_AGENT, ENV_BUS_DIR, ENV_RUN
from daedalus.core.config import load_config
from daedalus.core.errors import PolicyError
from daedalus.core.state_machine import Disposition
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import run_task

REVIEW = {"mode": "review"}
FINDING = {
    "severity": "MINOR",
    "title": "name is vague",
    "evidence": "app.txt:1",
    "required_change": "rename",
}


def scripted(seen: dict[str, list[AgentRequest]]):
    def build(req: AgentRequest) -> AgentResult:
        seen.setdefault("brunel", []).append(req)
        (req.cwd / "app.txt").write_text("ok\n", encoding="utf-8")
        if req.env.get(ENV_AGENT):  # what the session's `daedalus bus post` would do
            ari = Ariadne(req.env[ENV_REPO])
            try:
                Channel(ari, req.env[ENV_RUN], FileBus(req.env[ENV_BUS_DIR])).post(
                    req.env[ENV_AGENT], "decision", "app.txt holds the status word"
                )
            finally:
                ari.close()
        return AgentResult("COMPLETED", structured={"status": "done", "summary": "fixed"})

    def review(req: AgentRequest) -> AgentResult:
        seen.setdefault("aristotle", []).append(req)
        return AgentResult("COMPLETED", structured={"summary": "fine", "findings": [FINDING]})

    return SimulatedAdapter({"brunel": [build, build], "aristotle": [review, review]})


def run(root, clock, **kw):
    seen: dict[str, list[AgentRequest]] = {}
    ari = Ariadne(root, clock=clock)
    notes: list[str] = []
    report = run_task(
        ari,
        {"objective": "fix the app"},
        actor=HUMAN,
        adapter=scripted(seen),
        max_rounds=1,
        notify=notes.append,
        **kw,
    )
    return ari, report, seen, notes


def test_launched_agents_coordinate_by_default(tmp_path, clock):
    root = make_repo(tmp_path / "r", REVIEW)
    ari, report, seen, _ = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED, report.decision.to_dict()
        state = ari.state(report.run_id)
        [build] = seen["brunel"]
        [review] = seen["aristotle"]

        # The builder was registered before it worked, and its session got the run's context.
        name = build.env[ENV_AGENT]
        assert state.agents[name]["principal"] == "agent:brunel" and state.agents[name]["role"] == "brunel"
        assert name in state.candidate_authors
        assert build.env[ENV_RUN] == report.run_id and build.env[ENV_REPO] == str(ari.root)
        assert build.bus_commands == (BUS_COMMAND,) and BUS_COMMAND not in build.run_commands
        assert "## Coordination" in build.prompt and "not instructions from Daedalus" in build.prompt

        # The reviewer is registered and attributed, but gets no peer messages and no bus access.
        [r] = state.reviews
        assert state.agents[r.reviewer]["principal"] == "agent:reviewer:aristotle"
        assert "## Coordination" not in review.prompt and "status word" not in review.prompt
        assert review.env == {} and review.bus_commands == ()

        # Both posted on the run's channel; both are retired, so nothing is left subscribed.
        bus = FileBus(ari.root / ".daedalus" / "bus")
        posts = [(m["sender"], m["type"]) for m in bus.history("daedalus")[0]]
        assert posts == [(name, "decision"), (r.reviewer, "finding")]
        assert all(a["retired_at"] is not None for a in state.agents.values())
        assert bus.subscribers() == {}
    finally:
        ari.close()


def test_coordination_can_be_turned_off(tmp_path, clock):
    root = make_repo(tmp_path / "r", {**REVIEW, "coordination": False})
    ari, report, seen, _ = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED
        state = ari.state(report.run_id)
        assert state.agents == {}
        assert seen["brunel"][0].env == {} and "## Coordination" not in seen["brunel"][0].prompt
        assert state.reviews[0].reviewer.startswith("agent:reviewer:aristotle:")  # the old name
        assert not (ari.root / ".daedalus" / "bus").exists()
    finally:
        ari.close()


def test_a_configured_bus_dir_outside_the_repository_is_used(tmp_path, clock):
    root = make_repo(tmp_path / "r", {**REVIEW, "coordination": {"bus_dir": "../shared-bus"}})
    ari, report, seen, _ = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED, report.decision.to_dict()
        assert seen["brunel"][0].env[ENV_BUS_DIR] == str((tmp_path / "shared-bus").resolve())
        assert FileBus(tmp_path / "shared-bus").history("daedalus")[0]
    finally:
        ari.close()


def test_a_bus_inside_the_repository_is_refused_unless_under_the_state_dir(tmp_path):
    for bad in ("shared-bus", ".", "src/bus"):
        with pytest.raises(PolicyError):
            load_config(
                make_repo(
                    tmp_path / bad.replace("/", "_").replace(".", "dot"), {"coordination": {"bus_dir": bad}}
                )
            )
    assert load_config(make_repo(tmp_path / "ok", {"coordination": {"bus_dir": ".daedalus/other"}})).bus_dir


def test_a_relative_env_bus_dir_is_made_absolute(tmp_path, clock, monkeypatch):
    monkeypatch.setenv(ENV_BUS_DIR, "../env-bus")
    root = make_repo(tmp_path / "r", REVIEW)
    ari, report, seen, _ = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED
        assert seen["brunel"][0].env[ENV_BUS_DIR] == str((tmp_path / "env-bus").resolve())
    finally:
        ari.close()


def test_an_env_bus_inside_the_repository_turns_coordination_off_not_the_run(tmp_path, clock, monkeypatch):
    monkeypatch.setenv(ENV_BUS_DIR, "in-tree-bus")
    root = make_repo(tmp_path / "r", REVIEW)
    ari, report, seen, notes = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED
        assert seen["brunel"][0].env == {} and not (root / "in-tree-bus").exists()
        assert any("coordination is off for this run" in n for n in notes)
    finally:
        ari.close()


@pytest.mark.parametrize("bad", ["yes", {"enabled": "on"}, {"bus_dir": ""}, {"other": 1}])
def test_bad_coordination_settings_are_refused(tmp_path, bad):
    root = make_repo(tmp_path / "r", {"coordination": bad})
    with pytest.raises(PolicyError):
        load_config(root)


def test_a_failed_registration_turns_off_only_that_launch(tmp_path, clock, monkeypatch):
    real = Channel.spawn
    calls = []

    def flaky(self, role, task_id, principal):
        calls.append(role)
        if len(calls) == 1:
            raise BusError("disk full")
        return real(self, role, task_id, principal)

    monkeypatch.setattr(Channel, "spawn", flaky)
    root = make_repo(tmp_path / "r", REVIEW)
    ari, report, seen, notes = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED
        assert seen["brunel"][0].env == {}  # the builder went ahead without coordination
        assert any("coordination is off for this launch" in n and "disk full" in n for n in notes)
        state = ari.state(report.run_id)
        assert state.agents[state.reviews[0].reviewer]["role"] == "aristotle"  # the review still was
    finally:
        ari.close()


def test_a_failed_attach_still_retires_the_name(tmp_path, clock, monkeypatch):
    from daedalus.coordination import launch

    def broken(*a, **kw):
        raise BusError("cannot read history")

    monkeypatch.setattr(launch, "attach", broken)
    root = make_repo(tmp_path / "r", REVIEW)
    ari, report, seen, _ = run(root, clock)
    try:
        assert report.decision.disposition is Disposition.ACCEPTED
        state = ari.state(report.run_id)
        assert seen["brunel"][0].env == {}
        assert len(state.agents) == 2 and all(a["retired_at"] is not None for a in state.agents.values())
    finally:
        ari.close()


def test_settings_are_fixed_for_the_whole_drive(tmp_path, clock):
    import yaml

    root = make_repo(tmp_path / "r", REVIEW)
    seen: dict[str, list[AgentRequest]] = {}
    adapter = scripted(seen)
    build = adapter.script["brunel"][0]

    def turn_off_midway(req):
        cfg = yaml.safe_load((root / ".daedalus.yml").read_text())
        (root / ".daedalus.yml").write_text(yaml.safe_dump({**cfg, "coordination": False}))
        return build(req)

    adapter.script["brunel"] = [turn_off_midway]
    ari = Ariadne(root, clock=clock)
    try:
        report = run_task(ari, {"objective": "fix"}, actor=HUMAN, adapter=adapter, max_rounds=1)
        state = ari.state(report.run_id)
        assert state.reviews and state.reviews[0].reviewer in state.agents  # still coordinated
    finally:
        ari.close()


def test_the_report_attributes_the_model_of_a_registered_reviewer(tmp_path, clock):
    from daedalus.report.model import build_report

    root = make_repo(tmp_path / "r", REVIEW)
    seen: dict[str, list[AgentRequest]] = {}
    adapter = scripted(seen)
    review = adapter.script["aristotle"][0]

    def with_usage(req):
        res = review(req)
        return AgentResult(**{**res.__dict__, "usage": {"model": "model-x"}, "session_id": "abcdef1234"})

    adapter.script["aristotle"] = [with_usage]
    ari = Ariadne(root, clock=clock)
    try:
        report = run_task(ari, {"objective": "fix"}, actor=HUMAN, adapter=adapter, max_rounds=1)
        [row] = build_report(ari, report.run_id).reviews
        assert row.reviewer in ari.state(report.run_id).agents and row.model == "model-x"
    finally:
        ari.close()


def test_parallel_workers_get_names_and_context(tmp_path, clock):
    root = make_repo(tmp_path / "r", TWO_WORKERS)
    ari = Ariadne(root, clock=clock)
    try:
        agent = ScriptedBrunel(PLAN, WORK)
        seen: list[AgentRequest] = []
        real = agent.run_agent
        agent.run_agent = lambda req: (seen.append(req), real(req))[1]
        report = run_task(ari, SPEC, actor=HUMAN, adapter=agent)
        assert report.decision.disposition is Disposition.ACCEPTED, report.decision.to_dict()
        state = ari.state(report.run_id)
        workers = [r for r in seen if r.task_id.startswith("r1-")]
        assert len(workers) == 3
        for req in workers:
            name = req.env[ENV_AGENT]
            assert state.agents[name]["principal"] == f"agent:worker:{req.task_id}"
            assert state.workers[req.task_id].principal == name
            assert req.env[ENV_REPO] == str(ari.root) and "## Coordination" in req.prompt
        # Each worker is retired as soon as its session ends: a finished before c was dispatched.
        events = [e for e in ari.store.events(report.run_id)]
        a_name = next(r.env[ENV_AGENT] for r in workers if r.task_id == "r1-a")
        retired_a = next(
            i for i, e in enumerate(events) if e.type == "agent.retired" and e.payload["name"] == a_name
        )
        c_registered = next(
            i for i, e in enumerate(events) if e.type == "agent.registered" and e.payload["task_id"] == "r1-c"
        )
        assert retired_a < c_registered
        planner = next(r for r in seen if r.task_id.startswith("plan-"))
        assert planner.env == {}  # a short read-only planning session is not a coordinating agent
        assert FileBus(root / ".daedalus" / "bus").subscribers() == {}
    finally:
        ari.close()


def test_the_command_adapter_grants_bus_commands_separately():
    adapter = make_adapter({"name": "claude-code"})
    assert isinstance(adapter, CommandAdapter)
    req = AgentRequest("r", "t", "brunel", "p", ".", bus_commands=(BUS_COMMAND,))
    cmd, _ = adapter._command(req)
    assert f"Bash({BUS_COMMAND}:*)" in cmd
    cmd, _ = adapter._command(
        AgentRequest("r", "t", "aristotle", "p", ".", read_only=True, bus_commands=(BUS_COMMAND,))
    )
    assert f"Bash({BUS_COMMAND}:*)" not in cmd  # never for a read-only session


# ----------------------------------------------------------- alias authority
def test_findings_and_blockers_are_resolved_by_any_alias_of_their_principal(repo, ari, clock):
    from daedalus.core.errors import AuthorizationError

    run_id = ari.start({"objective": "o"}, actor=HUMAN)
    ch = Channel(ari, run_id, FileBus(repo / ".daedalus" / "bus", clock=clock))
    (repo / "app.txt").write_text("ok\n", encoding="utf-8")
    ari.record_candidate(run_id, actor="agent:brunel")
    first = ch.spawn("aristotle", "review", "agent:reviewer:aristotle")
    ari.submit_review(run_id, reviewer=first, perspective="aristotle", findings=[FINDING, FINDING])
    later = ch.spawn("aristotle", "review", "agent:reviewer:aristotle")
    stranger = ch.spawn("aristotle", "review", "agent:reviewer:other")
    with pytest.raises(AuthorizationError):
        ari.resolve_finding(run_id, "F1", actor=stranger)
    ari.resolve_finding(run_id, "F1", actor=later)
    assert ari.state(run_id).findings["F1"].resolved_by == later

    b1 = ch.spawn("brunel", "t1", "agent:brunel")
    b2 = ch.spawn("brunel", "t2", "agent:brunel")
    other = ch.spawn("brunel", "t3", "agent:worker:t3")
    bid = ari.raise_blocker(run_id, "need a decision", actor=b1)
    with pytest.raises(AuthorizationError):
        ari.resolve_blocker(run_id, bid, actor=other)
    ari.resolve_blocker(run_id, bid, actor=b2)
    assert not ari.state(run_id).open_blockers


# -------------------------------------------------------------- CLI confinement
@pytest.fixture
def session(repo, ari, clock, monkeypatch):
    run_id = ari.start({"objective": "o"}, actor=HUMAN)
    bus_dir = repo / ".daedalus" / "bus"
    ch = Channel(ari, run_id, FileBus(bus_dir))
    me, peer = ch.spawn("brunel", "t1", "agent:brunel"), ch.spawn("brunel", "t2", "agent:worker:t2")
    for k, v in {ENV_AGENT: me, ENV_RUN: run_id, ENV_BUS_DIR: str(bus_dir), ENV_REPO: str(repo)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DAEDALUS_AGENT", "1")
    return {"repo": repo, "run": run_id, "me": me, "peer": peer, "bus": bus_dir, "tmp": repo.parent}


def test_an_agent_session_acts_only_as_itself(session, capsys):
    from daedalus.cli import main

    r = str(session["repo"])
    assert main(["bus", "join"]) == 0  # no -C: DAEDALUS_REPO is used
    assert main(["-C", r, "bus", "post", "note", "hi"]) == 0  # the same repository, given explicitly
    refused = [
        ["bus", "--as", session["peer"], "post", "note", "as someone else"],
        ["bus", "--run", "otherrun", "read"],
        ["bus", "--bus-dir", str(session["tmp"] / "elsewhere"), "read"],
        ["-C", str(session["tmp"]), "bus", "read"],
        ["bus", "reconcile"],
        ["bus", "expire"],
    ]
    for argv in refused:
        capsys.readouterr()
        assert main(argv) == 2, argv
        assert "daedalus:" in capsys.readouterr().err


def test_an_explicit_dir_is_not_overridden_outside_agent_sessions(repo, ari, monkeypatch, capsys, tmp_path):
    from daedalus.cli import main

    run_id = ari.start({"objective": "o"}, actor=HUMAN)
    name = Channel(ari, run_id, FileBus(repo / ".daedalus" / "bus")).spawn("brunel", "t1", "agent:brunel")
    monkeypatch.setenv(ENV_REPO, str(tmp_path / "not-a-repo"))
    monkeypatch.chdir(repo)
    assert main(["-C", ".", "bus", "--as", name, "join"]) == 0  # `-C .` means this directory
    assert main(["-C", ".", "bus", "reconcile"]) == 0  # maintenance is fine outside agent sessions
