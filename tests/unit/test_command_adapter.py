"""The claude-code preset against a fake CLI: output shapes and launch environment."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from daedalus.adapters.base import AgentRequest
from daedalus.adapters.command import HARNESS_SESSION_VARS, CommandAdapter, claude_result

FAKE = """
import json, os, sys
sys.stdin.read()
mode = sys.argv[1]
names = ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "DAEDALUS_AGENT"]
env = {n: os.environ.get(n) for n in names}
result = {"type": "result", "subtype": "success", "is_error": mode == "error",
          "result": "done\\n```json\\n" + json.dumps(env) + "\\n```", "total_cost_usd": 0.25}
if mode == "garbage":
    print("not json")
elif mode == "object":
    print(json.dumps(result))
elif mode == "max_turns":
    print(json.dumps([{"type": "result", "subtype": "error_max_turns", "total_cost_usd": 0.1}]))
else:
    print(json.dumps([{"type": "system", "subtype": "init"}, {"type": "assistant"}, result]))
if mode == "exit1":
    sys.exit(1)
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    p = tmp_path / "fake_claude.py"
    p.write_text(FAKE, encoding="utf-8")
    return p


def run(fake_cli: Path, mode: str, tmp_path: Path):
    adapter = CommandAdapter("claude-code", [sys.executable, str(fake_cli), mode], parse="claude-json")
    return adapter.run_agent(AgentRequest("r", "t", "brunel", "prompt", tmp_path, timeout_s=60))


@pytest.mark.parametrize("mode", ["array", "object"])
def test_both_output_shapes_parse(fake_cli, tmp_path, mode):
    res = run(fake_cli, mode, tmp_path)
    assert res.status == "COMPLETED", res.error
    assert res.output.startswith("done")
    assert res.cost == pytest.approx(0.25)
    assert res.structured is not None


def test_launched_session_does_not_inherit_harness_identity(fake_cli, tmp_path, monkeypatch):
    for name in HARNESS_SESSION_VARS:
        monkeypatch.setenv(name, "parent-session")
    res = run(fake_cli, "array", tmp_path)
    assert res.structured == {**{n: None for n in HARNESS_SESSION_VARS}, "DAEDALUS_AGENT": "1"}


@pytest.mark.parametrize("mode", ["error", "garbage", "max_turns", "exit1"])
def test_error_outputs_are_failed(fake_cli, tmp_path, mode):
    res = run(fake_cli, mode, tmp_path)
    assert res.status == "FAILED"
    if mode == "max_turns":
        assert "error_max_turns" in (res.error or "")
    if mode == "exit1":
        assert res.cost == pytest.approx(0.25)  # spend is kept even when the session failed


def test_claude_result_picks_last_result_message():
    msgs = [{"type": "result", "result": "old"}, {"type": "assistant"}, {"type": "result", "result": "new"}]
    assert claude_result(json.dumps(msgs))["result"] == "new"
    assert claude_result(json.dumps([{"type": "assistant"}])) is None
    assert claude_result("[1, 2") is None
    assert claude_result("{}") is None
    assert claude_result('{"type": "assistant"}') is None


def test_unreadable_cost_does_not_crash(tmp_path):
    adapter = CommandAdapter("claude-code", ["unused"], parse="claude-json")
    doc = {"type": "result", "subtype": "success", "result": "ok", "total_cost_usd": "lots"}
    res = adapter._parse(json.dumps(doc), "", 0, "s")
    assert res.status == "COMPLETED" and res.cost == 0.0
