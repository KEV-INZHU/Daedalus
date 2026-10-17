"""Commit provenance: accepted work says so in its own commit message.

A `prepare-commit-msg` git hook (installed by `daedalus init`, and by the
Claude Code SessionStart hook when missing) adds trailers to a commit whose
content is exactly an ACCEPTED candidate:

    Daedalus-Run: 75787c4300b4
    Daedalus-Candidate: 3455f25c94fb...

"Exactly" means: a new commit (not an amend) made directly on the run's base,
whose content is the accepted candidate and nothing else (the index equals the
working tree, nothing outside the candidate is staged). A partial commit, an
empty one, a commit of the remainder after part of the work was committed, or
one that adds anything after acceptance, gets no trailers. The hook never blocks a commit and never edits one it cannot vouch for.

Trailers are claims, not proof: anyone can type them. They make the gate's
result visible where people look (the log, the PR) and give CI something to
check; `daedalus ci` re-runs the trusted checks rather than trusting them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from daedalus.core.config import find_root, load_config
from daedalus.core.errors import DaedalusError
from daedalus.core.state_machine import Disposition

MARK = "# daedalus: prepare-commit-msg"
HOOK_SCRIPT = f"""#!/bin/sh
{MARK} (adds Daedalus-Run trailers to commits of accepted work; never blocks a commit)
command -v daedalus >/dev/null 2>&1 || exit 0
daedalus hook commit-msg "$1" "$2" >/dev/null 2>&1 || true
"""
RECENT_RUNS = 5  # a commit hook must stay fast: only the latest runs can be what is committed


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def hook_path(root: Path) -> Path | None:
    r = _git(root, "rev-parse", "--git-path", "hooks/prepare-commit-msg")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else root / p


def install_git_hook(root: Path, *, only_if_absent: bool = False) -> str:
    """Install the hook. Never replaces someone else's prepare-commit-msg hook."""
    p = hook_path(root)
    if p is None:
        raise DaedalusError("not a git repository; the commit hook was not installed")
    if only_if_absent:
        top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
        if top and Path(top).resolve() in p.resolve().parents and ".git" not in p.resolve().relative_to(Path(top).resolve()).parts:
            # core.hooksPath points into the work tree (husky and friends): writing there would
            # change a tracked file behind the user's back.
            return f"git hooks live in the work tree ({p.parent}); run `daedalus install claude-code` to add ours"
    if p.exists():
        if MARK in p.read_text(encoding="utf-8", errors="replace"):
            if only_if_absent:
                return "git hook prepare-commit-msg already installed"
        else:
            raise DaedalusError(
                f"{p} already exists and is not Daedalus's; add `daedalus hook commit-msg \"$1\" \"$2\"` to it"
            )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HOOK_SCRIPT, encoding="utf-8")
    p.chmod(0o755)
    return f"git hook prepare-commit-msg -> {p}"


def accepted_run_for_commit(root: Path, source: str = "") -> tuple[str, str] | None:
    """(run id, candidate id) when the commit being made is exactly an accepted candidate:
    a new commit (not an amend), directly on the run's base, whose content is the candidate."""
    from daedalus.orchestration.ariadne import Ariadne
    from daedalus.repository.candidate import capture, git_head

    if source == "commit":
        return None  # an amend or -c/-C reuse: not a fresh commit of the accepted work
    if _git(root, "diff", "--quiet").returncode != 0:
        return None  # unstaged changes: the commit is not the working tree
    if _git(root, "diff", "--cached", "--quiet", "HEAD").returncode == 0:
        return None  # nothing changes: an empty commit vouches for nothing
    head = git_head(root)
    ari = Ariadne(root)
    try:
        exclude = load_config(root).policy.candidate_exclude
        for rid in reversed(ari.store.run_ids()[-RECENT_RUNS:]):
            try:
                state = ari.state(rid)
            except DaedalusError:
                continue
            if state.disposition is not Disposition.ACCEPTED or not state.accepted_candidate_id:
                continue
            base = state.contract.base_revision
            if not base or base != head:
                continue  # the commit must hold the whole candidate, so it must sit directly on the base
            cand = capture(root, base, exclude)
            if cand.candidate_id != state.accepted_candidate_id:
                continue
            staged = _git(root, "diff", "--cached", "--name-only", "--no-renames", base).stdout.splitlines()
            if any(p not in cand.manifest["files"] for p in staged):
                continue  # something outside the candidate is staged
            untracked = _git(root, "ls-files", "-o", "--exclude-standard").stdout.splitlines()
            if any(p in cand.manifest["files"] for p in untracked):
                continue  # part of the candidate is not in the commit
            return rid, state.accepted_candidate_id
        return None
    finally:
        ari.close()


def commit_msg_hook(args: list[str]) -> None:
    """`prepare-commit-msg <file> [source]`: add trailers for an accepted candidate. Never raises."""
    try:
        if not args:
            return
        msg_file = Path(args[0])
        root = find_root(".")
        text = msg_file.read_text(encoding="utf-8")
        if "Daedalus-Run:" in text:
            return  # the factory, or a human, already said where this came from
        found = accepted_run_for_commit(root, args[1] if len(args) > 1 else "")
        if found is None:
            return
        rid, cid = found
        trailers = [f"Daedalus-Run: {rid}", f"Daedalus-Candidate: {cid}"]
        cmd = ["git", "-C", str(root), "interpret-trailers", "--in-place"]
        for t in trailers:
            cmd += ["--trailer", t]
        subprocess.run([*cmd, str(msg_file)], capture_output=True, check=False)
    except Exception:  # noqa: BLE001 — a commit hook must never break a commit
        return
