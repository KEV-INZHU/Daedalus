"""`daedalus next`: one line saying what the run needs now, and from whom.

The order is who can move the run soonest: the agent's own fixes first, then what Daedalus runs by
itself (checks, launched reviews), then decisions only a person can make. `daedalus status` has the
full picture.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from daedalus.cli._common import _ari, _print, _run_id, add_command
from daedalus.core.errors import DaedalusError
from daedalus.core.state_machine import ExecutionState

ORDER = ("agent", "system", "human")


def next_step(ari: Any, run_id: str | None) -> dict[str, Any]:
    if run_id is None:
        return {
            "who": "agent",
            "action": 'no run is open: your first file edit opens one (or `daedalus start "goal"`)',
            "more": 0,
        }
    state = ari.state(run_id)
    if state.execution is ExecutionState.FINISHED:
        d = state.disposition.value if state.disposition else "FINISHED"
        return {"who": "nobody", "action": f"run {run_id} is {d}; the next edit opens a new run", "more": 0}
    decision = ari.evaluate(run_id)
    items: list[tuple[str, str]] = []
    unanswered = [f for f in state.open_findings if not f.answers and not f.disputes]
    if unanswered:
        f = unanswered[0]
        items.append(
            (
                "agent",
                f"answer finding {f.finding_id} ({f.title}): fix it and "
                f'`daedalus findings answer {f.finding_id} --fixed "..."`, or `--dispute`',
            )
        )
    for r in decision.reasons:
        if r.code.startswith("finding:") and unanswered:
            continue  # the worklist line above says it more usefully
        items.append((r.fixable_by, r.message))
    if state.pending_proposal is not None:
        items.append(
            (
                "human",
                f"confirm the proposed contract {state.pending_proposal['proposal_id']}: "
                "`daedalus confirm` (or `--reject`)",
            )
        )
    if not items:
        return {"who": "nobody", "action": "nothing is pending", "more": 0}
    items.sort(key=lambda x: ORDER.index(x[0]) if x[0] in ORDER else len(ORDER))
    who, action = items[0]
    return {"who": who, "action": action, "more": len(items) - 1}


def cmd_next(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        try:
            rid = _run_id(ari, args)
        except DaedalusError:
            rid = None
        step = next_step(ari, rid)
        if args.json:
            _print(json.dumps({"run": rid, **step}))
        else:
            more = f"  (+{step['more']} more: `daedalus status`)" if step["more"] else ""
            _print(f"{step['who']}: {step['action']}{more}")
        return 0
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(sub, "next", cmd_next, "one line: what the run needs now, and from whom", run=True)
    sp.add_argument("--json", action="store_true")
