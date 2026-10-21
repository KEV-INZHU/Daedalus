"""Codex CLI integration: hooks.json + AGENTS.md install, detect, the launch profile and the
`codex exec --json` parser. Behaviour was also checked against a real codex-cli 0.163.0 and a
mock Responses server (see docs/harnesses/codex.md)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_repo
from daedalus.adapters.base import AGENT_MARKER, AgentRequest
from daedalus.adapters.command import PRESETS, CommandAdapter, codex_result
from daedalus.harness import claude_code, codex
from daedalus.plugins import HarnessIntegration

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture(autouse=True)
def not_a_launched_session(monkeypatch):
    monkeypatch.delenv(AGENT_MARKER, raising=False)


def hooks_doc(root: Path) -> dict:
    return json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------- install
def test_integration_object():
    assert isinstance(codex.INTEGRATION, HarnessIntegration)
    assert codex.INTEGRATION.name == "codex" and codex.INTEGRATION.adapter_preset == "codex"
    assert codex.INTEGRATION.adapter_preset in PRESETS


def test_install_writes_all_four_hooks_with_portable_commands(tmp_path):
    done = codex.install(tmp_path)
    doc = hooks_doc(tmp_path)
    assert set(doc["hooks"]) == {"Stop", "SessionStart", "UserPromptSubmit", "PreToolUse"}
    for event, sub in (("Stop", "stop"), ("SessionStart", "session-start"), ("UserPromptSubmit", "prompt"),
                       ("PreToolUse", "pre-edit")):
        [group] = doc["hooks"][event]
        [h] = group["hooks"]
        assert f"daedalus hook {sub}" in h["command"] and "command -v daedalus" in h["command"]
        assert h["type"] == "command"
    assert doc["hooks"]["Stop"][0]["hooks"][0]["timeout"] == claude_code.STOP_HOOK_TIMEOUT_S
    assert doc["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch|Edit|Write"
    assert any(line.startswith("hook Stop") for line in done)
    assert any("trust" in line or "/hooks" in line for line in done)


def test_install_is_idempotent_and_updates_in_place(tmp_path):
    codex.install(tmp_path)
    first = (tmp_path / ".codex" / "hooks.json").read_text()
    agents = (tmp_path / "AGENTS.md").read_text()
    codex.install(tmp_path)
    assert (tmp_path / ".codex" / "hooks.json").read_text() == first
    assert (tmp_path / "AGENTS.md").read_text() == agents


def test_install_merges_with_user_hooks_and_replaces_old_daedalus_hooks(tmp_path):
    (tmp_path / ".codex").mkdir()
    mine = {"type": "command", "command": "echo mine"}
    old = {"type": "command", "command": "python -m daedalus hook stop"}
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps({
        "extra": 1,
        "hooks": {"Stop": [{"hooks": [mine, old]}], "PostToolUse": [{"hooks": [mine]}]},
    }))
    codex.install(tmp_path)
    doc = hooks_doc(tmp_path)
    assert doc["extra"] == 1 and doc["hooks"]["PostToolUse"] == [{"hooks": [mine]}]
    cmds = [h["command"] for g in doc["hooks"]["Stop"] for h in g["hooks"]]
    assert "echo mine" in cmds and sum("daedalus hook stop" in c for c in cmds) == 1
    assert not any("python -m" in c for c in cmds)


def test_install_refuses_invalid_json_without_clobbering(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text("{nope")
    with pytest.raises(Exception, match="not valid JSON"):
        codex.install(tmp_path)
    assert (tmp_path / ".codex" / "hooks.json").read_text() == "{nope"


def test_install_warns_about_inline_hooks_in_config_toml(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[[hooks.Stop]]\n')
    assert any(line.startswith("warning") for line in codex.install(tmp_path))


def test_agents_md_block_is_merged_not_clobbered(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n\nbe nice\n")
    codex.install(tmp_path)
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.startswith("# Mine\n\nbe nice\n") and codex.BEGIN in text and text.count(codex.END) == 1
    assert "daedalus" in text.lower() and "name: daedalus-gate" not in text  # front matter dropped
    (tmp_path / "AGENTS.md").write_text(text.replace("be nice", "be kind"))
    codex.install(tmp_path)
    assert "be kind" in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / "AGENTS.md").read_text().count(codex.BEGIN) == 1


def test_uninstall_removes_only_ours(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n")
    (tmp_path / ".codex").mkdir()
    mine = {"type": "command", "command": "echo mine"}
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [mine]}]}}))
    codex.install(tmp_path)
    codex.uninstall(tmp_path)
    assert hooks_doc(tmp_path) == {"hooks": {"Stop": [{"hooks": [mine]}]}}
    assert (tmp_path / "AGENTS.md").read_text() == "# Mine\n"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    codex.install(fresh)
    codex.uninstall(fresh)
    assert not (fresh / ".codex" / "hooks.json").exists() and not (fresh / "AGENTS.md").exists()


def test_detect(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert not codex.detect(tmp_path)
    (tmp_path / ".codex").mkdir()
    assert codex.detect(tmp_path)
    other = tmp_path / "o"
    other.mkdir()
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/codex" if n == "codex" else None)
    assert codex.detect(other)


# --------------------------------------------------------- hooks end to end
def codex_payload(root: Path, event: str, **extra) -> str:
    """The stdin Codex 0.163 sends (observed): common fields plus per-event ones."""
    return json.dumps({"session_id": "019-abc", "turn_id": "t1", "transcript_path": "/x/rollout.jsonl",
                       "cwd": str(root), "hook_event_name": event, "model": "gpt-5", "permission_mode": "default",
                       **extra})


def test_codex_events_drive_the_gate_through_the_installed_commands(tmp_path):
    """Codex-shaped payloads, fed to the committed hook commands: a changed tree makes Stop block with
    the reason on stdout; session start injects additionalContext."""
    root = make_repo(tmp_path / "r")
    codex.install(root)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "daedalus"
    shim.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -I -c '
                    f'"import sys; sys.path.insert(0, \'{SRC}\'); from daedalus.cli import main; sys.exit(main())" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    env.pop(AGENT_MARKER, None)
    doc = hooks_doc(root)

    def fire(event: str, **extra) -> str:
        cmd = doc["hooks"][event][0]["hooks"][0]["command"]
        r = subprocess.run(["sh", "-c", cmd], input=codex_payload(root, event, **extra), capture_output=True,
                           text=True, env=env, cwd=root, check=False)
        assert r.returncode == 0, r.stderr
        return r.stdout

    ctx = json.loads(fire("SessionStart", source="startup"))["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart" and "Daedalus" in ctx["additionalContext"]
    assert fire("UserPromptSubmit", prompt="make the app say ok") == ""
    (root / "app.txt").write_text("still bad\n")  # changed through a shell: no PreToolUse fired
    out = json.loads(fire("Stop", stop_hook_active=False, last_assistant_message="done"))
    assert out["decision"] == "block" and "tests" in out["reason"]


def test_hooks_are_noops_in_sessions_daedalus_launched(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "r")
    monkeypatch.setenv(AGENT_MARKER, "1")
    assert claude_code.run_hook("stop", codex_payload(root, "Stop")) == ""
    assert claude_code.run_hook("pre-edit", codex_payload(root, "PreToolUse", tool_name="apply_patch",
                                                          tool_input={"command": "*** Begin Patch"})) == ""


def test_apply_patch_payload_without_a_file_path_opens_a_run(tmp_path):
    root = make_repo(tmp_path / "r")
    claude_code.run_hook("prompt", codex_payload(root, "UserPromptSubmit", prompt="fix it"))
    out = claude_code.run_hook("pre-edit", codex_payload(root, "PreToolUse", tool_name="apply_patch",
                                                         tool_input={"command": "*** Begin Patch\n*** End Patch"}))
    assert json.loads(out)["systemMessage"]


# ------------------------------------------------------------------- preset
def test_preset_review_launch_is_read_only_and_has_no_write_flag():
    ad = CommandAdapter.from_config({"name": "codex"})
    cmd, stdin = ad._command(_req(True))
    assert cmd[:2] == ["codex", "exec"] and "--json" in cmd
    i = cmd.index("--sandbox")
    assert cmd[i + 1] == "read-only" and "workspace-write" not in cmd and "danger-full-access" not in cmd
    assert "--full-auto" not in cmd and not any("dangerously" in c for c in cmd)
    assert cmd[cmd.index("--disable") + 1] == "hooks"  # a reviewer runs no project hooks
    assert stdin == b"review please"


def test_preset_builder_launch_uses_workspace_write_and_no_removed_flag():
    ad = CommandAdapter.from_config({"name": "codex"})
    cmd, _ = ad._command(_req(False))
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--full-auto" not in cmd and "read-only" not in cmd


def _req(read_only: bool) -> AgentRequest:
    return AgentRequest(run_id="r", task_id="t", role="socrates", prompt="review please", cwd=Path("."),
                        read_only=read_only)


# ------------------------------------------------------------------- parser
OBSERVED = "\n".join([
    '{"type":"thread.started","thread_id":"01a1"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `mock` not found."}}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"first"}}',
    '{"type":"item.completed","item":{"id":"item_4","type":"agent_message","text":"{\\"verdict\\": \\"ok\\"}"}}',
    '{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":4,"cache_write_input_tokens":0,'
    + '"output_tokens":10,"reasoning_output_tokens":2}}',
])


def test_parser_takes_the_last_agent_message_and_sums_usage():
    res = codex_result(OBSERVED)
    assert res.completed and res.error is None and res.message == '{"verdict": "ok"}'
    assert res.usage["input_tokens"] == 20 and res.usage["cache_read_input_tokens"] == 4
    assert res.usage["output_tokens"] == 10 and res.usage["reasoning_output_tokens"] == 2


def test_warning_items_are_not_failures_but_turn_failed_is():
    assert codex_result(OBSERVED).error is None
    bad = '{"type":"turn.started"}\n{"type":"turn.failed","error":{"message":"quota exceeded"}}\n'
    res = codex_result(bad)
    assert not res.completed and "quota exceeded" in res.error


def test_adapter_parse_maps_results():
    ad = CommandAdapter.from_config({"name": "codex"})
    ok = ad._parse(OBSERVED, "", 0, "s")
    assert ok.status == "COMPLETED" and ok.structured == {"verdict": "ok"} and ok.usage["output_tokens"] == 10
    assert ad._parse('{"type":"turn.started"}', "boom", 1, "s").status == "FAILED"
    assert ad._parse("garbage", "", 0, "s").status == "FAILED"  # never mistaken for success


# ----------------------------------------------- unexpected shapes and markers (F13)
@pytest.mark.parametrize("doc", [
    {"hooks": []},
    {"hooks": {"Stop": {"hooks": []}}},
    {"hooks": {"Stop": ["not a group"]}},
    {"hooks": {"Stop": [{"hooks": "nope"}]}},
    {"hooks": {"Stop": [{"hooks": ["not an object"]}]}},
])
def test_install_and_uninstall_refuse_unexpected_hooks_shapes(tmp_path, doc):
    from daedalus.core.errors import DaedalusError

    (tmp_path / ".codex").mkdir()
    path = tmp_path / ".codex" / "hooks.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(DaedalusError, match="hooks"):
        codex.install(tmp_path)
    with pytest.raises(DaedalusError, match="hooks"):
        codex.uninstall(tmp_path)
    assert json.loads(path.read_text()) == doc and not (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("missing", ["begin", "end"])
def test_agents_md_with_a_half_deleted_block_is_refused_untouched(tmp_path, missing):
    from daedalus.core.errors import DaedalusError

    codex.install(tmp_path)
    agents = tmp_path / "AGENTS.md"
    text = agents.read_text()
    text = text.replace(codex.END, "") if missing == "end" else text.replace(codex.BEGIN, "")
    agents.write_text("# Mine\n" + text + "\nmy own notes\n")
    before = agents.read_text()
    hooks_before = (tmp_path / ".codex" / "hooks.json").read_text()
    with pytest.raises(DaedalusError, match="marker"):
        codex.install(tmp_path)
    with pytest.raises(DaedalusError, match="marker"):
        codex.uninstall(tmp_path)
    assert agents.read_text() == before and (tmp_path / ".codex" / "hooks.json").read_text() == hooks_before


def test_install_leaves_git_hooks_to_the_cli(tmp_path):
    from conftest import make_repo

    root = make_repo(tmp_path / "r")
    codex.install(root)
    assert not (root / ".git" / "hooks" / "prepare-commit-msg").exists()
