"""Builders may run the repository's trusted checks, and only those, before handing in work."""

from __future__ import annotations

import pytest

from conftest import HUMAN, make_repo
from daedalus.adapters.base import AgentRequest, AgentResult
from daedalus.adapters.command import CommandAdapter
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.orchestration import prompts
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import run_task

PYTEST = {"tests": {"command": ["python", "-m", "pytest", "-q"], "timeout_s": 60}}


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        (["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"], "python -m pytest"),
        (["npm", "test", "--silent"], "npm test"),
        (["cargo", "test", "--quiet"], "cargo test"),
        (["uv", "run", "pytest"], "uv run pytest"),
        (["make", "test"], "make test"),
        (["/usr/bin/python3", "-m", "pytest"], "/usr/bin/python3 -m pytest"),
        # Fail closed: nothing that would let a Builder run arbitrary code, or alter the rule.
        (["python", "-c", "print(1)"], ""),
        (["/usr/bin/python3", "-c", "x"], ""),
        (["sh", "-c", "pytest"], ""),
        (["bash"], ""),
        (["make"], ""),
        (["uv", "run"], ""),
        (["npx"], ""),
        (["pytest", "tests/a,b"], ""),
        (["pytest", "$(evil)"], ""),
        (["-x"], ""),
        (["python3.12", "-c", "x"], ""),
        (["C:\\Python\\python.exe", "-m", "pytest"], ""),  # backslash paths are never granted
        (["/opt/py/bin/python3.11", "-m", "pytest"], "/opt/py/bin/python3.11 -m pytest"),
        (["python.exe", "-c", "x"], ""),  # only the name normalisation rejects these two
        (["node.exe"], ""),
        (["/usr/bin/python3.12", "-c", "x"], ""),
    ],
)
def test_check_prefix(command, prefix):
    assert prompts.check_prefix(command) == prefix


def requests_for(root, clock):
    seen: list[AgentRequest] = []

    def build(req):
        seen.append(req)
        return AgentResult("COMPLETED", structured={"status": "done", "summary": "s"})

    a = Ariadne(root, clock=clock)
    run_task(a, {"objective": "o"}, actor=HUMAN, adapter=SimulatedAdapter({"brunel": [build]}), max_rounds=1)
    a.close()
    return seen


def test_builder_requests_carry_exactly_the_check_prefixes(tmp_path, clock):
    (req,) = requests_for(make_repo(tmp_path / "r", {"checks": PYTEST}), clock)
    assert req.run_commands == ("python -m pytest",)
    assert "**start with** these prefixes" in req.prompt and "- `python -m pytest`" in req.prompt


def test_an_unsafe_check_is_not_granted_and_the_builder_is_told(tmp_path, clock):
    (req,) = requests_for(make_repo(tmp_path / "r"), clock)  # the default test check is `python -c ...`
    assert req.run_commands == ()
    assert "The gate also runs `tests`, which you cannot run yourself." in req.prompt


def test_the_policy_can_grant_nothing(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"checks": PYTEST, "policy": {"builder_runs_checks": False}})
    (req,) = requests_for(root, clock)
    assert req.run_commands == () and "cannot run yourself" in req.prompt


def test_fan_out_workers_are_told_what_they_may_run(ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    contract = ari.state(rid).contract
    text = prompts.worker_prompt(contract, {"task_id": "r1-a", "owned_paths": ["src/a/**"]},
                                 ("python -m pytest",), ("lint",))
    assert "- `python -m pytest`" in text and "`lint`, which you cannot run yourself" in text


def test_a_required_check_missing_from_policy_is_reported_not_granted(ari):
    from dataclasses import replace

    from daedalus.orchestration.lifecycle import check_commands

    state = ari.state(ari.start({"objective": "o"}, actor=HUMAN))
    state.contract = replace(state.contract, required_checks=("tests", "ghost"))
    granted, skipped = check_commands(state)
    assert "ghost" in skipped and all("ghost" not in g for g in granted)


def test_no_grant_when_the_prompt_is_an_argument(tmp_path):
    adapter = CommandAdapter.from_config({"name": "claude-code"})
    adapter.prompt_via = "arg"
    cmd, _ = adapter._command(AgentRequest("r", "t", "brunel", "the prompt", tmp_path, run_commands=("python -m pytest",)))
    assert "--allowedTools" not in cmd and cmd[-1] == "the prompt"


def test_builder_runs_checks_must_be_a_boolean(tmp_path):
    from daedalus.core.config import load_config
    from daedalus.core.errors import PolicyError

    with pytest.raises(PolicyError, match="builder_runs_checks"):
        load_config(make_repo(tmp_path / "r", {"policy": {"builder_runs_checks": "no"}}))


def test_claude_builder_launch_grants_each_rule_as_its_own_argument(tmp_path):
    adapter = CommandAdapter.from_config({"name": "claude-code"})
    build = AgentRequest("r", "t", "brunel", "p", tmp_path, run_commands=("python -m pytest", "npm test"))
    cmd, _ = adapter._command(build)
    i = cmd.index("--allowedTools")
    assert cmd[i + 1 : i + 3] == ["Bash(python -m pytest:*)", "Bash(npm test:*)"]
    review = AgentRequest("r", "t", "aristotle", "p", tmp_path, read_only=True, run_commands=("python -m pytest",))
    assert not any("Bash(" in a for a in adapter._command(review)[0])  # a reviewer never runs anything
    assert "--allowedTools" not in adapter._command(AgentRequest("r", "t", "brunel", "p", tmp_path))[0]
