"""OpenCode (sst/opencode) shim: the gate as an ambient mode of the harness.

OpenCode has no shell-command hooks; it has JS/TS plugins that run inside its process and
receive lifecycle callbacks. `install` copies one small plugin (package data,
`daedalus/data/harness/opencode/daedalus-gate.js`) to `.opencode/plugins/`, where OpenCode loads
every `*.js`/`*.ts` file at startup, with nothing to register in `opencode.json`. The plugin
translates OpenCode events into the payloads of `daedalus hook <name>` and the answers back:

- ``chat.message`` (a user message was received)        -> ``prompt``
- ``tool.execute.before`` (edit/write/patch/multiedit)  -> ``pre-edit``; never blocks the edit
- ``event`` ``session.idle`` (the agent finished a turn) -> ``stop``. ``{"decision": "block"}`` is
  honoured by sending the reason back with ``client.session.prompt`` as a new user message, so the
  agent continues. OpenCode has no way to veto going idle, so this is the nearest equivalent; the
  gate itself caps how many times it answers "block", so there is no endless loop. A non-block
  answer's ``systemMessage`` is shown as a toast.
- ``experimental.chat.system.transform``                -> ``session-start``; its context is
  appended to the system prompt, once per session.

Degradations, all deliberate and documented in docs/harnesses/opencode.md: the stop gate runs
after the turn ended rather than preventing it; user interrupts (Esc) and subagent sessions are
not gated; ``opencode run`` (headless, one message) may exit before the plugin can continue the
agent, so there the gate is advisory and ``daedalus finish`` is authoritative; toasts only exist
in the TUI/web UI.

Sources (checked against opencode 1.19.3 and its published `@opencode-ai/plugin` types):
- plugins and hooks: https://opencode.ai/docs/plugins/
- config and its precedence: https://opencode.ai/docs/config/
- permissions: https://opencode.ai/docs/permissions/
- agents: https://opencode.ai/docs/agents/
- CLI (`opencode run` flags): https://opencode.ai/docs/cli/
- skills (`.opencode/skills`, also reads `.claude/skills`): https://opencode.ai/docs/skills/
- SDK (`client.session.prompt`, `tui.showToast`): https://opencode.ai/docs/sdk/
- plugin types: https://github.com/sst/opencode/blob/dev/packages/plugin/src/index.ts

Reviewers (`opencode run`) are made read-only by `python -m daedalus.harness.opencode`, the
launcher the `opencode` adapter preset runs: see `launch`.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from daedalus.core.errors import DaedalusError
from daedalus.orchestration.prompts import ROLE_SKILLS, SKILLS_DIR
from daedalus.plugins import HarnessIntegration

PLUGIN_NAME = "daedalus-gate.js"
PLUGIN_SOURCE = Path(__file__).resolve().parent.parent / "data" / "harness" / "opencode" / PLUGIN_NAME
READ_ONLY_FLAG = "--daedalus-read-only"
SKILL = ROLE_SKILLS["gate"]
MARK = "// daedalus-gate: installed by `daedalus init`"  # first line of the shipped plugin; proves it is ours


# -------------------------------------------------------------------- install
def _plugin_path(root: Path) -> Path:
    return root / ".opencode" / "plugins" / PLUGIN_NAME


def _skill_path(root: Path) -> Path:
    return root / ".opencode" / "skills" / SKILL / "SKILL.md"


def _ours(path: Path) -> bool:
    return MARK in path.read_text(encoding="utf-8", errors="replace")


def install(root: Path) -> list[str]:
    """Copy the plugin (and the gate skill) into the repository. Idempotent: a re-install
    overwrites our own file in place and touches nothing else under `.opencode/`. The result is
    meant to be committed so everyone who opens the repository in OpenCode gets the gate."""
    done: list[str] = []
    dest = _plugin_path(root)
    if dest.exists() and not _ours(dest):
        raise DaedalusError(f"{dest} exists and is not Daedalus's; move it away to install the OpenCode plugin")
    old = root / ".opencode" / "plugin" / PLUGIN_NAME  # never load the shim twice
    if old.exists() and not _ours(old):
        raise DaedalusError(f"{old} exists and is not Daedalus's; move it away to install the OpenCode plugin")
    dest.parent.mkdir(parents=True, exist_ok=True)
    new = PLUGIN_SOURCE.read_bytes()
    if dest.exists() and dest.read_bytes() == new:
        done.append(f"plugin {dest.relative_to(root).as_posix()} (up to date)")
    else:
        dest.write_bytes(new)
        done.append(f"plugin -> {dest.relative_to(root).as_posix()}")
    if old.exists():
        old.unlink()
        done.append(f"removed duplicate {old.relative_to(root).as_posix()}")
    if (root / ".claude" / "skills" / SKILL / "SKILL.md").exists():
        done.append(f"skill {SKILL} (already provided by .claude/skills, which OpenCode also reads)")
    else:
        skill = _skill_path(root)
        skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILLS_DIR / SKILL / "SKILL.md", skill)
        done.append(f"skill {SKILL} -> {skill.relative_to(root).as_posix()}")
    return done


def uninstall(root: Path) -> list[str]:
    done: list[str] = []
    for path in (_plugin_path(root), root / ".opencode" / "plugin" / PLUGIN_NAME):
        if path.exists() and not _ours(path):
            raise DaedalusError(f"{path} is not Daedalus's; not removing it")
    for path in (_plugin_path(root), root / ".opencode" / "plugin" / PLUGIN_NAME, _skill_path(root)):
        if path.exists():
            path.unlink()
            done.append(f"removed {path.relative_to(root).as_posix()}")
            parent = path.parent
            while parent != root / ".opencode":  # prune what is now empty; never the user's other files
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    return done


def detect(root: Path) -> bool:
    return (
        (root / ".opencode").is_dir()
        or (root / "opencode.json").exists()
        or (root / "opencode.jsonc").exists()
        or shutil.which("opencode") is not None
    )


# ---------------------------------------------------------- read-only launcher
# What a reviewer may use. Everything else is denied by default ("*"), and the write-capable
# tools are denied by name as well, so a tool added in a later release stays denied.
READ_ONLY_ALLOW = ("read", "grep", "glob", "list")
READ_ONLY_DENY = ("bash", "edit", "write", "patch", "apply_patch", "multiedit", "task", "todowrite",
                  "webfetch", "websearch", "codesearch", "skill", "external_directory", "doom_loop")
# Built-in agents. An `agent.<name>.permission` outranks the global `permission`, so a user's global
# agent-level rules could re-grant a tool; the same map is set under each of them as well.
READ_ONLY_AGENTS = ("build", "plan", "general")
READ_ONLY_AGENT = "build"  # the agent the reviewer is pinned to (`--agent build`)
# Keys of an inline config the user may already have set that stay (how to reach their model).
_KEEP_INLINE = ("provider", "model", "small_model", "enabled_providers", "disabled_providers")


def read_only_config(existing: str | None = None) -> str:
    """The inline config (OPENCODE_CONFIG_CONTENT) for a read-only session.

    Only provider selection survives from `existing`: its agents, plugins, MCP servers and
    permissions are dropped. The permission map is also set on each built-in agent, because an
    agent-level permission outranks the global one."""
    base: dict[str, Any] = {}
    if existing:
        try:
            doc = json.loads(existing)
        except ValueError:
            doc = {}
        if isinstance(doc, dict):
            base = {k: doc[k] for k in _KEEP_INLINE if k in doc}
    permission: dict[str, str] = {"*": "deny"}
    permission.update({t: "deny" for t in READ_ONLY_DENY})
    permission.update({t: "allow" for t in READ_ONLY_ALLOW})
    base["permission"] = permission
    base["agent"] = {name: {"permission": dict(permission)} for name in READ_ONLY_AGENTS}
    return json.dumps(base)


def read_only_env(env: dict[str, str], config_home: Path) -> dict[str, str]:
    """`env` for a read-only session. `config_home` is a fresh, empty directory.

    The inline config sits above the project's `opencode.json` and `.opencode/`, but the project
    could still re-grant a tool for the default agent (an `agent.build.permission` outranks the
    global permission; observed on opencode 1.19.3). OPENCODE_DISABLE_PROJECT_CONFIG=1 makes the
    reviewer ignore the project's config, agents, plugins and commands, which a Builder session
    can write, exactly as `claude --restricted` does.

    The user's global config would otherwise still load, and its agent-level permissions could
    widen the reviewer. So OPENCODE_CONFIG, OPENCODE_CONFIG_DIR and XDG_CONFIG_HOME all point into
    `config_home` and only the inline config applies. Provider credentials normally live in
    `auth.json` under XDG_DATA_HOME, which is pinned to its current value and still read, so model
    access keeps working; API keys written into the global config file, and `~/.opencode`, are not
    seen (use the provider's environment variable or `opencode auth login`)."""
    out = dict(env)
    data_home = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "share")
    cfg_file = config_home / "opencode.json"
    cfg_file.write_text("{}\n", encoding="utf-8")
    cfg_dir = config_home / "config-dir"
    cfg_dir.mkdir(exist_ok=True)
    (config_home / "xdg-config").mkdir(exist_ok=True)
    out["OPENCODE_CONFIG"] = str(cfg_file)
    out["OPENCODE_CONFIG_DIR"] = str(cfg_dir)
    out["XDG_CONFIG_HOME"] = str(config_home / "xdg-config")
    out["XDG_DATA_HOME"] = data_home  # keep auth.json reachable
    out["OPENCODE_CONFIG_CONTENT"] = read_only_config(env.get("OPENCODE_CONFIG_CONTENT"))
    out["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    out.pop("OPENCODE_PERMISSION", None)  # a permission override from the environment must not widen it
    return out


def _pin_agent(args: list[str]) -> list[str]:
    """`args` with any --agent/-a choice replaced by `--agent build` (whose permissions are locked down)."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in ("--agent", "-a"):
            skip = True
        elif not a.startswith("--agent="):
            out.append(a)
    return [*out, "--agent", READ_ONLY_AGENT]


def launch(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Run `opencode <argv>`. With READ_ONLY_FLAG, only as a read-only session. Fails closed:
    if opencode is missing the exit status says so, and no flag combination skips the lockdown."""
    env = dict(os.environ if env is None else env)
    read_only = READ_ONLY_FLAG in argv
    args = [a for a in argv if a != READ_ONLY_FLAG]
    exe = shutil.which("opencode", path=env.get("PATH"))
    if exe is None:
        print("opencode is not on PATH: install it (https://opencode.ai) to use the opencode adapter",
              file=sys.stderr)
        return 127
    # OpenCode takes its project directory from $PWD, which a parent that only set the child's
    # cwd leaves pointing at its own directory: the session would run (and edit) in the wrong place.
    env["PWD"] = os.getcwd()
    try:
        if read_only:
            with tempfile.TemporaryDirectory(prefix="daedalus-opencode-") as config_home:
                return subprocess.run([exe, *_pin_agent(args)], env=read_only_env(env, Path(config_home)),
                                      check=False).returncode
        return subprocess.run([exe, *args], env=env, check=False).returncode
    except OSError as exc:
        print(f"could not start opencode: {exc}", file=sys.stderr)
        return 127


# ------------------------------------------------------------------ run output
def parse_run(text: str) -> tuple[str, float, dict[str, Any], str | None]:
    """(final answer, cost in USD, usage, error) from `opencode run --format json`, one event per
    line: `text` parts carry the answer, `step_finish` parts the tokens and cost, `error` events
    the failure. The answer is the text of the last assistant message that has any. Never raises."""
    texts: dict[str, list[str]] = {}
    order: list[str] = []
    cost = 0.0
    tok = {"input": 0, "output": 0, "read": 0, "write": 0}
    steps = 0
    error: str | None = None

    def num(v: Any) -> float:
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        return float(v) if ok else 0.0

    for line in text.split("\n"):  # not splitlines(): U+2028 can sit inside JSON strings
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        kind = ev.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            mid = str(part.get("messageID") or "")
            if mid not in texts:
                texts[mid] = []
                order.append(mid)
            texts[mid].append(part["text"])
        elif kind == "step_finish":
            steps += 1
            cost += num(part.get("cost"))
            t = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            c = t.get("cache") if isinstance(t.get("cache"), dict) else {}
            tok["input"] += int(num(t.get("input")))
            tok["output"] += int(num(t.get("output")))
            tok["read"] += int(num(c.get("read")))
            tok["write"] += int(num(c.get("write")))
        elif kind == "error":
            err = ev.get("error")
            data = err.get("data") if isinstance(err, dict) and isinstance(err.get("data"), dict) else {}
            error = str(data.get("message") or (err.get("name") if isinstance(err, dict) else err) or "error")
    answer = "\n".join(texts[order[-1]]) if order else ""
    usage = {
        "model": None, "models": {},
        "input_tokens": tok["input"], "output_tokens": tok["output"],
        "cache_read_input_tokens": tok["read"], "cache_creation_input_tokens": tok["write"],
        "num_turns": steps, "duration_ms": 0,
    }
    return answer, cost if math.isfinite(cost) else 0.0, usage, error


def main(argv: list[str] | None = None) -> int:
    return launch(list(sys.argv[1:] if argv is None else argv))


INTEGRATION = HarnessIntegration(
    name="opencode",
    description="OpenCode: a plugin in .opencode/plugins/ gates every session (prompt, edits, session idle)",
    install=install,
    detect=detect,
    adapter_preset="opencode",
    uninstall=uninstall,
)


if __name__ == "__main__":
    raise SystemExit(main())
