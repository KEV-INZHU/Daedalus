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


def is_ancestor(root: Path, rev: str) -> bool:
    """True when `rev` is HEAD or one of its ancestors."""
    return _git(root, "merge-base", "--is-ancestor", rev, "HEAD", check=False).returncode == 0


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


def changed_paths(root: Path, base: str | None) -> list[str]:
    """Paths that differ from `base` in the working tree, untracked ones included; every tracked
    and untracked path when `base` is None."""
    return _changed_paths_git(root, base)


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
    """Human/agent-readable diff of the candidate against its base (for reviewers).

    Over `limit`, every file keeps a fair share instead of the tail being cut off: see `fit_diff`."""
    root = Path(root)
    chunks: list[tuple[str, str]] = []
    base = manifest.manifest["base_revision"]
    tracked: set[str] = set()
    if base and is_git_repo(root):
        paths = list(manifest.changed_paths)
        if paths:
            # The user's diff settings (no prefixes, colour, external tools) must not change what is parsed.
            r = _git(root, "diff", "--relative", "--no-renames", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                     "--dst-prefix=b/", base, "--", *paths, check=False)
            chunks += split_diff(r.stdout.decode("utf-8", "replace"))
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
            chunks.append((rel, f"--- /dev/null\n+++ b/{rel}  (new file)\n"
                           + "".join("+" + ln + "\n" for ln in body.splitlines())))
    return fit_diff(chunks, limit)


def split_diff(text: str) -> list[tuple[str, str]]:
    """A `git diff` as (path, that file's part), in order."""
    out: list[tuple[str, str]] = []
    for part in ("\n" + text).split("\ndiff --git ")[1:]:
        lines = part.split("\n")
        header = []
        for ln in lines[1:]:
            if ln.startswith("@@"):
                break  # past the header: content lines can start with --- or +++ too
            header.append(ln)
        path = next((_diff_path(ln[4:]) for ln in header if ln.startswith("+++ ") and ln[4:] != "/dev/null"), None) \
            or next((_diff_path(ln[4:]) for ln in header if ln.startswith("--- ") and ln[4:] != "/dev/null"), None)
        if path is None:  # no content lines (a mode change, a binary file): the header names it
            path = _header_path(lines[0])
        out.append((path, "diff --git " + part.rstrip("\n")))
    if not out and text.strip():
        out.append(("", text.rstrip("\n")))
    return out


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(name: str) -> str:
    """Git's C-style quoting (`"caf\\303\\251.py"`) undone: octal escapes are UTF-8 bytes."""
    out = bytearray()
    i, s = 0, name[1:-1]
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            octal = s[i + 1:i + 4]
            if len(octal) == 3 and all(x in "01234567" for x in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            out.append(_ESCAPES.get(n, ord(n) if ord(n) < 128 else 63))
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _diff_path(name: str) -> str:
    """A ---/+++ name as a path: git ends a name holding spaces with a tab, and quotes unusual ones."""
    name = name[:-1] if name.endswith("\t") else name
    if len(name) >= 2 and name[0] == name[-1] == '"':
        name = _unquote(name)
    return name[2:] if name[:2] in ("a/", "b/") else name


def _header_path(head: str) -> str:
    """The b/ path of a `diff --git` header line (after the prefix), quoted or not."""
    if head.endswith('"'):
        start = head.rfind(' "', 0, len(head) - 1)
        if start != -1:
            return _diff_path(head[start + 1:])
    return head.rsplit(" b/", 1)[-1]


MIN_SHARE = 400  # below this a file's share shows nothing useful: list it instead


def fit_diff(chunks: list[tuple[str, str]], limit: int) -> str:
    """Join per-file diffs within `limit` characters. If they do not fit, small files are kept
    whole and the space left is split evenly among the larger ones, each cut at a hunk boundary
    where possible. Every cut or omitted file is named at the end, so a reviewer knows what it did
    not see (and can read it from the repository) rather than mistaking a partial diff for the whole."""
    total = sum(len(c) for _, c in chunks) + len(chunks)
    if total <= limit:
        return "\n".join(c for _, c in chunks)
    budget = max(0, limit - 400 - 2 * max(200, limit // 8))  # room for the notes below
    order = sorted(range(len(chunks)), key=lambda i: len(chunks[i][1]))
    kept: dict[int, str] = {}
    cut: list[str] = []
    omitted: list[str] = []
    left = budget
    for n, i in enumerate(order):
        path, body = chunks[i]
        share = left // (len(order) - n)
        if len(body) <= share:
            kept[i] = body
            left -= len(body) + 1  # and the newline that joins it to the next
            continue
        if share < MIN_SHARE:
            omitted.append(f"{path or '(diff)'} ({len(body)} chars)")
            continue
        label = path if len(path) <= 120 else "..." + path[-117:]
        marker = f"\n... {label}: shown {{}} of {len(body)} chars; read the file for the rest ..."
        room = share - len(marker) - 8
        if room < MIN_SHARE // 2:  # a long name leaves no useful room: list the file instead
            omitted.append(f"{path or '(diff)'} ({len(body)} chars)")
            continue
        head = body[:room]
        at = head.rfind("\n@@")
        if at < room // 2:  # no hunk boundary in the back half: cut at a line instead
            at = head.rfind("\n")
        head = head[: at if at > 0 else room]
        kept[i] = head + marker.format(len(head))
        cut.append(path)
        left -= len(kept[i]) + 1
    text = "\n".join(kept[i] for i in range(len(chunks)) if i in kept)
    notes = [f"... diff over {limit} chars: every file kept a share instead of the end being dropped."]
    room = max(200, limit // 8)  # the lists of names may take at most this much each
    if cut:
        notes.append("Cut short (read these in the repository before judging them): " + _names(cut, room))
    if omitted:
        notes.append("Not shown at all (read these in the repository): " + _names(omitted, room))
    tail = "\n" + "\n".join(notes)
    if len(text) + len(tail) > limit:  # only when the notes ran long: say so rather than trim silently
        stop = "\n... (the diff above is cut short here to fit the notes below) ..."
        room = limit - len(tail) - len(stop)
        if room <= 0:
            return tail.lstrip("\n")[:limit]  # nothing of the diff fits: the notes alone
        text = text[:room] + stop
    return text + tail


def _names(paths: list[str], room: int) -> str:
    """As many of `paths` as fit in `room` characters, then how many more there are."""
    out: list[str] = []
    used = 0
    for p in paths:
        if out and used + len(p) + 2 > room:
            break
        out.append(p[:room])
        used += len(p) + 2
    rest = len(paths) - len(out)
    return ", ".join(out) + (f", and {rest} more" if rest else "")
