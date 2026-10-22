"""Pi integration: install, detect, the shipped extension, the `pi` preset, and the shim end to end
(driven by Node with a fake Pi API, when Node can run TypeScript)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from conftest import fix
from daedalus.adapters.base import AGENT_MARKER, AgentRequest
from daedalus.adapters.command import PRESETS, CommandAdapter, pi_result
from daedalus.core.errors import DaedalusError
from daedalus.harness import pi as pi_harness

SRC = Path(__file__).resolve().parents[2] / "src"
EXT = SRC / "daedalus" / "data" / "harness" / "pi" / "daedalus-gate.ts"


# ------------------------------------------------------------------ install
def test_install_is_idempotent_and_leaves_other_extensions_alone(tmp_path):
    other = tmp_path / ".pi" / "extensions" / "mine.ts"
    other.parent.mkdir(parents=True)
    other.write_text("export default () => {};\n")
    settings = tmp_path / ".pi" / "settings.json"
    settings.write_text('{"theme": "dark"}\n')
    first = pi_harness.install(tmp_path)
    ext = tmp_path / ".pi" / "extensions" / "daedalus-gate.ts"
    body = ext.read_text()
    assert any("daedalus-gate.ts" in line for line in first)
    assert (tmp_path / ".pi" / "skills" / "daedalus-gate" / "SKILL.md").exists()
    second = pi_harness.install(tmp_path)
    assert ext.read_text() == body and len(second) == len(first)
    assert other.read_text() == "export default () => {};\n"
    assert json.loads(settings.read_text()) == {"theme": "dark"}


def test_install_updates_a_stale_copy_in_place_but_refuses_a_foreign_file(tmp_path):
    ext = tmp_path / ".pi" / "extensions" / "daedalus-gate.ts"
    ext.parent.mkdir(parents=True)
    ext.write_text(pi_harness.MARK + " (old)\n")
    pi_harness.install(tmp_path)
    assert "daedalus hook" in ext.read_text() and "(old)" not in ext.read_text()
    ext.write_text("// somebody else's file\n")
    with pytest.raises(DaedalusError):
        pi_harness.install(tmp_path)
    assert ext.read_text() == "// somebody else's file\n"


def test_install_bakes_in_the_bootstrap_command(tmp_path):
    src = pi_harness.extension_source("pip install -e . && echo 'x'")
    assert 'const BOOTSTRAP: string = "pip install -e . && echo \'x\'";' in src
    assert 'const BOOTSTRAP: string = "";' in pi_harness.extension_source(None)


def test_uninstall_removes_only_ours(tmp_path):
    pi_harness.install(tmp_path)
    assert pi_harness.uninstall(tmp_path)
    assert not (tmp_path / ".pi" / "extensions" / "daedalus-gate.ts").exists()
    assert pi_harness.uninstall(tmp_path) == []


def test_detect(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert not pi_harness.detect(tmp_path)
    (tmp_path / ".pi").mkdir()
    assert pi_harness.detect(tmp_path)
    (tmp_path / ".pi").rmdir()
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/pi" if n == "pi" else None)
    assert pi_harness.detect(tmp_path)


def test_integration_record():
    i = pi_harness.INTEGRATION
    assert (i.name, i.adapter_preset) == ("pi", "pi") and i.adapter_preset in PRESETS


def test_shipped_extension_references_every_hook_and_event():
    text = EXT.read_text()
    for name in ("session-start", "prompt", "pre-edit", "stop"):
        assert f'"{name}"' in text
    for event in ("session_start", "before_agent_start", "input", "tool_call", "agent_end"):
        assert f'pi.on("{event}"' in text
    assert "DAEDALUS_AGENT" in text and "sendUserMessage" in text
    assert "block: true" not in text  # the gate never vetoes a tool call


# ------------------------------------------------------------------- preset
def test_review_launch_is_read_only_and_builder_launch_is_not_restricted(tmp_path):
    ad = CommandAdapter.from_config({"name": "pi"})
    cmd, stdin = ad._command(AgentRequest("r", "t", "socrates", "look", tmp_path, read_only=True))
    assert cmd[0] == "pi" and "--no-session" in cmd and stdin == b"look"
    tools = cmd[cmd.index("--tools") + 1]
    assert set(tools.split(",")) == {"read", "grep", "find", "ls"}
    assert "--no-extensions" in cmd and "--no-context-files" in cmd
    assert not {"bash", "edit", "write"} & set(tools.split(","))
    build, _ = ad._command(AgentRequest("r", "t", "brunel", "do", tmp_path))
    assert "--tools" not in build
    assert "experimental preset" in ad.capabilities.limitations


def _events(*assistant: dict) -> str:
    lines = [{"type": "session", "id": "x"}, {"type": "agent_start"}]
    lines += [{"type": "message_end", "message": {"role": "user", "content": []}}]
    lines += [{"type": "message_end", "message": {"role": "assistant", **m}} for m in assistant]
    lines += [{"type": "agent_end", "messages": []}]
    return "\n".join(json.dumps(x) for x in lines) + "\nnot json\n"


def _usage(total: float) -> dict:
    return {"input": 10, "output": 5, "cost": {"total": total}}


def test_pi_result_parses_answer_cost_and_tokens():
    out = _events(
        {"content": [{"type": "text", "text": "first"}], "usage": _usage(0.5), "stopReason": "toolUse"},
        {"content": [{"type": "text", "text": 'done\n```json\n{"ok": true}\n```'}], "usage": _usage(0.25),
         "stopReason": "stop", "model": "m"},
    )
    r = pi_result(out)
    assert r and r["text"].startswith("done") and r["cost"] == 0.75 and r["input_tokens"] == 20
    assert pi_result("garbage") is None
    assert pi_result(_events({"content": "bad", "usage": {"cost": {"total": "NaN"}}}))["cost"] == 0.0


def _fake_pi(tmp_path: Path, stdout: str, code: int = 0) -> CommandAdapter:
    script = tmp_path / "fakepi.py"
    script.write_text(f"import sys\nsys.stdin.read()\nsys.stdout.write({stdout!r})\nsys.exit({code})\n")
    return CommandAdapter("pi", [sys.executable, str(script)], parse="pi-json", capabilities=PRESETS["pi"]["capabilities"])


def test_adapter_parses_a_pi_session(tmp_path):
    out = _events({"content": [{"type": "text", "text": 'ok\n```json\n{"verdict": "pass"}\n```'}],
                   "usage": _usage(0.1), "stopReason": "stop"})
    res = _fake_pi(tmp_path, out).run_agent(AgentRequest("r", "t", "mozi", "p", tmp_path, read_only=True))
    assert res.status == "COMPLETED" and res.structured == {"verdict": "pass"}
    assert res.cost == 0.1 and res.cost_basis == "cash" and res.usage["input_tokens"] == 10


def test_adapter_fails_on_error_stop_reason_or_unparseable_output(tmp_path):
    req = AgentRequest("r", "t", "mozi", "p", tmp_path)
    err = _events({"content": [], "usage": _usage(0), "stopReason": "error", "errorMessage": "no key"})
    res = _fake_pi(tmp_path, err).run_agent(req)
    assert res.status == "FAILED" and "no key" in res.error
    assert _fake_pi(tmp_path, "nothing useful\n").run_agent(req).status == "FAILED"
    ok = _events({"content": [{"type": "text", "text": "x"}], "usage": _usage(0), "stopReason": "stop"})
    assert _fake_pi(tmp_path, ok, code=2).run_agent(req).status == "FAILED"


# ----------------------------------------------------- shim, driven by Node
DRIVER_JS = """
const mod = await import(process.argv[2]);
const [cwd] = process.argv.slice(3);
const reg = {}, sent = [], notes = [];
const handlers = new Proxy(reg, { get: (t, k) => t[k] ?? (async () => undefined) });
const pi = { on: (n, f) => { reg[n] = f; }, sendUserMessage: (t, o) => sent.push(t) };
mod.default(pi);
const ctx = { cwd, hasUI: true, ui: { notify: (m) => notes.push(m) }, isIdle: () => true,
              sessionManager: { getSessionId: () => "s1" } };
const out = {};
await handlers.session_start({ reason: "startup" }, ctx);
out.sysprompt = (await handlers.before_agent_start({ systemPrompt: "BASE" }, ctx))?.systemPrompt ?? null;
out.input = await handlers.input({ text: "make app.txt say ok", source: "interactive" }, ctx);
out.read = await handlers.tool_call({ toolName: "read", input: { path: "app.txt" } }, ctx);
out.edit = await handlers.tool_call({ toolName: "write", input: { path: "app.txt", content: "x" } }, ctx);
const stage = async (label, msgs) => {
  await handlers.agent_end({ messages: msgs ?? [{ role: "assistant", stopReason: "stop" }] }, ctx);
  await new Promise((r) => setTimeout(r, 400));
  out[label] = { sent: sent.splice(0), notes: notes.splice(0) };
};
await stage("aborted", [{ role: "assistant", stopReason: "aborted" }]);
await stage("blocked");
process.stdout.write(JSON.stringify({ out, notes0: notes }));
"""


def _node_can_run_ts() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    r = subprocess.run([node, "--experimental-strip-types", "-e", "0"], capture_output=True, text=True, check=False)
    return r.returncode == 0


needs_node = pytest.mark.skipif(not _node_can_run_ts(), reason="node with --experimental-strip-types is required")


def _daedalus_on_path(tmp_path: Path) -> dict[str, str]:
    """A `daedalus` executable that runs this checkout, and an environment where it is found."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != AGENT_MARKER}
    if os.name == "nt":
        # The extension runs `daedalus` through cmd.exe: use the .exe pip installed for this checkout.
        installed = shutil.which("daedalus")
        scripts = Path(sysconfig.get_path("scripts"))
        if installed is None or Path(installed).parent.resolve() != scripts.resolve():
            pytest.skip("on Windows this needs this checkout installed in the running interpreter (pip install -e .)")
        env["PATH"] = f"{scripts}{os.pathsep}{env['PATH']}"
        return env
    exe = bindir / "daedalus"
    exe.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m daedalus "$@"\n')
    exe.chmod(0o755)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return env


def _drive(tmp_path: Path, repo: Path, env: dict[str, str], ext: Path | None = None) -> dict:
    pi_harness.install(repo)
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER_JS)
    ext = ext or repo / ".pi" / "extensions" / "daedalus-gate.ts"
    r = subprocess.run(["node", "--no-warnings", "--experimental-strip-types", str(driver), ext.as_uri(), str(repo)],
                       capture_output=True, text=True, env=env, cwd=repo, timeout=180, check=False)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_shim_end_to_end_blocks_then_accepts(tmp_path, repo):
    env = _daedalus_on_path(tmp_path)
    res = _drive(tmp_path, repo, env)
    out = res["out"]
    assert "acceptance gate" in out["sysprompt"] and out["sysprompt"].startswith("BASE")  # session-start context
    assert out["input"] == {"action": "continue"} and out.get("read") is None and out.get("edit") is None  # never blocks
    # an aborted turn is not a finish attempt (the only note so far is the first edit opening a run)
    assert out["aborted"]["sent"] == [] and len(out["aborted"]["notes"]) == 1
    assert "opened run" in out["aborted"]["notes"][0]
    sent = out["blocked"]["sent"]
    assert len(sent) == 1 and "Daedalus gate" in sent[0] and "tests" in sent[0]
    # the agent fixes it; the next agent_end accepts
    fix(repo)
    driver = tmp_path / "driver2.mjs"
    driver.write_text(DRIVER_JS.replace('await stage("blocked");', 'await stage("accepted");'))
    r = subprocess.run(["node", "--no-warnings", "--experimental-strip-types", str(driver),
                        (repo / ".pi" / "extensions" / "daedalus-gate.ts").as_uri(), str(repo)],
                       capture_output=True, text=True, env=env, cwd=repo, timeout=180, check=False)
    assert r.returncode == 0, r.stderr
    accepted = json.loads(r.stdout)["out"]["accepted"]
    assert accepted["sent"] == [] and any("ACCEPTED" in n for n in accepted["notes"])


@needs_node
def test_shim_is_a_no_op_without_daedalus_and_inside_launched_sessions(tmp_path, repo):
    env = {k: v for k, v in os.environ.items() if k != AGENT_MARKER}
    empty = tmp_path / "empty"
    empty.mkdir()
    env["PATH"] = str(Path(shutil.which("node")).parent) + os.pathsep + str(empty)  # node, but no daedalus
    res = _drive(tmp_path, repo, env)["out"]
    assert res["input"] == {"action": "continue"} and res["sysprompt"] is None
    assert res["blocked"] == {"sent": [], "notes": []} and res["aborted"]["sent"] == []
    assert "not installed" in res["aborted"]["notes"][0]  # the one thing it says: how to install
    env3 = dict(os.environ, **{AGENT_MARKER: "1"})
    res = _drive(tmp_path, repo, env3)["out"]
    assert res["sysprompt"] is None and res["blocked"] == {"sent": [], "notes": []}


def test_install_leaves_git_hooks_to_the_cli(tmp_path):
    from conftest import make_repo

    root = make_repo(tmp_path / "r")
    pi_harness.install(root)
    assert not (root / ".git" / "hooks" / "prepare-commit-msg").exists()
