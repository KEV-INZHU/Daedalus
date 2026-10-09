"""Lean launch profiles: plan usage per launched session, and locked read-only reviews."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.adapters.base import AgentRequest
from daedalus.adapters.command import CLAUDE_REVIEW_TOOLS, CommandAdapter
from daedalus.core.errors import CapabilityError

EDITING_OR_SHELL = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell")


def argv(cfg: dict, *, read_only: bool) -> list[str]:
    adapter = CommandAdapter.from_config({"name": "claude-code", **cfg})
    req = AgentRequest("r", "t", "aristotle" if read_only else "brunel", "prompt", Path("."), read_only=read_only)
    return adapter._command(req)[0]


@pytest.mark.parametrize("read_only", [True, False])
def test_launches_skip_mcp_servers_and_skills(read_only):
    cmd = argv({}, read_only=read_only)
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert "--mcp-config" not in cmd


def test_review_launch_gets_only_an_allowlist_of_read_tools():
    cmd = argv({}, read_only=True)
    assert cmd[cmd.index("--tools") + 1] == CLAUDE_REVIEW_TOOLS == "Read,Grep,Glob"
    assert "--disallowedTools" not in cmd
    assert not any(t in CLAUDE_REVIEW_TOOLS.split(",") for t in EDITING_OR_SHELL)


def test_builder_launch_is_not_restricted_to_read_tools():
    assert "--tools" not in argv({}, read_only=False)


def test_models_are_selected_per_role():
    assert "--model=sonnet" in argv({"model": "opus", "review_model": "sonnet"}, read_only=True)
    assert "--model=opus" in argv({"model": "opus", "review_model": "sonnet"}, read_only=False)
    assert "--model=opus" in argv({"model": "opus"}, read_only=True)  # review falls back to model
    assert not any(a.startswith("--model") for a in argv({}, read_only=True))


@pytest.mark.parametrize(
    "cfg",
    [
        {"read_only_args": ["--tools", "Read,Bash"]},
        {"argv": ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]},
        {"model_arg": "--tools", "review_model": "Read,Grep,Glob,Bash"},
        {"prompt_via": "arg"},
        {"args": ["--tools", "default"]},
        {"args": ["--allowedTools=Bash"]},
        {"args": ["--dangerously-skip-permissions"]},
        {"args": ["--mcp-config", "servers.json"]},
        {"args": ["--plugin-dir", "x"]},
        {"args": ["--settings", "s.json"]},
        {"args": ["--agents", "{}"]},
        {"args": ["--permission-mode", "bypassPermissions"]},
        {"args": "--tools default"},
        {"args": ["--max-turns", "--tools"]},
        {"args": ["--max-turns"]},
        {"args": ["--verbose=yes"]},
        {"args": ["--fallback-model", "-x"]},
        {"review_model": "--dangerously-skip-permissions"},
        {"model": ""},
        {"model": 4},
    ],
)
def test_config_cannot_change_the_locked_launch_line(cfg):
    with pytest.raises(CapabilityError):
        CommandAdapter.from_config({"name": "claude-code", **cfg})


def test_allowed_extra_args_reach_builders_only():
    cfg = {"args": ["--verbose", "--max-turns", "40", "--fallback-model=sonnet"]}
    builder = argv(cfg, read_only=False)
    assert builder[-4:] == ["--verbose", "--max-turns", "40", "--fallback-model=sonnet"]
    review = argv(cfg, read_only=True)
    assert "--verbose" not in review and review[-2:] == ["--tools", CLAUDE_REVIEW_TOOLS]


def test_model_is_a_single_argument():
    assert "--model=sonnet" in argv({"review_model": "sonnet"}, read_only=True)


@pytest.mark.parametrize(
    "cfg",
    [
        {"name": "codex", "read_only_args": []},
        {"name": "aider", "read_only_args": []},
        {"name": "codex", "model": "o3"},
        {"name": "custom", "argv": ["my-agent"], "review_model": "x"},
    ],
)
def test_other_adapters_cannot_drop_read_only_or_take_model_flags(cfg):
    with pytest.raises(CapabilityError):
        CommandAdapter.from_config(cfg)


def test_codex_review_keeps_its_read_only_sandbox():
    adapter = CommandAdapter.from_config({"name": "codex"})
    cmd = adapter._command(AgentRequest("r", "t", "aristotle", "p", Path("."), read_only=True))[0]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert not any(a.startswith("--model") for a in cmd)
