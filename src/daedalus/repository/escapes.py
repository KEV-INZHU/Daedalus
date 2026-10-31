"""Escaped defects: accepted runs that a later commit says it fixes.

A commit in HEAD's history whose message carries a `Fixes-Run: <run id>` trailer (the full id, or a
unique prefix of at least 8 characters) marks that accepted run as escaped: the gate accepted work
that needed fixing afterwards. Derived from git history on demand, never stored in the audit log.

When the run's own accepted commit is known (its `Daedalus-Run:` trailer), only a fix made after it
counts: a commit cannot fix a defect that did not exist yet. Factory orders use `Fixes-Order:`
(factory/escapes.py) the same way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

TRAILER = "Fixes-Run"
PROVENANCE = "Daedalus-Run"
MIN_PREFIX = 8
_MARK = "\x1e"


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else ""


def _trailers(root: Path, key: str) -> dict[str, set[str]]:
    """Trailer value -> commits (in HEAD's history) that carry it."""
    log = _git(root, "log", f"--format={_MARK}%H%n%(trailers:key={key},valueonly,unfold)", "HEAD", "--")
    out: dict[str, set[str]] = {}
    for chunk in log.split(_MARK)[1:]:
        lines = chunk.splitlines()
        for v in lines[1:]:
            if v.strip():
                out.setdefault(v.strip(), set()).add(lines[0].strip())
    return out


def _is_ancestor(root: Path, a: str, b: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", a, b], capture_output=True, check=False
        ).returncode
        == 0
    )


def escaped_runs(root: Path, accepted: list[str]) -> dict[str, list[str]]:
    """Accepted run id -> the commits that say they fix it. Runs nobody fixed are left out."""
    fixes = _trailers(root, TRAILER)
    if not fixes:
        return {}
    shipped = _trailers(root, PROVENANCE)
    out: dict[str, list[str]] = {}
    for value, commits in fixes.items():
        if len(value) < MIN_PREFIX:
            continue
        matches = [rid for rid in accepted if rid.startswith(value)]
        if len(matches) != 1:
            continue  # unknown here, or ambiguous: never guessed
        rid = matches[0]
        own = shipped.get(rid, set())
        later = [
            c for c in commits if c not in own and (not own or any(_is_ancestor(root, o, c) for o in own))
        ]
        if later:
            out.setdefault(rid, []).extend(sorted(later))
    return {rid: sorted(set(cs)) for rid, cs in out.items()}
