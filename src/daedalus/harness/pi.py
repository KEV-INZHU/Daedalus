"""Pi shim (https://github.com/badlogic/pi-mono, `@mariozechner/pi-coding-agent`, CLI `pi`).

Pi has no MCP and no settings-file hooks; its extension point is a TypeScript
extension, auto-discovered from `.pi/extensions/*.ts` (project) or
`~/.pi/agent/extensions/` (global) and loaded without a build step. `install` copies
a small shipped extension (data/harness/pi/daedalus-gate.ts) to
`.pi/extensions/daedalus-gate.ts`; it shells out to `daedalus hook <name>` with the
same payloads Claude Code sends, so the gate logic lives in one place.

Event mapping (all verified against @mariozechner/pi-coding-agent 0.73.1 docs and dist):

| gate step   | Pi event                                  | enforced? |
|-------------|-------------------------------------------|-----------|
| prompt      | `input` (source != "extension")           | yes       |
| pre-edit    | `tool_call` for `edit` / `write`          | yes (never blocks the edit) |
| stop        | `agent_end`                               | partly: see below |
| session-start | `session_start` + `before_agent_start`  | yes: context appended to the system prompt each turn |

Pi has no way to veto the end of a turn (`agent_end` is notification-only), so the
Claude `decision: block` is degraded to the closest thing: once the run is idle, the
gate's reason is sent as a new user message (`pi.sendUserMessage`), which starts a
fresh turn. That works in interactive and RPC mode. In print/json mode the process
exits after the turn, so the reason (and the gate's summary) goes to stderr instead
and the agent is not sent back. Daedalus-launched agents are never gated this way
anyway (DAEDALUS_AGENT), so only a person using `pi -p` directly sees that limit.
`agent_end` handlers are awaited by Pi, so a long stop hook (tests, reviews) keeps the
agent busy rather than being cut off.

Docs:
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
  (locations, events, `pi.sendUserMessage`, `pi.sendMessage`, tool_call block semantics, modes)
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md (`.pi/skills/`)
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md (CLI: `-p`,
  `--mode json`, `--no-session`, `--tools`, `--no-extensions`; "No MCP")
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/json.md (event stream)
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

from daedalus.core.config import load_config
from daedalus.core.errors import DaedalusError
from daedalus.orchestration.prompts import ROLE_SKILLS, SKILLS_DIR
from daedalus.plugins import HarnessIntegration

EXTENSION_NAME = "daedalus-gate.ts"
MARK = "// daedalus-gate: installed by `daedalus init`"
BOOTSTRAP_LINE = 'const BOOTSTRAP: string = ""; // @bootstrap'


def _pi_dir(root: Path) -> Path:
    return root / ".pi"


def extension_source(bootstrap: str | None = None) -> str:
    """The shipped extension, with `harness.bootstrap` from .daedalus.yml (if any) baked in."""
    text = (resources.files("daedalus") / "data" / "harness" / "pi" / EXTENSION_NAME).read_text(encoding="utf-8")
    if bootstrap:
        import json

        text = text.replace(BOOTSTRAP_LINE, f"const BOOTSTRAP: string = {json.dumps(bootstrap)}; // @bootstrap", 1)
    return text


def _bootstrap(root: Path) -> str | None:
    try:
        value = load_config(root).harness.get("bootstrap")
    except DaedalusError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def install(root: Path, *, skills: tuple[str, ...] = ("gate",)) -> list[str]:
    """Write .pi/extensions/daedalus-gate.ts and the gate skill. Idempotent; commit the result.
    Only ever touches files of its own name: other extensions and settings are left alone.
    The git provenance hooks are installed once by the CLI, not here."""
    done: list[str] = []
    dest = _pi_dir(root) / "extensions" / EXTENSION_NAME
    if dest.exists() and MARK not in dest.read_text(encoding="utf-8", errors="replace"):
        raise DaedalusError(f"{dest} exists and is not Daedalus's; move it away to install the Pi extension")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(extension_source(_bootstrap(root)), encoding="utf-8")
    done.append(f"extension {dest.relative_to(root).as_posix()} (events: input, tool_call, agent_end, session_start)")
    for role in skills:
        name = ROLE_SKILLS[role]
        sdir = _pi_dir(root) / "skills" / name
        sdir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILLS_DIR / name / "SKILL.md", sdir / "SKILL.md")
        done.append(f"skill {name} -> {sdir.relative_to(root).as_posix()}")
    return done


def uninstall(root: Path) -> list[str]:
    done: list[str] = []
    ext = _pi_dir(root) / "extensions" / EXTENSION_NAME
    if ext.exists() and MARK in ext.read_text(encoding="utf-8", errors="replace"):
        ext.unlink()
        done.append(f"removed {ext.relative_to(root).as_posix()}")
    return done


def detect(root: Path) -> bool:
    return _pi_dir(root).is_dir() or shutil.which("pi") is not None


INTEGRATION = HarnessIntegration(
    name="pi",
    description="Pi (pi-mono coding agent): TypeScript extension in .pi/extensions that calls `daedalus hook`",
    install=install,
    detect=detect,
    adapter_preset="pi",
    uninstall=uninstall,
)
