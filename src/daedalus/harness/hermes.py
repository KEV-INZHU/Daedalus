"""Hermes Agent (Nous Research) integration: the gate as a Hermes plugin.

Hermes has Python plugins (`register(ctx)` in `__init__.py` + `plugin.yaml`) whose hooks can
steer the agent. The shipped shim (src/daedalus/data/harness/hermes/) translates:

- **pre_llm_call** (every turn)  -> `daedalus hook prompt`; on a session's first turn also
  `session-start`, whose context is injected through pre_llm_call's `{"context": ...}`.
- **pre_tool_call** (write_file, patch) -> `daedalus hook pre-edit`. Never blocks the edit.
- **pre_verify** (the agent edited code and is about to finish) -> `daedalus hook stop`.
  `{"decision": "block", "reason"}` becomes `{"action": "continue", "message": reason}`: Hermes
  appends it as a synthetic user turn and the agent keeps working.

What degrades, and why:
- Project-local plugins are NOT loaded: Hermes runs every session in one gateway that discovers
  plugins only from `~/.hermes/plugins/` (or pip entry points), and plugins are opt-in
  (`plugins.enabled`). So `install` writes the plugin into the repo (`.hermes/plugins/daedalus/`,
  committable) and prints the one command that links it into `~/.hermes` and enables it. It never
  writes outside the repository itself.
- pre_verify fires only when Hermes saw a file-edit tool land (write_file/patch), at most
  `agent.max_verify_nudges` (default 3) times per turn, and Hermes abandons a callback after
  `plugins.hook_callback_timeout` (default 30s, max 600). The shim waits only that long; a later
  verdict is delivered with the next turn. Raise the timeout for slow gates (printed by install).
  Shell-only edits are not seen by pre_verify.
- Hermes sessions run in a shared gateway, so the DAEDALUS_AGENT env marker does not reach
  plugins; Daedalus-launched sessions are marked with `--source daedalus` instead (the preset).

Sources (Hermes repo docs, website/docs/user-guide/features/):
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/plugins.md
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/context-files.md
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md
  https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/plugins.py (VALID_HOOKS, pre_verify)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from daedalus.core.errors import DaedalusError
from daedalus.plugins import HarnessIntegration

PLUGIN_NAME = "daedalus"
PLUGIN_DIR = Path(".hermes") / "plugins" / PLUGIN_NAME
SHIM_DIR = Path(__file__).resolve().parent.parent / "data" / "harness" / "hermes"
SHIM_FILES = ("plugin.yaml", "__init__.py")
MARK = "daedalus-gate: installed by `daedalus init`"  # a comment line in each shipped file


def enable_command(root: Path) -> str:
    """The one command that links the repo's plugin into ~/.hermes and enables it."""
    src = (root / PLUGIN_DIR).resolve()
    return (
        f'mkdir -p ~/.hermes/plugins && ln -sfn "{src}" ~/.hermes/plugins/{PLUGIN_NAME} '
        f"&& hermes plugins enable {PLUGIN_NAME}"
    )


def install(root: Path) -> list[str]:
    """Write the plugin into .hermes/plugins/daedalus/ (idempotent; re-install updates in place).
    Nothing outside the repository is touched, and a same-named file that is not ours is refused."""
    done: list[str] = []
    dest = root / PLUGIN_DIR
    for name in SHIM_FILES:
        target = dest / name
        if target.exists() and MARK not in target.read_text(encoding="utf-8", errors="replace"):
            raise DaedalusError(f"{target} exists and is not Daedalus's; move it away to install the Hermes plugin")
    dest.mkdir(parents=True, exist_ok=True)
    for name in SHIM_FILES:
        target = dest / name
        data = (SHIM_DIR / name).read_bytes()
        if not target.exists() or target.read_bytes() != data:
            shutil.copyfile(SHIM_DIR / name, target)
        done.append(f"plugin file {(PLUGIN_DIR / name).as_posix()}")
    done.append(
        "Hermes loads plugins only from ~/.hermes/plugins (not from a project) and only once enabled. "
        f"Run once:\n    {enable_command(root)}"
    )
    done.append(
        "For gates slower than 30s, also let Hermes wait for them: "
        "hermes config set plugins.hook_callback_timeout 600"
    )
    return done


def detect(root: Path) -> bool:
    return (root / ".hermes").is_dir() or (root / ".hermes.md").exists() or shutil.which("hermes") is not None


INTEGRATION = HarnessIntegration(
    name="hermes",
    description="Hermes Agent (Nous Research): a plugin using pre_llm_call, pre_tool_call and pre_verify",
    install=install,
    detect=detect,
    adapter_preset="hermes",
)
