"""Candidate identity (spec §7.1).

A commit hash does not identify a dirty working tree. A candidate manifest is
the base revision plus the content hash of every path that differs from it
(tracked edits, deletions, and untracked non-ignored files). Because the base
tree is itself content-addressed, base + changes identifies the complete
non-ignored working-tree content. `candidate_id` is the hash of the canonical
manifest.

Anything that cannot be captured (a dirty submodule, an unreadable file) marks
the manifest incomplete; risk classification escalates on incomplete capture
and the gap is reported rather than ignored.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daedalus.core.paths import match_any, normalize
from daedalus.core.policy import canonical_json, sha256_hex

MANIFEST_SCHEMA = 1
MAX_DIFF_BYTES = 200_000


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=check,
    )


def is_git_repo(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    except FileNotFoundError:
        return False


def git_head(root: Path) -> str | None:
    """HEAD commit, or None for a non-git directory or an unborn branch."""
    if not is_git_repo(root):
        return None
    r = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip() or None


def _split_z(data: bytes) -> list[str]:
    return [normalize(p.decode("utf-8", "surrogateescape")) for p in data.split(b"\0") if p]


def file_sha256(path: Path) -> str:
    """Raises OSError; callers decide whether that makes a capture incomplete."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class CandidateManifest:
    manifest: dict[str, Any]
    candidate_id: str
    changed_paths: tuple[str, ...]
    complete: bool
    incomplete_reasons: tuple[str, ...] = field(default=())

    @property
    def short(self) -> str:
        return self.candidate_id[:12]

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, **self.manifest}


def _hash_entry(root: Path, rel: str, reasons: list[str]) -> str | None:
    p = root / rel
    try:
        if p.is_symlink():
            return "symlink:" + sha256_hex(os.readlink(p))
        if p.is_dir():
            # A gitlink (submodule) shows up as a directory in the diff.
            sub = _git(p, "rev-parse", "HEAD", check=False)
            if sub.returncode != 0:
                reasons.append(f"{rel}: directory entry is not a readable submodule")
                return "unreadable"
            dirty = _git(p, "status", "--porcelain", check=False).stdout.strip()
            if dirty:
                reasons.append(f"{rel}: submodule has uncommitted changes")
            return "submodule:" + sub.stdout.decode().strip() + ("+dirty" if dirty else "")
        if not p.exists():
            return None  # deleted relative to base
        return file_sha256(p)
    except OSError as exc:
        reasons.append(f"{rel}: {exc.strerror or exc}")
        return "unreadable"


def _changed_paths_git(root: Path, base: str | None) -> list[str]:
    untracked = _split_z(_git(root, "ls-files", "-o", "--exclude-standard", "-z").stdout)
    if base is None:
        tracked = _split_z(_git(root, "ls-files", "-c", "-z").stdout)
    else:
        tracked = _split_z(_git(root, "diff", "--relative", "--name-only", "--no-renames", "-z", base).stdout)
    return sorted(set(tracked) | set(untracked))


def _all_files(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            out.append(normalize(os.path.relpath(os.path.join(dirpath, name), root)))
    return sorted(out)


def capture(root: str | Path, base_revision: str | None, exclude: tuple[str, ...] = ()) -> CandidateManifest:
    root = Path(root)
    reasons: list[str] = []
    git = is_git_repo(root)
    if git:
        paths = _changed_paths_git(root, base_revision)
        sub = _git(root, "submodule", "status", "--recursive", check=False)
        submodules = sha256_hex(sub.stdout) if sub.returncode == 0 and sub.stdout.strip() else None
        remote = _git(root, "config", "--get", "remote.origin.url", check=False).stdout.decode().strip()
        repo_identity = remote or "local:" + root.name
    else:
        paths = _all_files(root)
        submodules = None
        repo_identity = "dir:" + root.name
        if base_revision is not None:
            reasons.append("base revision recorded but directory is no longer a git repository")

    files: dict[str, str | None] = {}
    for rel in paths:
        if match_any(rel, exclude):
            continue
        files[rel] = _hash_entry(root, rel, reasons)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "repository": repo_identity,
        "base_revision": base_revision,
        "files": files,
        "submodules": submodules,
    }
    return CandidateManifest(
        manifest=manifest,
        candidate_id=sha256_hex(canonical_json(manifest)),
        changed_paths=tuple(sorted(files)),
        complete=not reasons,
        incomplete_reasons=tuple(reasons),
    )


def diff_text(root: str | Path, manifest: CandidateManifest, limit: int = MAX_DIFF_BYTES) -> str:
    """Human/agent-readable diff of the candidate against its base (for reviewers)."""
    root = Path(root)
    parts: list[str] = []
    base = manifest.manifest["base_revision"]
    tracked: set[str] = set()
    if base and is_git_repo(root):
        paths = list(manifest.changed_paths)
        if paths:
            r = _git(root, "diff", "--relative", "--no-renames", base, "--", *paths, check=False)
            parts.append(r.stdout.decode("utf-8", "replace"))
            tracked = set(
                _split_z(
                    _git(
                        root, "diff", "--relative", "--name-only", "--no-renames", "-z", base, "--", *paths
                    ).stdout
                )
            )
    for rel in manifest.changed_paths:
        if rel in tracked or manifest.manifest["files"][rel] is None:
            continue
        p = root / rel
        if p.is_file():
            try:
                body = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                body = "<binary or unreadable>"
            parts.append(
                f"--- /dev/null\n+++ b/{rel}  (new file)\n"
                + "".join("+" + ln + "\n" for ln in body.splitlines())
            )
    text = "\n".join(parts)
    if len(text) > limit:
        text = text[:limit] + f"\n... diff truncated at {limit} bytes ..."
    return text
