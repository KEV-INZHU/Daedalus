"""Built-in presets: review launches are exactly the read-only profile (run 27c6 review)."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

from conftest import make_repo
from daedalus.adapters.base import AgentRequest
from daedalus.adapters.command import PRESETS, CommandAdapter
from daedalus.core.errors import CapabilityError

EXACT_REVIEW_ARGV = {
    "claude-code": [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--disable-slash-commands",
        "--restricted", "--tools", "Read,Grep,Glob",
    ],
    "codex": ["codex", "exec", "--sandbox", "read-only", "PROMPT"],
    "aider": ["aider", "--yes-always", "--no-auto-commits", "--dry-run", "--message", "PROMPT"],
}
CONFIG_ARG = {"claude-code": "--verbose", "codex": "--dangerously-bypass-approvals-and-sandbox", "aider": "--x"}


def request(read_only: bool) -> AgentRequest:
    return AgentRequest("r", "t", "aristotle" if read_only else "brunel", "PROMPT", Path("."), read_only=read_only)


@pytest.mark.parametrize("name", sorted(EXACT_REVIEW_ARGV))
def test_review_launch_is_exactly_the_read_only_profile(name):
    adapter = CommandAdapter.from_config({"name": name, "args": [CONFIG_ARG[name]]})
    assert adapter._command(request(True))[0] == EXACT_REVIEW_ARGV[name]
    assert CONFIG_ARG[name] in adapter._command(request(False))[0]


def test_write_enabling_preset_flags_are_builder_only():
    for name, preset in PRESETS.items():
        shared = preset["argv"] + preset["args"] + preset["read_only_args"]
        for flag in ("--full-auto", "acceptEdits", "--permission-mode"):
            assert flag not in shared, (name, flag)
    assert "--full-auto" in CommandAdapter.from_config({"name": "codex"})._command(request(False))[0]


@pytest.mark.parametrize("name", ["codex", "aider"])
@pytest.mark.parametrize("cfg", [{"arg": ["typo"]}, {"sandbox": "danger"}, {"argv": ["x"]}, {"read_only_args": []}])
def test_unknown_or_fixed_preset_keys_are_refused(name, cfg):
    with pytest.raises(CapabilityError):
        CommandAdapter.from_config({"name": name, **cfg})


def test_cli_adapter_override_with_same_name_keeps_repo_settings(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r", {"adapter": {"name": "claude-code", "review_model": "sonnet"}})
    ari = cli._ari(argparse.Namespace(dir=str(root), adapter="claude-code"), adapter=True)
    try:
        assert ari.adapter.review_model == "sonnet"
    finally:
        ari.close()


def test_cli_adapter_override_with_custom_name_keeps_repo_settings(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r", {"adapter": {"name": "my-agent", "argv": ["my-agent"]}})
    ari = cli._ari(argparse.Namespace(dir=str(root), adapter="my-agent"), adapter=True)
    try:
        assert ari.adapter.argv == ["my-agent"]
    finally:
        ari.close()


def test_hook_stdin_bom_is_stripped(repo, monkeypatch):
    from daedalus import cli

    body = b"\xef\xbb\xbf" + json.dumps({"cwd": str(repo)}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body)))
    seen: dict[str, str] = {}

    def fake(name: str, text: str) -> str:
        seen["text"] = text
        return ""

    monkeypatch.setattr("daedalus.harness.claude_code.run_hook", fake)
    cli.main(["hook", "stop"])
    assert json.loads(seen["text"])["cwd"] == str(repo)
