"""Hermes Agent integration: install, the plugin shim's translation, and the adapter preset."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import fix, make_repo
from daedalus.adapters import command
from daedalus.adapters.base import AGENT_MARKER, AgentRequest
from daedalus.harness import hermes

SRC = Path(hermes.__file__).resolve().parents[2]


@pytest.fixture
def shim(monkeypatch):
    monkeypatch.delenv(AGENT_MARKER, raising=False)
    spec = importlib.util.spec_from_file_location("daedalus_hermes_shim", hermes.SHIM_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.said = []
    mod._ctx = SimpleNamespace(session_cwd=lambda: "", post_message=lambda t: mod.said.append(t))
    return mod


@pytest.fixture
def daedalus_on_path(tmp_path, monkeypatch):
    """A `daedalus` executable on PATH that runs this checkout (the installed one may be another)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "daedalus"
    exe.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m daedalus "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


# -------------------------------------------------------------------- install
def test_install_writes_the_plugin_into_the_repo_and_prints_the_enable_command(repo):
    lines = hermes.install(repo)
    for name in hermes.SHIM_FILES:
        assert (repo / ".hermes" / "plugins" / "daedalus" / name).read_bytes() == (hermes.SHIM_DIR / name).read_bytes()
    text = "\n".join(lines)
    assert "hermes plugins enable daedalus" in text and ".hermes/plugins/daedalus" in text
    assert "hook_callback_timeout" in text


def test_install_is_idempotent_and_updates_in_place(repo):
    hermes.install(repo)
    plugin = repo / ".hermes" / "plugins" / "daedalus" / "__init__.py"
    plugin.write_text(f"# {hermes.MARK}\n# stale\n")
    other = repo / ".hermes" / "plugins" / "mine" / "__init__.py"
    other.parent.mkdir(parents=True)
    other.write_text("# the user's own plugin\n")
    (repo / ".hermes.md").write_text("# project notes\n")
    first = hermes.install(repo)
    assert plugin.read_bytes() == (hermes.SHIM_DIR / "__init__.py").read_bytes()
    assert other.read_text() == "# the user's own plugin\n"  # merged, never clobbered
    assert (repo / ".hermes.md").read_text() == "# project notes\n"
    assert hermes.install(repo) == first


def test_install_never_writes_outside_the_repo(repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hermes.install(repo)
    assert list(home.iterdir()) == []


def test_detect(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert not hermes.detect(tmp_path)
    (tmp_path / ".hermes").mkdir()
    assert hermes.detect(tmp_path)


def test_integration_record():
    assert (hermes.INTEGRATION.name, hermes.INTEGRATION.adapter_preset) == ("hermes", "hermes")


def test_shipped_shim_references_the_daedalus_hooks():
    src = (hermes.SHIM_DIR / "__init__.py").read_text()
    for needle in ('"session-start"', '"prompt"', '"pre-edit"', '"stop"', "pre_verify", "pre_tool_call", "pre_llm_call"):
        assert needle in src
    manifest = (hermes.SHIM_DIR / "plugin.yaml").read_text()
    assert "name: daedalus" in manifest and "pre_verify" in manifest


# ----------------------------------------------------------------------- shim
def test_shim_registers_the_three_hooks(shim):
    seen = {}
    shim.register(SimpleNamespace(register_hook=lambda n, f: seen.update({n: f}), session_cwd=lambda: ""))
    assert set(seen) == {"pre_llm_call", "pre_tool_call", "pre_verify"}


def test_shim_is_a_noop_without_daedalus_or_inside_a_launched_session(shim, monkeypatch, repo):
    with monkeypatch.context() as m:
        m.setattr(shim.shutil, "which", lambda _n: None)
        assert shim.pre_verify(session_id="s") is None
        assert shim.pre_llm_call(session_id="s", user_message="hi", is_first_turn=True) is None
    monkeypatch.setenv(AGENT_MARKER, "1")
    assert shim._run("stop", {"cwd": str(repo)}, 5) == {}


def test_shim_ignores_non_edit_tools_and_never_blocks(shim, monkeypatch):
    called = []
    monkeypatch.setattr(shim, "_run", lambda *a, **k: called.append(a) or {})
    assert shim.pre_tool_call(tool_name="read_file", args={"path": "x"}) is None
    assert shim.pre_tool_call(tool_name="write_file", args={"path": "x"}) is None
    assert [c[0] for c in called] == ["pre-edit"]
    assert called[0][1]["tool_input"] == {"file_path": "x"}


def test_shim_translates_block_to_continue_and_acceptance_to_a_message(shim, monkeypatch):
    monkeypatch.setattr(shim, "_wait_budget", lambda: 5)
    monkeypatch.setattr(shim, "_run", lambda *a, **k: {"decision": "block", "reason": "fix the tests"})
    assert shim.pre_verify(session_id="s") == {"action": "continue", "message": "Stop hook feedback:\nfix the tests"}
    monkeypatch.setattr(shim, "_run", lambda *a, **k: {"systemMessage": "Daedalus: ACCEPTED"})
    assert shim.pre_verify(session_id="s") is None
    assert shim.said == ["Daedalus: ACCEPTED"]


def test_shim_delivers_a_verdict_that_outlived_hermes_timeout_on_the_next_turn(shim, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(shim, "_wait_budget", lambda: 0.05)
    monkeypatch.setattr(shim, "_run", lambda *a, **k: gate.wait(5) and {"decision": "block", "reason": "late"})
    assert shim.pre_verify(session_id="s") is None  # Hermes gives up waiting: fail open, never accept
    gate.set()
    for _ in range(200):
        if "s" in shim._pending:
            break
        threading.Event().wait(0.02)
    monkeypatch.setattr(shim, "_run", lambda *a, **k: {})
    assert shim.pre_llm_call(session_id="s", user_message="go on", is_first_turn=False) == {"context": "late"}


def test_shim_injects_session_start_context_on_the_first_turn_only(shim, monkeypatch):
    def fake(sub, payload, timeout):
        return {"hookSpecificOutput": {"additionalContext": "gated"}} if sub == "session-start" else {}

    monkeypatch.setattr(shim, "_run", fake)
    assert shim.pre_llm_call(session_id="s1", user_message="hi", is_first_turn=True) == {"context": "gated"}
    assert shim.pre_llm_call(session_id="s1", user_message="more", is_first_turn=False) is None


def test_shim_end_to_end_through_daedalus_hook(shim, daedalus_on_path, tmp_path):
    repo = make_repo(tmp_path / "gated")
    shim._ctx = SimpleNamespace(session_cwd=lambda: str(repo), post_message=lambda t: shim.said.append(t))
    out = shim.pre_llm_call(session_id="s1", user_message="make the app say ok", is_first_turn=True)
    assert "Daedalus acceptance gate" in out["context"]
    shim.pre_tool_call(tool_name="write_file", args={"path": "app.txt"}, session_id="s1")
    assert any("opened run" in m for m in shim.said)
    # the check fails (app.txt says "bad"): the gate sends the agent back with the reason
    verdict = shim.pre_verify(session_id="s1", changed_paths=["app.txt"])
    assert verdict["action"] == "continue" and "not accepted yet" in verdict["message"]
    fix(repo)
    shim.said.clear()
    assert shim.pre_verify(session_id="s1", changed_paths=["app.txt"]) is None
    assert any("ACCEPTED" in m for m in shim.said)


# --------------------------------------------------------------------- preset
def test_preset_review_argv_is_read_only():
    adapter = command.CommandAdapter.from_config({"name": "hermes"})
    cmd, stdin = adapter._command(AgentRequest("r", "t", "socrates", "review this", Path("."), read_only=True))
    assert cmd[:3] == ["hermes", "chat", "--oneshot"] and stdin == b"review this"
    assert cmd[cmd.index("--toolsets") + 1] == "todo"  # no file, terminal, web or MCP tools
    assert "--ignore-rules" in cmd
    for flag in ("--yolo", "--accept-hooks", "--worktree", "all", "file", "terminal"):
        assert flag not in cmd
    assert cmd[cmd.index("--source") + 1] == "daedalus"


def test_preset_builder_argv_keeps_default_tools():
    adapter = command.CommandAdapter.from_config({"name": "hermes"})
    cmd, _ = adapter._command(AgentRequest("r", "t", "brunel", "do it", Path(".")))
    assert "--toolsets" not in cmd and "--ignore-rules" not in cmd
    assert "experimental preset" in adapter.capabilities.limitations


def test_continue_message_carries_the_harness_prompt_prefix(shim, monkeypatch):
    from daedalus.harness.claude_code import HARNESS_PROMPTS, is_harness_prompt

    monkeypatch.setattr(shim, "_wait_budget", lambda: 5)
    monkeypatch.setattr(shim, "_run", lambda *a, **k: {"decision": "block", "reason": "fix it"})
    message = shim.pre_verify(session_id="s")["message"]
    assert message.startswith("Stop hook feedback:\n") and is_harness_prompt(message)
    assert tuple(shim.HARNESS_PROMPTS) == tuple(HARNESS_PROMPTS)  # the stdlib-only copy has not drifted


def test_prompt_hook_is_skipped_for_harness_injected_messages(shim, monkeypatch):
    from daedalus.harness.claude_code import HARNESS_PROMPTS

    called = []
    monkeypatch.setattr(shim, "_run", lambda sub, *a, **k: called.append(sub) or {})
    for prefix in HARNESS_PROMPTS:
        shim.pre_llm_call(session_id="s", user_message=f"  {prefix} whatever", is_first_turn=False)
    assert "prompt" not in called
    shim.pre_llm_call(session_id="s", user_message="please add a test", is_first_turn=False)
    assert called.count("prompt") == 1


def test_install_refuses_files_that_are_not_ours(repo):
    from daedalus.core.errors import DaedalusError

    mine = repo / ".hermes" / "plugins" / "daedalus" / "__init__.py"
    mine.parent.mkdir(parents=True)
    mine.write_text("# somebody else's plugin\n")
    with pytest.raises(DaedalusError, match="not Daedalus's"):
        hermes.install(repo)
    assert mine.read_text() == "# somebody else's plugin\n"
    assert not (mine.parent / "plugin.yaml").exists()  # nothing half-installed


def test_shipped_files_carry_the_ownership_marker():
    for name in hermes.SHIM_FILES:
        assert hermes.MARK in (hermes.SHIM_DIR / name).read_text(encoding="utf-8")


def test_install_does_not_touch_git_hooks(repo):
    hermes.install(repo)
    assert not (repo / ".git" / "hooks" / "prepare-commit-msg").exists()
