"""The few git operations the line needs: branches, exact staging, commits.

Commits skip repository hooks (`--no-verify`). A pre-commit hook that reformats
or rewrites files would make the committed tree differ from the candidate that
was accepted, and the factory only ships exactly what was accepted.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from daedalus.core.paths import match_any, normalize
from daedalus.factory.orders import FactoryError


def git(root: Path, *args: str, check: bool = True, stdin: bytes | None = None, raw: bool = False) -> str:
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, input=stdin, env=env, check=False
        )
    except FileNotFoundError as exc:
        raise FactoryError("git is not installed") from exc
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise FactoryError(f"git {' '.join(args[:2])} failed: {err}")
    if r.returncode != 0:
        return ""
    text = r.stdout.decode("utf-8", "surrogateescape")
    return text if raw else text.strip()


def ok(root: Path, *args: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    except FileNotFoundError:
        return False
    return r.returncode == 0


def head(root: Path) -> str:
    sha = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if not sha:
        raise FactoryError("the repository has no commits yet; commit something first")
    return sha


def current_branch(root: Path) -> str | None:
    """The checked-out branch, or None on a detached HEAD."""
    return git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None


def branch_exists(root: Path, name: str) -> bool:
    return ok(root, "rev-parse", "--verify", "-q", f"refs/heads/{name}")


def valid_branch_name(root: Path, name: str) -> bool:
    return ok(root, "check-ref-format", "--branch", name)


def dirty_paths(root: Path, exclude: Iterable[str] = ()) -> list[str]:
    """Uncommitted changes, ignoring paths the candidate never covers (Daedalus state, caches)."""
    out = git(root, "status", "--porcelain", "-z", "--untracked-files=all", raw=True)
    paths, skip = [], False
    ex = tuple(exclude)
    for entry in out.split("\0"):
        if skip:  # the second path of a rename record
            skip = False
            continue
        if len(entry) < 4:
            continue
        code, path = entry[:2], normalize(entry[3:])
        if "R" in code or "C" in code:
            skip = True
        if not match_any(path, ex):
            paths.append(path)
    return paths


def stage_exact(root: Path, paths: Iterable[str]) -> None:
    """Stage exactly these paths (edits, additions and deletions), nothing else."""
    ps = list(paths)
    if ps:
        git(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", stdin="\0".join(ps).encode("utf-8", "surrogateescape"))


def has_staged_changes(root: Path) -> bool:
    return not ok(root, "diff", "--cached", "--quiet")


def commit(root: Path, message: str) -> str:
    git(root, "commit", "-q", "--no-verify", "-F", "-", stdin=message.encode())
    return head(root)
