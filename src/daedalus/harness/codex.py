"""OpenAI Codex CLI shim: the gate as an ambient mode of Codex.

Codex has a hooks system that mirrors Claude Code's: same event names, the same
JSON on stdin (`session_id`, `cwd`, `hook_event_name`, `prompt`, `tool_name`,
`tool_input`, ...) and the same stdout contract (`{"decision": "block", "reason": ...}`
on Stop and UserPromptSubmit, `hookSpecificOutput.additionalContext` on
SessionStart). So no translating shim is needed: the committed config calls
`daedalus hook <name>` directly, exactly like `.claude/settings.json` does, and
the handlers in `claude_code.py` answer both harnesses.

Sources (checked 2026-10 against codex-cli 0.163.0, installed and exercised
against a mock Responses server, see docs/harnesses/codex.md):
- https://developers.openai.com/codex/hooks (hooks reference)
- https://developers.openai.com/codex/guides/agents-md (AGENTS.md)
- https://developers.openai.com/codex/noninteractive (`codex exec`, `--json`)
- https://developers.openai.com/codex/security (sandbox modes)
- https://github.com/openai/codex (source; `codex exec --help`, `codex features list`)

Per lifecycle event:
- **UserPromptSubmit** -> `daedalus hook prompt`. Enforced.
- **PreToolUse** (matcher `apply_patch|Edit|Write`; Codex's file-edit tool is
  `apply_patch`, and `Edit`/`Write` are matcher aliases) -> `daedalus hook pre-edit`.
  Enforced for apply_patch. Edits made through shell commands do not fire it; the
  Stop hook notices those by comparing the tree with the prompt snapshot.
- **Stop** -> `daedalus hook stop`. `{"decision": "block", "reason": ...}` makes
  Codex continue the turn with `reason` as a new prompt (verified: the reason
  reaches the model as a hook prompt and the turn goes on). Enforced.
- **SessionStart** -> `daedalus hook session-start`; `additionalContext` is added
  to the model's context as a developer message (verified). Enforced.
- **AGENTS.md**: a managed block (the daedalus-gate instructions) is merged into
  the repository's AGENTS.md, so the agent knows the loop even where hooks are off.

Trust (important): hooks are on by default (`[features] hooks`), but a PROJECT's
`.codex/hooks.json` loads only when (1) the project is trusted
(`[projects."<path>"] trust_level = "trusted"` in ~/.codex/config.toml, set by
the trust prompt) and (2) each command hook has been reviewed in the TUI's
`/hooks` screen; the approval is a hash of the hook definition kept in the user's
config (`hooks.state."<key>".trusted_hash`), so editing a hook, including
re-running `daedalus init` with a changed command, asks for review again.
Unreviewed hooks are skipped silently by `codex exec` ("hooks need review"). Nothing
a repository commits can pre-approve itself. `codex exec --dangerously-bypass-hook-trust`
exists for vetted automation. Until then Codex sessions are NOT gated by hooks;
`daedalus doctor`/`status` still work and the git provenance hook still stamps commits.

Hooks do nothing inside sessions Daedalus launched itself (`DAEDALUS_AGENT`),
and fail open on internal errors but never into acceptance (see claude_code.py).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from daedalus.core.errors import DaedalusError
from daedalus.harness import claude_code
from daedalus.harness.claude_code import STOP_HOOK_TIMEOUT_S, _hook_command, _ours
from daedalus.orchestration.prompts import ROLE_SKILLS, SKILLS_DIR
from daedalus.plugins import HarnessIntegration

CODEX_DIR = ".codex"
HOOKS_FILE = "hooks.json"
# Codex's edit tool is `apply_patch`; `Edit` and `Write` are accepted matcher aliases.
EDIT_MATCHER = "apply_patch|Edit|Write"
BEGIN = "<!-- daedalus:begin (managed by `daedalus init`; edit outside this block) -->"
END = "<!-- daedalus:end -->"
_BLOCK = re.compile(re.escape(BEGIN.split(" (")[0]) + r".*?" + re.escape(END) + r"\n?", re.DOTALL)


def _gate_instructions() -> str:
    text = (SKILLS_DIR / ROLE_SKILLS["gate"] / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):  # drop the skill front matter; AGENTS.md has none
        text = text.split("---", 2)[2]
    return text.strip()


def _agents_block() -> str:
    return f"{BEGIN}\n{_gate_instructions()}\n{END}\n"


def _check_markers(path: Path, text: str) -> None:
    """Refuse a file whose managed block is half there (a BEGIN without its END, or the reverse)."""
    begins, ends = text.count(BEGIN.split(" (")[0]), text.count(END)
    if begins != ends:
        raise DaedalusError(
            f"{path} has {begins} Daedalus BEGIN and {ends} END marker(s); repair or delete the managed block "
            "by hand before installing"
        )


def _merge_agents_md(path: Path) -> str:
    """Insert or replace our managed block, leaving everything else as it was."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _check_markers(path, existing)
    block = _agents_block()
    if _BLOCK.search(existing):
        updated = _BLOCK.sub(lambda _m: block, existing, count=1)
    else:
        updated = existing + ("" if not existing or existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n") + block
    if updated != existing:
        path.write_text(updated, encoding="utf-8")
    return "AGENTS.md"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DaedalusError(f"{path} is not valid JSON; fix it before installing") from exc
    if not isinstance(doc, dict):
        raise DaedalusError(f"{path} must be a JSON object")
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict):
        raise DaedalusError(f"{path}: \"hooks\" must be an object mapping event names to lists of groups")
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not all(
            isinstance(g, dict) and isinstance(g.get("hooks", []), list) and all(isinstance(h, dict) for h in g.get("hooks", []))
            for g in groups
        ):
            raise DaedalusError(f"{path}: hooks.{event} must be a list of groups, each an object with a \"hooks\" list of objects")
    return doc


def install(root: Path) -> list[str]:
    """Merge Daedalus hooks into .codex/hooks.json and the gate instructions into
    AGENTS.md. Idempotent; the result is meant to be committed."""
    done: list[str] = []
    path = root / CODEX_DIR / HOOKS_FILE
    doc = _load(path)
    hooks = doc.setdefault("hooks", {})
    _check_markers(root / "AGENTS.md", (root / "AGENTS.md").read_text(encoding="utf-8") if (root / "AGENTS.md").exists() else "")
    bootstrap = claude_code._harness(root).get("bootstrap")
    bootstrap = bootstrap if isinstance(bootstrap, str) and bootstrap.strip() else None
    wanted: dict[str, dict[str, Any]] = {
        "Stop": {"hooks": [{"type": "command", "command": _hook_command("stop"), "timeout": STOP_HOOK_TIMEOUT_S,
                            "statusMessage": "Daedalus gate"}]},
        "SessionStart": {"hooks": [{"type": "command", "command": _hook_command("session-start", bootstrap)}]},
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": _hook_command("prompt")}]},
        "PreToolUse": {"matcher": EDIT_MATCHER, "hooks": [{"type": "command", "command": _hook_command("pre-edit")}]},
    }
    for event, group in wanted.items():
        groups = hooks.setdefault(event, [])
        for g in groups:  # drop older Daedalus hooks so re-install updates them
            g["hooks"] = [h for h in g.get("hooks", []) if not _ours(h)]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append(group)
        done.append(f"hook {event}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    cfg = root / CODEX_DIR / "config.toml"
    if cfg.exists() and re.search(r"^\s*\[+hooks[.\]]", cfg.read_text(encoding="utf-8"), re.MULTILINE):
        done.append("warning: .codex/config.toml also defines [hooks]; Codex loads both layers and warns")
    done.append(f"instructions -> {_merge_agents_md(root / 'AGENTS.md')}")
    done.append(
        "note: Codex runs project hooks only in a trusted project whose hooks were reviewed in /hooks "
        "(or with --dangerously-bypass-hook-trust); see docs/harnesses/codex.md"
    )
    return done


def uninstall(root: Path) -> list[str]:
    done: list[str] = []
    path = root / CODEX_DIR / HOOKS_FILE
    agents = root / "AGENTS.md"
    if agents.exists():  # refuse before changing anything
        _check_markers(agents, agents.read_text(encoding="utf-8"))
    if path.exists():
        doc = _load(path)
        hooks = doc.get("hooks") or {}
        for event in list(hooks):
            for g in hooks[event]:
                g["hooks"] = [h for h in g.get("hooks", []) if not _ours(h)]
            hooks[event] = [g for g in hooks[event] if g.get("hooks")]
            if not hooks[event]:
                del hooks[event]
        if hooks:
            path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        else:
            path.unlink()
        done.append(f"removed Daedalus hooks from {CODEX_DIR}/{HOOKS_FILE}")
    if agents.exists() and _BLOCK.search(agents.read_text(encoding="utf-8")):
        text = _BLOCK.sub("", agents.read_text(encoding="utf-8"), count=1).rstrip()
        if text:
            agents.write_text(text + "\n", encoding="utf-8")
        else:
            agents.unlink()
        done.append("removed Daedalus block from AGENTS.md")
    return done


def detect(root: Path) -> bool:
    return (root / CODEX_DIR).is_dir() or shutil.which("codex") is not None


INTEGRATION = HarnessIntegration(
    name="codex",
    description="OpenAI Codex CLI: hooks.json (Stop/PreToolUse/SessionStart/UserPromptSubmit) + AGENTS.md",
    install=install,
    detect=detect,
    adapter_preset="codex",
    uninstall=uninstall,
)
