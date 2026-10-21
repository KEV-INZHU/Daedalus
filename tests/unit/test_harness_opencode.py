"""The OpenCode integration: install, the shipped plugin, the read-only launcher and the preset."""

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
from daedalus.adapters.command import PRESETS, CommandAdapter
from daedalus.harness import opencode

SRC = Path(opencode.__file__).resolve().parents[2]
NODE = shutil.which("node")


# -------------------------------------------------------------------- install
def test_integration_is_registered_with_its_preset():
    i = opencode.INTEGRATION
    assert (i.name, i.adapter_preset) == ("opencode", "opencode")
    assert "opencode" in PRESETS and i.uninstall is opencode.uninstall


def test_install_writes_plugin_and_skill_and_is_idempotent(tmp_path):
    first = opencode.install(tmp_path)
    plugin = tmp_path / ".opencode" / "plugins" / "daedalus-gate.js"
    skill = tmp_path / ".opencode" / "skills" / "daedalus-gate" / "SKILL.md"
    assert plugin.read_bytes() == opencode.PLUGIN_SOURCE.read_bytes() and skill.exists()
    assert any("plugins/daedalus-gate.js" in line for line in first)
    second = opencode.install(tmp_path)
    assert any("up to date" in line for line in second)
    assert plugin.read_bytes() == opencode.PLUGIN_SOURCE.read_bytes()


def test_reinstall_updates_a_stale_plugin_in_place(tmp_path):
    opencode.install(tmp_path)
    plugin = tmp_path / ".opencode" / "plugins" / "daedalus-gate.js"
    plugin.write_text(f"{opencode.MARK}\n// an older shim\n")
    opencode.install(tmp_path)
    assert plugin.read_bytes() == opencode.PLUGIN_SOURCE.read_bytes()


def test_install_leaves_user_config_and_plugins_alone(tmp_path):
    cfg = {"model": "anthropic/claude", "plugin": ["some-npm-plugin"], "permission": {"bash": "ask"}}
    (tmp_path / "opencode.json").write_text(json.dumps(cfg))
    (tmp_path / ".opencode" / "plugins").mkdir(parents=True)
    mine = tmp_path / ".opencode" / "plugins" / "mine.ts"
    mine.write_text("export const Mine = async () => ({})\n")
    opencode.install(tmp_path)
    assert json.loads((tmp_path / "opencode.json").read_text()) == cfg
    assert mine.read_text() == "export const Mine = async () => ({})\n"


def test_install_removes_a_duplicate_in_the_singular_plugin_dir(tmp_path):
    old = tmp_path / ".opencode" / "plugin"
    old.mkdir(parents=True)
    (old / "daedalus-gate.js").write_text(f"{opencode.MARK}\n// old\n")
    opencode.install(tmp_path)
    assert not (old / "daedalus-gate.js").exists()


def test_skill_is_not_duplicated_when_claude_skills_already_has_it(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "daedalus-gate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    done = opencode.install(tmp_path)
    assert not (tmp_path / ".opencode" / "skills").exists()
    assert any("already provided" in line for line in done)


def test_uninstall_removes_only_ours(tmp_path):
    opencode.install(tmp_path)
    mine = tmp_path / ".opencode" / "plugins" / "mine.ts"
    mine.write_text("x")
    opencode.uninstall(tmp_path)
    assert not (tmp_path / ".opencode" / "plugins" / "daedalus-gate.js").exists()
    assert mine.exists()
    assert not (tmp_path / ".opencode" / "skills").exists()


def test_detect(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert not opencode.detect(tmp_path)
    (tmp_path / ".opencode").mkdir()
    assert opencode.detect(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "opencode.json").write_text("{}")
    assert opencode.detect(other)
    third = tmp_path / "third"
    third.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "opencode").write_text("#!/bin/sh\n")
    (binary / "opencode").chmod(0o755)
    monkeypatch.setenv("PATH", str(binary))
    assert opencode.detect(third)


# ------------------------------------------------------------ the shipped shim
def test_shipped_plugin_calls_every_hook_and_is_safe():
    js = opencode.PLUGIN_SOURCE.read_text()
    for hook in ("prompt", "pre-edit", "stop", "session-start"):
        assert f'"{hook}"' in js
    for event in ("chat.message", "tool.execute.before", "session.idle", "experimental.chat.system.transform"):
        assert event in js
    assert "client.session.prompt" in js
    assert "process.env.DAEDALUS_AGENT" in js  # launched sessions are not gated twice
    assert "ENOENT" in js  # portable: no daedalus on PATH means no-op
    assert "export default" not in js  # OpenCode would register the same plugin twice
    assert js.count("export const") == 1
    assert 'FEEDBACK_PREFIX = "Stop hook feedback:' in js  # ignored as a user request by the prompt hook


def test_plugin_is_package_data():
    assert 'data/harness/*/*' in (SRC.parent / "pyproject.toml").read_text()


DRIVER = r"""
import { pathToFileURL } from "node:url"
const mod = await import(pathToFileURL(process.env.PLUGIN).href)
const calls = { prompts: [], toasts: [] }
const client = {
  session: {
    get: async () => ({ data: {} }),
    prompt: async (a) => { calls.prompts.push(a) },
  },
  tui: { showToast: async (a) => { calls.toasts.push(a.body.message) } },
}
const hooks = await mod.DaedalusGate({ client, directory: process.env.REPO })
const steps = JSON.parse(process.env.STEPS)
const out = []
for (const s of steps) {
  const before = { p: calls.prompts.length, t: calls.toasts.length }
  if (s.do === "message") await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: s.text }] })
  if (s.do === "edit") await hooks["tool.execute.before"]({ tool: s.tool, sessionID: "s1" }, { args: { filePath: s.path } })
  if (s.do === "idle") await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  if (s.do === "system") { const o = { system: [] }; await hooks["experimental.chat.system.transform"]({ sessionID: "s1" }, o); out.push({ system: o.system }) }
  if (s.do === "fix") await import("node:fs").then((fs) => fs.writeFileSync(process.env.REPO + "/app.txt", "ok\n"))
  await new Promise((r) => setTimeout(r, 50)) // let any un-awaited prompt call land
  out.push({ step: s.do, prompts: calls.prompts.slice(before.p), toasts: calls.toasts.slice(before.t) })
}
console.log(JSON.stringify(out))
"""


def _run_plugin(tmp_path: Path, repo: Path, steps: list[dict], *, with_daedalus: bool = True):
    plugin = tmp_path / "gate.mjs"
    plugin.write_text(opencode.PLUGIN_SOURCE.read_text())
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != AGENT_MARKER}
    node_dir = str(Path(NODE).parent)
    if with_daedalus:
        shim = bindir / "daedalus"
        shim.write_text(f'#!/bin/sh\nPYTHONPATH={SRC} exec {sys.executable} -m daedalus "$@"\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = os.pathsep.join([str(bindir), node_dir, "/usr/bin", "/bin"])
    env.update(PLUGIN=str(plugin), REPO=str(repo), STEPS=json.dumps(steps))
    done = subprocess.run([NODE, "--input-type=module", "-e", DRIVER], capture_output=True, text=True, env=env,
                          timeout=120, cwd=tmp_path, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_plugin_end_to_end_through_the_real_stop_hook(tmp_path):
    repo = make_repo(tmp_path / "repo")
    steps = [
        {"do": "system"},
        {"do": "message", "text": "make app.txt say ok"},
        {"do": "edit", "tool": "write", "path": str(repo / "app.txt")},
        {"do": "idle"},  # app.txt still says "bad": the gate blocks, the plugin sends the agent back
        {"do": "fix"},
        {"do": "idle"},  # now the check passes: accepted, shown to the user, nothing sent to the agent
    ]
    out = _run_plugin(tmp_path, repo, steps)
    assert "acceptance gate" in out[0]["system"][0]
    assert any("opened run" in t for t in out[3]["toasts"])  # the first edit opened a run
    blocked = out[4]
    assert len(blocked["prompts"]) == 1
    sent = blocked["prompts"][0]
    assert sent["path"] == {"id": "s1"}
    text = sent["body"]["parts"][0]["text"]
    assert text.startswith("Stop hook feedback:\n") and "tests" in text
    accepted = out[6]
    assert accepted["prompts"] == [] and any("ACCEPTED" in t for t in accepted["toasts"])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_plugin_does_nothing_without_daedalus_on_path(tmp_path):
    repo = make_repo(tmp_path / "repo")
    steps = [{"do": "message", "text": "p"}, {"do": "edit", "tool": "edit", "path": "app.txt"}, {"do": "idle"}]
    out = _run_plugin(tmp_path, repo, steps, with_daedalus=False)
    assert all(not s["prompts"] and not s["toasts"] for s in out)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_plugin_ignores_non_edit_tools_and_subagent_sessions(tmp_path):
    repo = make_repo(tmp_path / "repo")
    steps = [{"do": "message", "text": "p"}, {"do": "edit", "tool": "read", "path": str(repo / "app.txt")}]
    out = _run_plugin(tmp_path, repo, steps)
    assert out[1]["toasts"] == []
    assert not (repo / ".daedalus" / "runs").exists() or not list((repo / ".daedalus" / "runs").iterdir())


# --------------------------------------------------------- read-only launcher
def test_preset_review_argv_is_read_only_and_builder_argv_is_not_locked_down():
    adapter = CommandAdapter.from_config({"name": "opencode"})
    review, stdin = adapter._command(AgentRequest("r", "t", "aristotle", "the prompt", Path("."), read_only=True))
    assert opencode.READ_ONLY_FLAG in review and stdin == b"the prompt"
    for write_enabling in ("--auto", "--dangerously-skip-permissions", "--agent", "build"):
        assert write_enabling not in review
    assert review[-1] == opencode.READ_ONLY_FLAG  # nothing configurable can follow it
    build, _ = adapter._command(AgentRequest("r", "t", "brunel", "p", Path("."), read_only=False))
    assert opencode.READ_ONLY_FLAG not in build
    assert review[:6] == [sys.executable, "-m", "daedalus.harness.opencode", "run", "--format", "json"]
    assert adapter.capabilities.cost_reporting and "experimental preset" in adapter.capabilities.limitations


def test_preset_refuses_config_that_could_loosen_the_launch_line():
    from daedalus.core.errors import CapabilityError

    with pytest.raises(CapabilityError):
        CommandAdapter.from_config({"name": "opencode", "read_only_args": []})


def test_read_only_config_denies_everything_but_reading():
    cfg = json.loads(opencode.read_only_config())
    perm = cfg["permission"]
    assert perm["*"] == "deny"
    assert all(perm[t] == "allow" for t in ("read", "grep", "glob", "list"))
    assert all(perm[t] == "deny" for t in ("bash", "edit", "write", "patch", "apply_patch", "task", "webfetch"))
    assert list(perm).index("*") == 0  # the catch-all comes first so the specific rules refine it


def test_read_only_config_keeps_only_provider_selection_from_an_inline_config():
    inline = json.dumps({
        "model": "p/m", "provider": {"p": {}}, "permission": {"bash": "allow"},
        "agent": {"build": {"permission": {"edit": "allow"}}}, "plugin": ["x"], "mcp": {"s": {}},
    })
    cfg = json.loads(opencode.read_only_config(inline))
    assert cfg["model"] == "p/m" and cfg["provider"] == {"p": {}}
    assert not {"plugin", "mcp"} & set(cfg) and cfg["permission"]["bash"] == "deny"
    assert cfg["agent"]["build"] == {"permission": cfg["permission"]}  # the user's agent rules are gone
    assert json.loads(opencode.read_only_config("not json"))["permission"]["*"] == "deny"


def test_read_only_config_sets_the_same_permissions_on_every_builtin_agent():
    cfg = json.loads(opencode.read_only_config(json.dumps({"agent": {"build": {"permission": {"bash": "allow"}}}})))
    assert set(cfg["agent"]) == {"build", "plan", "general"}
    for agent in cfg["agent"].values():
        assert agent == {"permission": cfg["permission"]}
        assert agent["permission"]["*"] == "deny" and agent["permission"]["bash"] == "deny"
        assert agent["permission"]["read"] == "allow"


def test_read_only_env_locks_out_project_config_and_permission_overrides(tmp_path):
    env = opencode.read_only_env({"PATH": "/x", "OPENCODE_PERMISSION": '{"bash":"allow"}'}, tmp_path)
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and "OPENCODE_PERMISSION" not in env
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]["*"] == "deny"


def test_read_only_env_hides_the_global_config_but_keeps_auth(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text('{"agent": {"build": {"permission": {"bash": "allow"}}}}')
    cfg_home = tmp_path / "fresh"
    cfg_home.mkdir()
    env = opencode.read_only_env({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                                  "OPENCODE_CONFIG": "/user/own.json", "OPENCODE_CONFIG_DIR": "/user/dir"}, cfg_home)
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        assert Path(env[key]).resolve().is_relative_to(cfg_home.resolve()), key
    assert json.loads(Path(env["OPENCODE_CONFIG"]).read_text()) == {}
    assert list(Path(env["OPENCODE_CONFIG_DIR"]).iterdir()) == []
    assert list(Path(env["XDG_CONFIG_HOME"]).iterdir()) == []
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")  # auth.json stays reachable
    kept = opencode.read_only_env({"XDG_DATA_HOME": "/data"}, cfg_home)
    assert kept["XDG_DATA_HOME"] == "/data"


def test_pin_agent_replaces_any_agent_choice():
    assert opencode._pin_agent(["run", "--agent", "evil", "-a", "x", "--agent=y", "--format", "json"]) == [
        "run", "--format", "json", "--agent", "build"]


FAKE_OPENCODE = """#!/bin/sh
echo "$@" > "$OUT/args"
echo "${OPENCODE_DISABLE_PROJECT_CONFIG:-unset}" > "$OUT/disable"
echo "${OPENCODE_CONFIG_CONTENT:-unset}" > "$OUT/content"
pwd > "$OUT/cwd"
echo "$PWD" > "$OUT/pwdvar"
echo "$OPENCODE_CONFIG|$OPENCODE_CONFIG_DIR|$XDG_CONFIG_HOME" > "$OUT/cfgenv"
[ -n "$OPENCODE_CONFIG" ] && cat "$OPENCODE_CONFIG" > "$OUT/cfgfile"; exit 0
"""


def _fake_opencode(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "opencode").write_text(FAKE_OPENCODE)
    (bindir / "opencode").chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    return {"PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin", "OUT": str(out), "PWD": "/somewhere/else"}


def test_launch_read_only_strips_the_flag_and_locks_the_session(tmp_path, monkeypatch):
    env = _fake_opencode(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    code = opencode.launch(["run", "--format", "json", opencode.READ_ONLY_FLAG], env)
    out = Path(env["OUT"])
    assert code == 0
    assert (out / "args").read_text().strip() == "run --format json --agent build"
    assert (out / "disable").read_text().strip() == "1"
    assert json.loads((out / "content").read_text())["permission"]["bash"] == "deny"
    cfg_paths = (out / "cfgenv").read_text().strip().split("|")
    assert all("daedalus-opencode-" in p for p in cfg_paths)
    assert not Path(cfg_paths[0]).exists()  # the temporary config home is removed afterwards
    assert json.loads((out / "cfgfile").read_text()) == {}
    assert Path((out / "pwdvar").read_text().strip()).resolve() == work.resolve()  # not the parent's $PWD


def test_launch_without_the_flag_leaves_the_session_unrestricted(tmp_path):
    env = _fake_opencode(tmp_path)
    assert opencode.launch(["run", "--format", "json"], env) == 0
    out = Path(env["OUT"])
    assert (out / "disable").read_text().strip() == "unset" and (out / "content").read_text().strip() == "unset"


def test_launch_without_opencode_fails_closed(tmp_path, capsys):
    assert opencode.launch(["run", opencode.READ_ONLY_FLAG], {"PATH": str(tmp_path)}) == 127
    assert "not on PATH" in capsys.readouterr().err


def test_launcher_as_a_module_runs_the_fake_cli_end_to_end(tmp_path):
    """The exact argv the adapter builds, through a real child process."""
    env = _fake_opencode(tmp_path)
    (tmp_path / "bin" / "opencode").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' \'{"type":"text","part":{"type":"text","messageID":"m1","text":"hi"}}\' '
        '\'{"type":"step_finish","part":{"type":"step-finish","cost":0.5,"tokens":{"input":7,"output":3,"cache":{"read":1,"write":2}}}}\'\n'
    )
    adapter = CommandAdapter.from_config({"name": "opencode"})
    cmd, stdin = adapter._command(AgentRequest("r", "t", "aristotle", "p", tmp_path, read_only=True))
    done = subprocess.run(cmd, input=stdin, capture_output=True, cwd=tmp_path, check=False,
                          env={**env, "PYTHONPATH": str(SRC)})
    assert done.returncode == 0, done.stderr
    res = adapter._parse(done.stdout.decode(), "", 0, "sess")
    assert res.status == "COMPLETED" and res.output == "hi" and res.cost == 0.5
    assert res.usage["input_tokens"] == 7 and res.usage["cache_creation_input_tokens"] == 2


# ------------------------------------------------------------------ output parse
def _events(*evs: dict) -> str:
    return "\n".join(json.dumps(e) for e in evs)


def test_parse_run_takes_the_last_message_text_cost_and_tokens():
    text, cost, usage, err = opencode.parse_run(_events(
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"messageID": "m1", "text": "thinking aloud"}},
        {"type": "step_finish", "part": {"cost": 0.25, "tokens": {"input": 10, "output": 2, "cache": {"read": 4, "write": 0}}}},
        {"type": "text", "part": {"messageID": "m2", "text": "final "}},
        {"type": "text", "part": {"messageID": "m2", "text": "answer"}},
        {"type": "step_finish", "part": {"cost": 0.5, "tokens": {"input": 5, "output": 1, "cache": {"read": 0, "write": 3}}}},
    ))
    assert (text, cost, err) == ("final \nanswer", 0.75, None)
    assert usage["input_tokens"] == 15 and usage["output_tokens"] == 3 and usage["num_turns"] == 2
    assert usage["cache_read_input_tokens"] == 4 and usage["cache_creation_input_tokens"] == 3


def test_parse_run_reports_errors_and_survives_garbage():
    _, _, _, err = opencode.parse_run(_events({"type": "error", "error": {"name": "APIError", "data": {"message": "Forbidden"}}}))
    assert err == "Forbidden"
    text, cost, usage, err = opencode.parse_run("not json\n[1]\n{\"type\": \"step_finish\", \"part\": {\"cost\": -1, \"tokens\": 5}}")
    assert (text, cost, err) == ("", 0.0, None) and usage["input_tokens"] == 0


def test_adapter_marks_an_opencode_error_event_as_failed(tmp_path):
    adapter = CommandAdapter.from_config({"name": "opencode"})
    out = _events({"type": "error", "error": {"name": "APIError", "data": {"message": "no key"}}})
    res = adapter._parse(out, "", 0, "s")
    assert res.status == "FAILED" and "no key" in res.error
    assert adapter._parse("", "boom", 1, "s").status == "FAILED"


# ------------------------------------------------------------- ownership (F12)
def test_install_refuses_a_foreign_plugin_of_the_same_name(tmp_path):
    from daedalus.core.errors import DaedalusError

    plugin = tmp_path / ".opencode" / "plugins" / "daedalus-gate.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// the user's own plugin\n")
    with pytest.raises(DaedalusError, match="not Daedalus's"):
        opencode.install(tmp_path)
    assert plugin.read_text() == "// the user's own plugin\n"
    plugin.unlink()
    old = tmp_path / ".opencode" / "plugin" / "daedalus-gate.js"
    old.parent.mkdir(parents=True)
    old.write_text("// theirs\n")
    with pytest.raises(DaedalusError):
        opencode.install(tmp_path)
    assert old.exists()


def test_uninstall_refuses_to_delete_a_foreign_file(tmp_path):
    from daedalus.core.errors import DaedalusError

    plugin = tmp_path / ".opencode" / "plugins" / "daedalus-gate.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// the user's own plugin\n")
    with pytest.raises(DaedalusError):
        opencode.uninstall(tmp_path)
    assert plugin.exists()


def test_shipped_plugin_carries_the_ownership_marker():
    assert opencode.MARK in opencode.PLUGIN_SOURCE.read_text(encoding="utf-8")
