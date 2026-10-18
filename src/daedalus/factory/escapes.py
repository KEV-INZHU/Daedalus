"""Escaped defects: shipped orders that a later commit says it fixes.

A commit on the integration branch whose message carries a `Fixes-Order: <id>` trailer marks that
shipped order as escaped. Escapes are derived from git history on demand, never stored in the ledger.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.factory import gitops
from daedalus.factory.ledger import OrderRecord

TRAILER = "Fixes-Order"
_MARK = "\x1e"


def escaped_orders(root: Path, recs: dict[str, OrderRecord]) -> list[str]:
    """Ids of shipped orders named by a `Fixes-Order:` trailer on a commit made after they shipped."""
    by_branch: dict[str, list[OrderRecord]] = {}
    for r in recs.values():
        if r.status == "shipped" and r.commit and r.into:
            by_branch.setdefault(r.into, []).append(r)
    escaped: set[str] = set()
    for branch, shipped in by_branch.items():
        log = gitops.git(
            root, "log", f"--format={_MARK}%H%n%(trailers:key={TRAILER},valueonly,unfold)", branch, "--",
            check=False, raw=True,
        )
        fixes: dict[str, set[str]] = {}  # order id -> commits whose trailers name it
        for chunk in log.split(_MARK)[1:]:
            lines = chunk.splitlines()
            for v in lines[1:]:
                if v.strip():
                    fixes.setdefault(v.strip(), set()).add(lines[0].strip())
        for r in shipped:
            named = fixes.get(r.order_id)
            if not named:
                continue
            later = set(gitops.git(root, "rev-list", f"{r.commit}..{branch}", check=False).split())
            if named & later:
                escaped.add(r.order_id)
    return sorted(escaped)
