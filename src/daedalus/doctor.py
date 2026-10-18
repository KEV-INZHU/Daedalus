"""`daedalus doctor`: is this repository set up for the frictionless gate?

Each check reports what it found and, when it fails, a one-line fix. Doctor only
reads: no config is loaded through anything that writes state, git is asked with
`--no-optional-locks` so not even the index is refreshed, and no adapter is launched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from daedalus.core.config import CONFIG_FILE, RepoConfig, load_config
from daedalus.core.errors import DaedalusError

CLAUDE_SETTINGS = ".claude/settings.json"
HOOK_EVENTS = ("Stop", "SessionStart", "UserPromptSubmit", "PreToolUse")


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                              capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def _committed(root: Path, path: str) -> bool:
    """Tracked, and identical to HEAD in the index and the working tree."""
    tracked = _git(root, "ls-files", "--error-unmatch", "--", path)
    status = _git(root, "status", "--porcelain", "--", path)
    return bool(tracked and status and tracked.returncode == 0 and status.returncode == 0 and not status.stdout.strip())


def _config(root: Path) -> tuple[Finding, RepoConfig | None]:
    name = f"{CONFIG_FILE} committed and valid"
    if not (root / CONFIG_FILE).exists():
        return Finding(name, False, f"no {CONFIG_FILE}", f"run `daedalus init`, then commit {CONFIG_FILE}"), None
    try:
        cfg = load_config(root)
    except DaedalusError as exc:
        return Finding(name, False, f"invalid: {exc}", f"correct {CONFIG_FILE} (see `daedalus policy --default`)"), None
    if not _committed(root, CONFIG_FILE):
        return Finding(name, False, "valid but not committed",
                       f"git add {CONFIG_FILE} && git commit -m 'Configure Daedalus'"), cfg
    return Finding(name, True, "valid and committed"), cfg


def _mandatory(cfg: RepoConfig | None) -> Finding:
    name = "a mandatory check"
    if cfg is None:
        return Finding(name, False, f"unknown until {CONFIG_FILE} is valid", f"fix {CONFIG_FILE} first")
    mandatory = cfg.policy.mandatory_checks()
    if not mandatory:
        return Finding(name, False, "no mandatory check: nothing would prove a change works",
                       f"add one under `checks:` in {CONFIG_FILE}, e.g. `tests: python -m pytest -q`")
    return Finding(name, True, ", ".join(mandatory))


def _claude_hooks(root: Path) -> Finding:
    from daedalus.harness.claude_code import _ours

    name = "Claude Code hooks installed and committed"
    fix = "run `daedalus install claude-code`, then commit .claude/"
    path = root / CLAUDE_SETTINGS
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Finding(name, False, f"no {CLAUDE_SETTINGS}", fix)
    except (OSError, ValueError) as exc:
        return Finding(name, False, f"{CLAUDE_SETTINGS} is unreadable: {exc}",
                       f"repair {CLAUDE_SETTINGS}, then run `daedalus install claude-code`")
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    missing = [ev for ev in HOOK_EVENTS
               if not any(isinstance(h, dict) and _ours(h)
                          for g in hooks.get(ev) or [] if isinstance(g, dict) for h in g.get("hooks") or [])]
    if missing:
        return Finding(name, False, f"missing hooks: {', '.join(missing)}", fix)
    if not _committed(root, CLAUDE_SETTINGS):
        return Finding(name, False, f"{CLAUDE_SETTINGS} is not committed",
                       "git add .claude/ && git commit -m 'Install Daedalus hooks'")
    return Finding(name, True, ", ".join(HOOK_EVENTS))


def _git_hooks(root: Path) -> Finding:
    from daedalus.harness.provenance import GIT_HOOKS, hook_path

    name = "git hooks installed"
    fix = "run `daedalus install claude-code`"
    missing = []
    for hook, mark, _script, _hint in GIT_HOOKS:
        p = hook_path(root, hook)
        if p is None:
            return Finding(name, False, "not a git repository", "run `git init`, then `daedalus install claude-code`")
        try:
            ours = mark in p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ours = False
        if not ours:
            missing.append(hook)
    if missing:
        return Finding(name, False, f"missing: {', '.join(missing)}", fix)
    return Finding(name, True, ", ".join(h for h, *_ in GIT_HOOKS))


def _on_path() -> Finding:
    from daedalus.harness.claude_code import INSTALL_HINT

    name = "daedalus on PATH"
    found = shutil.which("daedalus")
    if not found:
        return Finding(name, False, "the hooks call `daedalus` and find nothing, so they do nothing",
                       f"install it where the hooks run: {INSTALL_HINT}")
    return Finding(name, True, found)


def _adapter(cfg: RepoConfig | None) -> tuple[list[Finding], object | None]:
    from daedalus.adapters import make_adapter

    name = "launching adapter for reviews"
    if cfg is None:
        return [Finding(name, False, f"unknown until {CONFIG_FILE} is valid", f"fix {CONFIG_FILE} first")], None
    try:
        adapter = make_adapter(cfg.adapter)
    except DaedalusError as exc:
        return [Finding(name, False, f"invalid adapter: {exc}", f"correct `adapter:` in {CONFIG_FILE}")], None
    caps = adapter.capabilities
    if not (caps.launch_agent and caps.structured_results and caps.identity == "launched"):
        return [Finding(name, False, f"`{adapter.name}` cannot launch reviewers",
                        f"set `adapter: claude-code` in {CONFIG_FILE}")], None
    available = getattr(adapter, "available", None)
    if available is not None and not available():
        cmd = getattr(adapter, "argv", [adapter.name])[0]
        return [Finding(name, False, f"`{adapter.name}` is configured but `{cmd}` is not on PATH",
                        f"install `{cmd}` or put it on PATH")], adapter
    return [Finding(name, True, adapter.name)], adapter


def _cash(cfg: RepoConfig, adapter: object) -> Finding | None:
    expected = getattr(adapter, "expected_cost_basis", None)
    if expected is None or expected() != "cash":
        return None
    name = "cash budget for billed reviews"
    max_cash = float((cfg.policy.default_budget or {}).get("max_cash", 0) or 0)
    if not max_cash > 0:
        return Finding(name, False, "reviews would be billed in cash and max_cash is 0, so every review is refused",
                       f"set `policy: {{default_budget: {{max_cash: 5}}}}` in {CONFIG_FILE}")
    return Finding(name, True, f"max_cash {max_cash:.2f}")


def diagnose(root: Path) -> list[Finding]:
    config, cfg = _config(root)
    found = [config, _mandatory(cfg), _claude_hooks(root), _git_hooks(root), _on_path()]
    adapter_findings, adapter = _adapter(cfg)
    found += adapter_findings
    if cfg is not None and adapter is not None:
        cash = _cash(cfg, adapter)
        if cash is not None:
            found.append(cash)
    return found


def render(findings: list[Finding]) -> list[str]:
    lines = []
    for f in findings:
        lines.append(f"{'ok  ' if f.ok else 'FAIL'}  {f.name}: {f.detail}")
        if not f.ok:
            lines.append(f"      fix: {f.fix}")
    bad = sum(not f.ok for f in findings)
    lines.append("ready: the gate is set up" if not bad else f"{bad} problem(s) found")
    return lines
