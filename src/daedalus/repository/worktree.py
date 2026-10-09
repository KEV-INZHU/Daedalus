"""Isolated worker workspaces (spec §12, §14).

A worker that is not in-place gets its own `git worktree`, created *outside*
the repository so test runners and file watchers never see it. The worktree is
checked out at the run's base revision and then seeded, byte for byte, with
every path the current candidate changed plus every file the package owns, so
the worker starts from exactly what Ariadne holds as authoritative.

The proposal is derived from the worktree itself, never from the agent's
report: seeded files are compared with their seed hashes, and anything else
git reports as differing from the base revision is a change the worker made.
Changes outside the package's ownership are included so integration can
reject them rather than silently dropping them.

Not part of isolation: gitignored paths (never seeded, never proposed) and the
policy's `candidate.exclude` paths (never seeded, dropped from proposals,
exactly as they are absent from candidates). Symlinks are refused in both
directions, so a worker cannot smuggle outside content in as a regular file.

Limits: a worktree shares the repository's `.git` directory, so a worker can
still create refs, objects or config there (not files in the working tree), and
can hide untracked files from the proposal through `.git/info/exclude`. Hidden
index flags (skip-worktree, assume-unchanged) are detected and fail the worker.
Daedalus runs its own worktree commands with hooks disabled, and the proposal
is computed against the base revision, so commits the worker makes inside the
workspace are included rather than lost.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from daedalus.core.errors import IntegrationError
from daedalus.core.paths import match_any, normalize
from daedalus.repository.candidate import file_sha256, is_git_repo

ROOT_PREFIX = "daedalus-worktrees"


@dataclass(frozen=True)
class Workspace:
    path: Path
    seed: dict[str, str | None]  # rel -> sha256 as seeded; None: absent in the authoritative tree


def worktree_root() -> Path:
    """Per-user directory for workspaces, private on POSIX and verified before use."""
    user = re.sub(r"[^A-Za-z0-9._-]", "_", getpass.getuser() or "user")
    root = Path(tempfile.gettempdir()) / f"{ROOT_PREFIX}-{user}"
    root.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(root)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise IntegrationError(f"{root} is not a plain directory; refusing to use it for workspaces")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise IntegrationError(f"{root} is owned by another user; refusing to use it for workspaces")
    return root


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    # Hooks disabled: creating a workspace must not run the repository's post-checkout
    # hook. The hooks path is the null device, which no process can turn into a
    # directory of hooks. fsmonitor is off so no configured daemon runs while a
    # proposal is computed.
    r = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
    )
    if check and r.returncode != 0:
        raise IntegrationError(f"git {' '.join(args[:2])} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r


def _z(data: bytes) -> list[str]:
    return [normalize(p.decode("utf-8", "surrogateescape")) for p in data.split(b"\0") if p]


def _listed(cwd: Path) -> list[str]:
    """Tracked and untracked, non-ignored files."""
    return _z(_git(cwd, "ls-files", "-c", "-o", "--exclude-standard", "-z").stdout)


def _run_dir(root: Path, run_id: str) -> Path:
    repo = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]
    return worktree_root() / repo / run_id


def workspace_path(root: Path, run_id: str, task_id: str) -> Path:
    run_dir = _run_dir(root, run_id)
    path = run_dir / task_id
    if path.resolve().parent != run_dir.resolve():
        raise IntegrationError(f"task id {task_id!r} does not name a single directory")
    return path


def create(
    root: Path,
    run_id: str,
    task_id: str,
    base_revision: str | None,
    changed_paths: tuple[str, ...] | list[str],
    owned: tuple[str, ...] | list[str],
    exclude: tuple[str, ...] = (),
) -> Workspace:
    """Create and seed a workspace. On any failure the partial workspace is removed."""
    if base_revision is None or not is_git_repo(root):
        raise IntegrationError("worktree isolation needs a git repository with a base commit")
    path = workspace_path(root, run_id, task_id)
    if path.exists():
        remove(root, path)  # a stale workspace from an earlier, interrupted attempt
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(root, "worktree", "add", "--detach", "--force", str(path), base_revision)
        seeded = set(changed_paths) | {rel for rel in _listed(root) if match_any(rel, owned)}
        seed: dict[str, str | None] = {}
        for rel in sorted(seeded):
            if match_any(rel, exclude):
                continue
            src, dst = root / rel, path / rel
            if src.is_symlink():
                raise IntegrationError(f"{rel} is a symlink; symlinks are not supported in isolated workspaces")
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)  # bytes, so seeded files hash identically
                seed[rel] = file_sha256(dst)
            else:
                if dst.is_file() or dst.is_symlink():
                    dst.unlink()
                seed[rel] = None
    except BaseException:
        try:
            remove(root, path)
        except Exception:  # noqa: BLE001 — never let rollback hide the original failure
            pass
        raise
    return Workspace(path, seed)


def proposal(
    ws_path: Path, seed: dict[str, str | None], base_revision: str, exclude: tuple[str, ...] = ()
) -> dict[str, object]:
    """{files: {rel: text | None}, base_hashes: {rel: seed hash}} for every change in the workspace.

    A path that was not seeded was, at dispatch, either absent or identical to the
    base revision outside the package's ownership; its base hash is None, which
    integration treats as "must not exist" — so an unowned edit is rejected for
    ownership before that matters, and a new file integrates only if still absent.
    """
    if not ws_path.is_dir():
        raise IntegrationError(f"workspace {ws_path} no longer exists")
    changed: set[str] = set()
    for rel, h in seed.items():
        p = ws_path / rel
        if p.is_symlink():
            raise IntegrationError(f"{rel} became a symlink; symlinks are not supported in proposals")
        now = file_sha256(p) if p.is_file() else None
        if now != h:
            changed.add(rel)
    # Index flags that hide working-tree edits from git (skip-worktree, assume-unchanged)
    # would let an unowned edit slip past ownership checks: refuse them outright.
    hidden = [e[2:] for e in _z(_git(ws_path, "ls-files", "-v", "-z").stdout) if e[:1] == "S" or e[:1].islower()]
    if hidden:
        raise IntegrationError(f"index entries hidden from git in the workspace: {hidden[:5]}")
    # Against the base revision, not HEAD: a worker that commits inside the workspace
    # moves HEAD, and those changes must still be part of the proposal.
    tracked = _z(_git(ws_path, "diff", "--name-only", "--no-renames", "-z", base_revision).stdout)
    untracked = _z(_git(ws_path, "ls-files", "-o", "--exclude-standard", "-z").stdout)
    changed |= {rel for rel in tracked + untracked if rel not in seed}
    files: dict[str, str | None] = {}
    for rel in sorted(changed):
        if match_any(rel, exclude):
            continue  # not part of any candidate, so not part of a proposal
        p = ws_path / rel
        if p.is_symlink():
            raise IntegrationError(f"{rel} is a symlink; symlinks are not supported in proposals")
        if not p.is_file():
            files[rel] = None
            continue
        try:
            files[rel] = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrationError(f"{rel} is not UTF-8 text; binary proposals are not supported") from exc
    return {"files": files, "base_hashes": {rel: seed.get(rel) for rel in files}}


def _is_workspace(path: Path) -> bool:
    """True only for paths inside this user's workspace root (matched by its
    directory name, so a workspace still qualifies if TMP changed since creation)."""
    return any(p.name == worktree_root().name for p in path.resolve().parents)


def remove(root: Path, path: Path) -> bool:
    """Remove a workspace. Idempotent; never touches the authoritative tree.

    Returns whether the workspace is gone afterwards (files held open by an
    exiting agent or a scanner can keep it alive on Windows).
    """
    path = Path(path)
    if not _is_workspace(path):
        raise IntegrationError(f"refusing to remove {path}: not a Daedalus workspace")
    _git(root, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    for parent in path.parents:  # drop now-empty run and repository directories
        if parent.name == worktree_root().name:
            break
        try:
            parent.rmdir()
        except OSError:
            break  # not empty: another workspace still lives here
    return not path.exists()


def sweep(root: Path, run_id: str, keep: set[str]) -> list[str]:
    """Remove this run's workspaces that no active worker records (e.g. after a
    crash between creating a workspace and logging the dispatch)."""
    run_dir = _run_dir(root, run_id)
    if not run_dir.is_dir():
        return []
    removed = []
    for child in run_dir.iterdir():
        if str(child) not in keep and child.is_dir():
            remove(root, child)
            removed.append(str(child))
    return removed
