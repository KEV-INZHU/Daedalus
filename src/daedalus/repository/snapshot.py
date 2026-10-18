"""Reviewed-content snapshots: what a reviewer actually saw, kept so the next review can
be scoped to what changed since.

A candidate manifest records only content hashes, so on its own it cannot say *how*
a file changed between two candidates. When Daedalus launches a review it copies the
candidate's changed files into the run's state directory. A later review of the same
perspective can then be a **delta review**: the inter-diff between the reviewed
candidate and the current one, plus the earlier findings to confirm.

Every snapshot file is checked against the hash its manifest recorded before it is
used. A missing, edited or unreadable snapshot means no inter-diff, and the review
falls back to a full one: a delta is only ever built from content proven to be what
was reviewed.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from daedalus.repository.candidate import MAX_DIFF_BYTES, file_sha256

SNAPSHOTS = "snapshots"


def snapshot_dir(run_dir: Path, candidate_id: str) -> Path:
    return run_dir / SNAPSHOTS / candidate_id


def _plain(digest: Any) -> bool:
    return isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def save(root: Path, run_dir: Path, candidate_id: str, manifest: dict[str, Any]) -> bool:
    """Copy the candidate's changed files (as they are now) beside the run. Idempotent and
    best effort: True only when every plain file was copied and matches its hash. Anything
    else leaves the snapshot incomplete, and the next review is simply a full one."""
    dest = snapshot_dir(run_dir, candidate_id)
    if (dest / ".complete").exists():
        return True
    try:
        for rel, digest in manifest.get("files", {}).items():
            if digest is None:
                continue  # deleted: nothing to keep
            if not _plain(digest):
                return False  # a symlink, submodule or unreadable entry cannot be proven later
            target = dest / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, target)  # keeps the mode (an executable stays executable)
            if file_sha256(target) != digest:
                return False  # changed between capture and copy
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".complete").write_text("", encoding="utf-8")
        return True
    except OSError:
        return False


def restore(root: Path, run_dir: Path, candidate_id: str, manifest: dict[str, Any], current: dict[str, Any]) -> bool:
    """Put the working tree back to a snapshotted candidate: its changed files from the
    snapshot, everything else as on the base. True when every path could be restored."""
    if not (snapshot_dir(run_dir, candidate_id) / ".complete").exists():
        return False
    base = manifest.get("base_revision")
    if not base or base != current.get("base_revision"):
        return False
    want = manifest.get("files", {})
    # Read and prove every source first; write only when all of them are in hand.
    plan: dict[str, bytes | None] = {}
    modes: dict[str, int] = {}
    for rel in sorted(set(want) | set(current.get("files", {}))):
        if rel in want and want[rel] is None:
            plan[rel] = None  # deleted in the candidate
        elif rel in want:
            src = snapshot_dir(run_dir, candidate_id) / "files" / rel
            try:
                if file_sha256(src) != want[rel]:
                    return False
                plan[rel] = src.read_bytes()
                modes[rel] = src.stat().st_mode & 0o777
            except OSError:
                return False
        else:  # as on the base in the candidate (or absent from it)
            exists = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{base}:{rel}"],
                                    capture_output=True, check=False).returncode == 0
            if not exists:
                plan[rel] = None  # not on the base: added after the candidate
                continue
            r = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"], capture_output=True, check=False)
            if r.returncode != 0:
                return False  # a git error is not "absent": refuse rather than delete
            plan[rel] = r.stdout
            tree = subprocess.run(["git", "-C", str(root), "ls-tree", base, "--", rel],
                                  capture_output=True, text=True, check=False).stdout.split()
            modes[rel] = 0o755 if tree and tree[0] == "100755" else 0o644
    backup: dict[str, bytes | None] = {}
    try:
        for rel in plan:
            p = root / rel
            backup[rel] = p.read_bytes() if p.is_file() else None
        for rel, data in plan.items():
            p = root / rel
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                if rel in modes:
                    p.chmod(modes[rel])
    except OSError:
        for rel, data in backup.items():  # put the tree back the way it was
            try:
                p = root / rel
                if data is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(data)
            except OSError:
                pass
        return False
    return True


def _base_text(root: Path, base: str | None, rel: str) -> str | None:
    if not base:
        return None
    r = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"], capture_output=True, check=False)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def _snap_text(run_dir: Path, candidate_id: str, rel: str, digest: str) -> str | None:
    p = snapshot_dir(run_dir, candidate_id) / "files" / rel
    try:
        if file_sha256(p) != digest:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _side(root: Path, run_dir: Path, old_id: str, base: str | None, rel: str, h: Any, *, reviewed: bool) -> list[str]:
    """One side of the inter-diff for `rel`: the base, the reviewed snapshot, or the live file,
    each proven by its hash before it is used."""
    if h == "base":
        text = _base_text(root, base, rel)
        return [] if text is None else text.splitlines(keepends=True)
    if h is None:
        return []  # deleted
    if not _plain(h):
        raise NoDelta(f"{rel} is not a plain file")
    if reviewed:
        text = _snap_text(run_dir, old_id, rel, h)
    else:
        try:
            p = root / rel
            text = p.read_text(encoding="utf-8", errors="replace") if file_sha256(p) == h else None
        except OSError:
            text = None
    if text is None:
        raise NoDelta(f"cannot prove the content of {rel}")
    return text.splitlines(keepends=True)


def _side_size(root: Path, run_dir: Path, old_id: str, rel: str, a_hash: Any, b_hash: Any) -> int:
    """Bytes on both sides of a file's diff, from stat alone: a huge file is refused before it is read."""
    total = 0
    for p, h in ((snapshot_dir(run_dir, old_id) / "files" / rel, a_hash), (root / rel, b_hash)):
        if _plain(h):
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


class NoDelta(Exception):
    """The inter-diff cannot be proven; review the whole candidate instead."""


def inter_diff(
    root: Path, run_dir: Path, old_id: str, old: dict[str, Any], new: dict[str, Any]
) -> tuple[str, list[str]]:
    """Unified diff from a reviewed candidate to the current one, and the paths it touches."""
    if old.get("base_revision") != new.get("base_revision"):
        raise NoDelta("the base revision changed")
    if not (snapshot_dir(run_dir, old_id) / ".complete").exists():
        raise NoDelta("no snapshot of the reviewed candidate")
    base = new.get("base_revision")
    old_files, new_files = old.get("files", {}), new.get("files", {})
    out: list[str] = []
    touched: list[str] = []
    size = 0
    for rel in sorted(set(old_files) | set(new_files)):
        a_hash, b_hash = old_files.get(rel, "base"), new_files.get(rel, "base")
        if a_hash == b_hash:
            continue
        touched.append(rel)

        if size + _side_size(root, run_dir, old_id, rel, a_hash, b_hash) > 2 * MAX_DIFF_BYTES:
            raise NoDelta("the change since the last review is too large for a delta review")
        chunk = list(difflib.unified_diff(
            _side(root, run_dir, old_id, base, rel, a_hash, reviewed=True),
            _side(root, run_dir, old_id, base, rel, b_hash, reviewed=False),
            f"a/{rel} (reviewed)", f"b/{rel}",
        ))
        out += chunk
        size += sum(len(x.encode("utf-8")) for x in chunk)
        if size > MAX_DIFF_BYTES:
            raise NoDelta("the change since the last review is too large for a delta review")
    return "".join(out), touched
