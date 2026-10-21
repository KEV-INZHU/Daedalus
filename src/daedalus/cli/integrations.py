"""Integrations: install harness hooks, and list the harnesses and adapters that are registered."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from daedalus.cli._common import (
    _print,
    add_command,
    shorten,
)
from daedalus.core.config import (
    find_root,
)
from daedalus.core.errors import DaedalusError


def _install_harness(name: str, root: Path, roles: tuple[str, ...] | None = None) -> list[str]:
    """Install one registered harness integration; the lines say what was done."""
    from daedalus import plugins

    try:
        integration = plugins.get_harness(name)
    except plugins.PluginError as exc:
        raise DaedalusError(str(exc)) from exc
    if name == "claude-code" and roles is not None:  # integrations with role skills to choose
        from daedalus.harness.claude_code import install

        return install(root, skills=roles)
    if name == "pi" and roles is not None:
        from daedalus.harness.pi import install as pi_install

        return [f"pi: {line}" for line in pi_install(root, skills=roles)]
    return [f"{name}: {line}" if name != "claude-code" else line for line in integration.install(root)]


SKILL_HARNESSES = ("claude-code", "pi")  # the integrations whose role skills `--all-skills` can choose
# The paths each harness's install writes under, for the "commit this" advice. `init --commit` commits
# only the files that changed inside them (see `snapshot_paths`), never a user's other files there.
HARNESS_PATHS = {
    "claude-code": [".claude"], "codex": [".codex", "AGENTS.md"], "opencode": [".opencode"],
    "pi": [".pi"], "hermes": [".hermes"],
}
HARNESS_FILES = {
    name: " and ".join(p if p.endswith(".md") else p + "/" for p in paths) for name, paths in HARNESS_PATHS.items()
}


def snapshot_paths(root: Path, paths: list[str]) -> dict[str, tuple[str, int]]:
    """(content hash, mtime) of every file under `paths` (repository-relative, forward slashes). An
    installer that rewrites a file with identical content still moves its mtime, so "written" means
    "created, changed or rewritten" and a user's untouched files are never in it."""
    import hashlib

    out: dict[str, tuple[str, int]] = {}
    for rel_path in paths:
        base = root / rel_path
        files = [base] if base.is_file() else [f for f in base.rglob("*") if f.is_file()] if base.is_dir() else []
        for f in files:
            try:
                out[f.relative_to(root).as_posix()] = (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns)
            except OSError:
                continue
    return out


def _commit_what(names: list[str]) -> str:
    """The paths to commit after installing `names`; harnesses we do not know are 'the files listed above'."""
    paths = [HARNESS_FILES[n] for n in names if n in HARNESS_FILES]
    if any(n not in HARNESS_FILES for n in names) or not paths:
        paths.append("the files listed above")
    return ", ".join(paths)


def _init_harnesses(root: Path, explicit: list[str] | None) -> list[str]:
    """Harnesses `init` installs: those named with --harness, else claude-code plus every detected one."""
    from daedalus import plugins

    if explicit:
        unknown = [n for n in explicit if n not in plugins.harness_names()]
        if unknown:
            raise DaedalusError(f"unknown harness {unknown[0]!r}: use one of {plugins.harness_names()}")
        return list(dict.fromkeys(explicit))
    return ["claude-code"] + [h.name for h in plugins.detected_harnesses(root) if h.name != "claude-code"]


def cmd_install(args: argparse.Namespace) -> int:
    from daedalus import plugins
    from daedalus.harness.provenance import install_git_hook

    root = find_root(args.dir)
    if bool(args.harness) == bool(args.all_detected):
        raise DaedalusError(f"name a harness or pass --all-detected (registered: {plugins.harness_names()})")
    names = args.harness and [args.harness] or [h.name for h in plugins.detected_harnesses(root)]
    if args.all_skills and (bad := [n for n in names if n not in SKILL_HARNESSES]):
        raise DaedalusError(
            f"--all-skills is supported only for {', '.join(SKILL_HARNESSES)}, not {', '.join(bad)}; "
            "drop --all-skills or name one of those"
        )
    if not names:
        _print("no harness detected in this repository or on PATH; name one: daedalus install <harness>")
        return 0
    roles = ("gate",) + (("brunel", "socrates", "mozi", "aristotle", "james", "plato", "vitruvius") if args.all_skills else ())
    for name in names:
        for line in _install_harness(name, root, roles):
            _print(line)
    try:
        _print(shorten(root, install_git_hook(root)))
    except DaedalusError as exc:
        _print(f"note: {exc}")
    if names == ["claude-code"]:
        _print("Commit .claude/ so every session works under the gate: the first edit opens a run, and the")
        _print("gate verifies, reviews and decides when the agent stops.")
    else:
        _print(f"Commit {_commit_what(names)} so every session works under the gate.")
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    from daedalus import plugins

    root = find_root(args.dir)
    detected = {h.name for h in plugins.detected_harnesses(root)}
    _print("Harness integrations (daedalus install <name>)")
    for info in plugins.list_harnesses():
        state = "available" if info.available else "unavailable"
        seen = ("detected" if info.name in detected else "not detected") if info.available else "-"
        _print(f"  {info.name:<14} {info.source:<12} {state:<12} {seen:<13} {info.detail}")
    _print()
    _print("Adapters (adapter.name in .daedalus.yml)")
    for info in plugins.list_adapters():
        state = "available" if info.available else "unavailable"
        _print(f"  {info.name:<14} {info.source:<12} {state:<12} read-only reviews: {info.read_only:<16} {info.detail}")
    return 0


def register(sub: Any) -> None:
    sp = add_command(sub, "install", cmd_install, "install harness integration")
    sp.add_argument("harness", nargs="?", help="a registered harness (see `daedalus plugins`)")
    sp.add_argument("--all-detected", action="store_true", help="install every harness detected here")
    sp.add_argument("--all-skills", action="store_true", help="also install the role skills")
    sp = add_command(sub, "plugins", cmd_plugins, "list harness integrations and adapters, built in or from entry points")
    sp.add_argument("action", nargs="?", choices=("list",), default="list")
