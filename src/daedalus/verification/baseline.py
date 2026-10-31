"""Was it already failing? Run a failed check once on a clean checkout of the base revision.

A required check that fails on the candidate may have been failing before the change. A reviewer that
only sees the diff cannot tell, so it blames the change (the commonest false alarm), and a builder
may chase a failure it did not cause. When a required check fails, Daedalus checks out the run's base
revision into a temporary git worktree, runs the same check there once, and records the result
(`baseline.checked`). Reviews and the block message then say "also fails on the base revision".

This is advice, never acceptance: the gate still requires every required check to pass on the
candidate. Results are cached per base revision and check definition, so runs that share a base pay
once. A clean checkout lacks untracked files (an `.env`, installed node modules), so a base failure
is reported as what it is: the check also fails on a clean checkout of the base.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from daedalus.core.policy import CheckDefinition
from daedalus.verification.check_runner import run_check


def _cache_file(cache_dir: Path, base: str, defn: CheckDefinition) -> Path:
    return cache_dir / f"{base}-{defn.check_id}-{defn.definition_hash[:16]}.json"


def run_on_base(root: Path, base: str, defn: CheckDefinition, log_dir: Path, cache_dir: Path) -> dict[str, Any]:
    """{result: PASS | FAIL | ERROR, exit_code, error} of `defn` on a clean checkout of `base`."""
    cached = _cache_file(cache_dir, base, defn)
    try:
        hit = json.loads(cached.read_text(encoding="utf-8"))
        if isinstance(hit, dict) and hit.get("result") in ("PASS", "FAIL", "ERROR"):
            return {**hit, "cached": True}
    except (OSError, ValueError):
        pass
    tmp = Path(tempfile.mkdtemp(prefix="daedalus-base-"))
    where = tmp / "tree"
    try:
        r = subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", "-q", str(where), base],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            return {"result": "ERROR", "exit_code": None, "error": f"cannot check out {base[:12]}: {r.stderr.strip()}"}
        # The base checkout's own code first, ahead of an editable install of the working tree's.
        path = os.pathsep.join([str(where / "src"), str(where)] + ([os.environ["PYTHONPATH"]]
                                                                     if os.environ.get("PYTHONPATH") else []))
        out = run_check(defn, where, log_dir, log_name=f"{defn.check_id}-base",
                        run_env={"DAEDALUS_BASE_REVISION": base, "PYTHONPATH": path})
        res = {"result": out.result, "exit_code": out.exit_code, "error": out.error, "log_path": out.log_path}
    finally:
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(where)],
                       capture_output=True, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["git", "-C", str(root), "worktree", "prune"], capture_output=True, check=False)
    if res["result"] != "ERROR":  # an error says nothing about the base; try again next time
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(res), encoding="utf-8")
        except OSError:
            pass
    return res


def base_note(entry: dict[str, Any] | None) -> str:
    """The sentence a failing check's reason gains, or ""."""
    if not entry or entry.get("result") != "FAIL":
        return ""
    log = f" (base log: {entry['log_path']})" if entry.get("log_path") else ""
    return (f" It also fails on a clean checkout of the base revision {str(entry.get('base_revision', ''))[:12]}"
            f"{log}, so the failure may predate this change: compare the two logs before blaming or clearing it. "
            "It must still pass before acceptance.")
