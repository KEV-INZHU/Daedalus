"""`daedalus risk`: what a change will need, before any run judges it.

It applies the repository's policy (risk floors and the test-integrity scan) to the uncommitted
changes, or to paths you name, and says which floors fire, which files fire them, and what that
tier (and the configured mode) requires. Same rules as the gate and `daedalus ci`; it records nothing.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from daedalus.cli._common import _print, add_command, review_blocker
from daedalus.core.config import find_root, load_config
from daedalus.core.contracts import MODE_REVIEWS
from daedalus.core.errors import DaedalusError
from daedalus.core.names import display_name
from daedalus.core.paths import match_any
from daedalus.core.risk import assess


def preview(root: Path, paths: list[str] | None = None, base_dir: Path | None = None) -> dict[str, Any]:
    from daedalus.repository.candidate import changed_paths, git_head, is_git_repo

    cfg = load_config(root)
    policy = cfg.policy
    named = paths is not None
    base = None
    missing: list[str] = []
    excluded: list[str] = []
    if named:
        wanted: set[str] = set()
        for given in paths or []:
            rel = _repo_path(root, given, base_dir or Path.cwd())
            if rel and not (root / rel).exists() and not (root / rel).is_symlink():
                missing.append(rel)  # assessed anyway: it may be a deletion
            wanted.update(_expand(root, rel))
        excluded = sorted(p for p in wanted if match_any(p, policy.candidate_exclude))  # never in a candidate
        changed = sorted(wanted - set(excluded))
    else:
        if not is_git_repo(root):
            raise DaedalusError("not a git repository: name the paths to assess (`daedalus risk PATH ...`)")
        base = git_head(root)  # None before the first commit: then every file counts as changed
        changed = [p for p in changed_paths(root, base) if not match_any(p, policy.candidate_exclude)]
    flagged: tuple[str, ...] = ()
    signals: list[str] = []
    if base is not None and policy.test_integrity.enabled and changed:
        from daedalus.verification.test_integrity import analyze, check_runner_specs

        files = {p: _hash(root, p) for p in changed}
        scan = analyze(root, base, files, policy.test_integrity.test_globs,
                       check_runner_specs(policy.checks[c] for c in policy.mandatory_checks()))
        flagged, signals = scan.paths, [f"{s.path}: {s.detail}" for s in scan.signals]
    risk = assess(policy, changed, policy.tiers[0], test_integrity_paths=flagged)
    req = policy.requirement(risk.effective_tier)
    blocked = review_blocker(root)
    floors = [{"source": c.source, "floor": c.name, "tier": c.tier, "path": c.path}
              for c in risk.contributions if c.source != "declared"]
    return {
        "named": named,
        "missing": missing,
        "excluded": excluded,
        "changed": changed,
        "tier": risk.effective_tier,
        "floors": floors,
        "test_signals": signals,
        "test_scan": "skipped: nothing to compare with before the first commit" if not named and base is None else "",
        "mode": cfg.mode,
        "reviews": list(dict.fromkeys(req.reviews + MODE_REVIEWS.get(cfg.mode, ()))),
        "independent": req.independent_review,
        "approvals": list(req.approvals),
        "reviews_launch": blocked is None,
        "reviews_blocked": blocked,
    }


def _hash(root: Path, rel: str) -> str | None:
    """The content hash the gate's candidate would record (symlinks by their target text)."""
    from daedalus.repository.candidate import _hash_entry

    h = _hash_entry(root, rel, [])
    return None if h in (None, "unreadable") else h


def _repo_path(root: Path, path: str, base: Path) -> str:
    """A path given on the command line, as a repository path. Relative paths are taken from `base`:
    the `-C` directory when one is given (as `git -C` does), else where you are."""
    if not path.strip():
        raise DaedalusError("an empty path names nothing: give a file or directory")
    top = root.resolve()
    # absolute + normpath, not resolve: a symlink is judged by where it sits, as the candidate sees it
    full = Path(os.path.normpath(base.resolve() / path))
    try:
        rel = full.relative_to(top).as_posix()
    except ValueError:
        raise DaedalusError(f"{path} is outside the repository at {root}") from None
    return "" if rel == "." else rel


def _expand(root: Path, rel: str) -> list[str]:
    """A named directory as the files under it (tracked and untracked, git's ignores applied), so a
    floor on a file inside it fires; a file, or a path that is not there (a deletion), as itself."""
    p = root / rel if rel else root
    if not p.is_dir() or p.is_symlink():
        return [rel] if rel else []
    from daedalus.repository.candidate import is_git_repo

    if is_git_repo(root):
        import subprocess

        out = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z", "--", rel or "."],
                             capture_output=True, check=False).stdout.decode("utf-8", "replace")
        return [x for x in out.split("\0") if x]
    return sorted(Path(dp, f).relative_to(root).as_posix() for dp, _, fs in os.walk(p) for f in fs)


def render(p: dict[str, Any]) -> list[str]:
    notes = [f"  not in the working tree (assessed as a deletion): {m}" for m in p.get("missing", [])]
    if p.get("excluded"):
        notes.append(f"  skipped {len(p['excluded'])} path(s) that are never part of a candidate (e.g. {p['excluded'][0]})")
    if not p["changed"]:
        return [f"tier {p['tier']}: " + ("no files to assess" if p["named"] else "nothing has changed since HEAD")] + notes
    lines = [f"tier {p['tier']} for {len(p['changed'])} {'' if p['named'] else 'changed '}file(s)"] + notes
    by_floor: dict[tuple[str, str, str], list[str]] = {}
    for f in p["floors"]:
        by_floor.setdefault((f["source"], f["floor"], f["tier"]), []).append(f["path"] or "")
    for (source, floor, tier), where in by_floor.items():
        shown = ", ".join(w for w in where[:3] if w) + (f" (+{len(where) - 3} more)" if len(where) > 3 else "")
        lines.append(f"  {source} floor `{floor}` -> {tier}" + (f": {shown}" if shown else ""))
    if not p["floors"]:
        lines.append("  no risk floor fires")
    lines += [f"  weakened test: {s}" for s in p["test_signals"]]
    if p.get("test_scan"):
        lines.append(f"  (weakened-test scan {p['test_scan']})")
    if p["named"]:
        lines.append("  (named paths: risk floors only; run without paths to scan your test changes too)")
    if p["reviews"]:
        kind = "independent reviews" if p["independent"] else "reviews"
        lines.append(f"needs {kind}: " + ", ".join(display_name(r) for r in p["reviews"]))
        lines.append("  launched automatically once the checks pass" if p["reviews_launch"] else
                     f"  {p['reviews_blocked']}")
    if p["approvals"]:
        lines.append("needs human approval for: " + ", ".join(p["approvals"]))
    if not p["reviews"] and not p["approvals"]:
        lines.append("needs: the checks only")
    return lines


def cmd_risk(args: argparse.Namespace) -> int:
    root = find_root(args.dir)
    base = Path(args.dir) if getattr(args, "dir_given", False) else Path.cwd()  # as `git -C` takes paths
    p = preview(root, args.paths or None, base)
    _print(json.dumps(p, indent=2) if args.json else "\n".join(render(p)))
    return 0


def register(sub: Any) -> None:
    sp = add_command(sub, "risk", cmd_risk, "preview the risk tier of your changes and what it will require")
    sp.add_argument("paths", nargs="*", help="paths to assess instead of the uncommitted changes")
    sp.add_argument("--json", action="store_true")
