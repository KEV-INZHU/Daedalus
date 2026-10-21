"""`daedalus doctor`: is this repository set up for the frictionless gate?

Each check reports what it found and, when it fails, a one-line fix. Doctor only
reads: no config is loaded through anything that writes state, git is asked with
`--no-optional-locks` so not even the index is refreshed, and no adapter is launched.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
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
    warn: bool = False  # passes, but something the person should know (shown as WARN, not FAIL)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                              capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


NO_GIT = "git is not on PATH"
NO_REPO = "not a git repository"
INIT_FIX = "run `git init`, then `daedalus install claude-code`"


NOT_COMMITTED = "not committed"


def _repo(root: Path) -> str:
    """"" inside a work tree; otherwise why not: NO_GIT, NO_REPO or git's own error."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is None:
        return NO_GIT
    if inside.returncode == 0:
        return "" if inside.stdout.strip() == "true" else NO_REPO
    err = inside.stderr.strip()
    if not err or NO_REPO in err:
        return NO_REPO
    return f"git failed: {err.splitlines()[0]}"


def _committed(root: Path, path: str) -> str:
    """"" when tracked and identical to HEAD in the index and the working tree; otherwise
    why not: whatever `_repo` reports, or NOT_COMMITTED."""
    why = _repo(root)
    if why:
        return why
    tracked = _git(root, "ls-files", "--error-unmatch", "--", path)
    status = _git(root, "status", "--porcelain", "--", path)
    ok = tracked and status and tracked.returncode == 0 and status.returncode == 0 and not status.stdout.strip()
    return "" if ok else NOT_COMMITTED


def _not_committed(name: str, why: str, detail: str, fix: str) -> Finding:
    if why == NO_GIT:
        return Finding(name, False, NO_GIT, "install git")
    if why == NO_REPO:
        return Finding(name, False, NO_REPO, INIT_FIX)
    if why != NOT_COMMITTED:
        return Finding(name, False, why, "fix the git error, then run `daedalus doctor` again")
    return Finding(name, False, detail, fix)


def _config(root: Path) -> tuple[Finding, RepoConfig | None]:
    name = f"{CONFIG_FILE} committed and valid"
    if not (root / CONFIG_FILE).exists():
        return Finding(name, False, f"no {CONFIG_FILE}", f"run `daedalus init`, then commit {CONFIG_FILE}"), None
    try:
        cfg = load_config(root)
    except DaedalusError as exc:
        return Finding(name, False, f"invalid: {exc}", f"correct {CONFIG_FILE} (see `daedalus policy --default`)"), None
    why = _committed(root, CONFIG_FILE)
    if why:
        return _not_committed(name, why, "valid but not committed",
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

    def lst(v: object) -> list:
        return v if isinstance(v, list) else []

    missing = [ev for ev in HOOK_EVENTS
               if not any(isinstance(h, dict) and isinstance(h.get("command"), str) and _ours(h)
                          for g in lst(hooks.get(ev)) if isinstance(g, dict) for h in lst(g.get("hooks")))]
    if missing:
        return Finding(name, False, f"missing hooks: {', '.join(missing)}", fix)
    why = _committed(root, CLAUDE_SETTINGS)
    if why:
        return _not_committed(name, why, f"{CLAUDE_SETTINGS} is not committed",
                              "git add .claude/ && git commit -m 'Install Daedalus hooks'")
    return Finding(name, True, ", ".join(HOOK_EVENTS))


def _git_hooks(root: Path) -> Finding:
    from daedalus.harness.provenance import GIT_HOOKS, hook_path

    name = "git hooks installed"
    fix = "run `daedalus install claude-code`"
    missing: list[str] = []
    not_exec: list[Path] = []
    for hook, mark, _script, _hint in GIT_HOOKS:
        try:
            p = hook_path(root, hook)
        except FileNotFoundError:
            return Finding(name, False, NO_GIT, "install git")
        if p is None:
            return Finding(name, False, NO_REPO, INIT_FIX)
        try:
            ours = mark in p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ours = False
        if not ours:
            missing.append(hook)
        elif os.name == "posix" and not os.access(p, os.X_OK):
            not_exec.append(p)
    if missing:
        return Finding(name, False, f"missing: {', '.join(missing)}", fix)
    if not_exec:
        return Finding(name, False, f"not executable, so git skips them: {', '.join(p.name for p in not_exec)}",
                       shlex.join(["chmod", "+x", *map(str, not_exec)]))
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
        if cfg.mode == "gate":
            # What `init` writes: gate mode runs checks only, so no reviewer is needed. A risk floor can
            # still ask for a review; that falls to a person until an adapter is configured.
            return [Finding(name, True,
                            f"`{adapter.name}`: gate mode runs checks only, and nothing can launch an independent "
                            f"review, so high-tier changes (risk floors such as auth, migrations, test_integrity) "
                            f"will need a human `daedalus risk-exception`",
                            f"for automatic reviews set `adapter: claude-code` and `mode: review` in {CONFIG_FILE}",
                            warn=True)], None
        return [Finding(name, False, f"`{adapter.name}` cannot launch reviewers, and mode `{cfg.mode}` needs them",
                        f"set `adapter: claude-code` in {CONFIG_FILE}, or `mode: gate`")], None
    if getattr(adapter, "endpoint", None):
        return _http_findings(name, adapter), adapter
    available = getattr(adapter, "available", None)
    if available is not None and not available():
        cmd = getattr(adapter, "argv", [adapter.name])[0]
        return [Finding(name, False, f"`{adapter.name}` is configured but `{cmd}` is not on PATH",
                        f"install `{cmd}` or put it on PATH")], adapter
    return [Finding(name, True, adapter.name)], adapter


def _http_findings(name: str, adapter: object) -> list[Finding]:
    """The config of an HTTP reviewer (`openai-compatible`, `anthropic`). The adapter's constructor has
    already checked base_url and review_model, so what is left is the key and what the reviewer can see."""
    label = getattr(adapter, "name", "adapter")
    found: list[Finding] = []
    env = getattr(adapter, "api_key_env", None)
    local = getattr(adapter, "local", False)
    if env and not os.environ.get(env) and not local:
        found.append(Finding(name, False, f"`{label}` reads its API key from ${env}, which is not set",
                             f"export {env}=... where reviews run (the key stays out of {CONFIG_FILE})"))
    elif not env and not local:
        found.append(Finding(name, False, f"`{label}` talks to {adapter.base_url} but names no `api_key_env`",
                             f"add `api_key_env: <VARIABLE NAME>` under `adapter:` in {CONFIG_FILE}"))
    else:
        found.append(Finding(name, True, f"{label} -> {adapter.base_url}, model {adapter.review_model}"))
    if not local and adapter.base_url.startswith("http://"):
        found.append(Finding("HTTPS for reviews", True, "the API key would travel unencrypted",
                             "use an https:// base_url", warn=True))
    if getattr(adapter, "cost_untracked", False):
        found.append(Finding("review cost tracking", True,
                             f"`{label}` talks to {adapter.base_url} and has no `cost_per_mtok`, so what it bills is "
                             "unknown: reviews count as cash with unknown cost, the cash budget fails closed, and a "
                             "run that launches one cannot be accepted",
                             f"add `cost_per_mtok: {{input: <dollars>, output: <dollars>}}` under `adapter:` in "
                             f"{CONFIG_FILE}", warn=True))
    found.append(Finding("review context", True,
                         f"`{label}` reviewers have no tools: they judge from the diff and the full content of "
                         f"changed files only (up to {adapter.max_context_chars} characters), never the rest of "
                         "the repository",
                         "fine for medium-tier changes; use a stronger model for high-tier ones "
                         "(see docs/adapters.md)", warn=True))
    return found


def _cash(cfg: RepoConfig, adapter: object) -> Finding | None:
    expected = getattr(adapter, "expected_cost_basis", None)
    if expected is None or expected() != "cash":
        return None
    name = "cash budget for billed reviews"
    fix = f"set `max_cash` to a positive amount in {CONFIG_FILE}, e.g. `policy: {{default_budget: {{max_cash: 5}}}}`"
    try:
        max_cash = float((cfg.policy.default_budget or {}).get("max_cash", 0) or 0)
    except (TypeError, ValueError):
        return Finding(name, False, "max_cash is not a number", fix)
    if not max_cash < math.inf:  # NaN fails this too; every run rejects a cap that is not finite
        return Finding(name, False, "max_cash is not a finite amount, so every run rejects it", fix)
    if max_cash < 0:
        return Finding(name, False, f"max_cash is negative ({max_cash:g}), so every run rejects it", fix)
    if not max_cash > 0:
        return Finding(name, False, "reviews would be billed in cash and max_cash is 0, so every review is refused",
                       fix)
    return Finding(name, True, f"max_cash {max_cash:.2f}")


def diagnose(root: Path) -> list[Finding]:
    config, cfg = _config(root)
    found = [config, _mandatory(cfg), _claude_hooks(root), _git_hooks(root), _on_path()]
    repo = _repo(root)
    if repo in (NO_GIT, NO_REPO):  # a fix that ends in a commit needs a repository first
        first = "install git" if repo == NO_GIT else "run `git init`"
        found = [replace(f, fix=f"{first}, then {f.fix}") if not f.ok and "commit" in f.fix else f for f in found]
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
        detail = " ".join(f.detail.split())  # parser errors span lines; keep one line per finding
        lines.append(f"{'FAIL' if not f.ok else 'WARN' if f.warn else 'ok  '}  {f.name}: {detail}")
        if not f.ok or f.warn:
            lines.append(f"      fix: {f.fix}")
    bad = sum(not f.ok for f in findings)
    lines.append("ready: the gate is set up" if not bad else f"{bad} problem(s) found")
    return lines
