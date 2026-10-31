"""`daedalus plan-review`: have an independent reviewer read a plan before any code is written.

    daedalus plan-review "Add a channel module; register agents in Ariadne; ..."
    daedalus plan-review --file PLAN.md

The reviewer runs read-only through the repository's adapter and sees the contract and the plan.
Its concerns are advice: nothing is recorded as a review or a finding (only the session's cost),
so an agent may ask for one of its own plan. `plan_review: true` in .daedalus.yml makes
`daedalus run` do this before every first build round.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from daedalus.cli._common import _ari, _print, _run_id, add_command
from daedalus.core.errors import DaedalusError


def cmd_plan_review(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import review_plan

    if bool(args.plan) == bool(args.file):
        raise DaedalusError("give the plan as text or with --file, not both")
    plan = Path(args.file).read_text(encoding="utf-8") if args.file else args.plan
    ari = _ari(args, adapter=True)
    try:
        if ari.adapter is None:
            raise DaedalusError("no adapter configured to launch a reviewer (`adapter:` in .daedalus.yml)")
        rid = _run_id(ari, args)
        tag = str(
            sum(1 for s in ari.state(rid).sessions if str(s.get("task_id", "")).startswith("plan-review")) + 1
        )
        review = review_plan(ari, rid, plan, ari.adapter, _print, tag=tag)
        if review is None:
            _print("the plan review gave nothing usable; nothing was recorded")
            return 1
        if args.json:
            _print(json.dumps(review, indent=1))
            return 0
        if review["summary"]:
            _print(review["summary"])
        for c in review["concerns"]:
            _print(f"[{c['severity']}] {c['title']}" + (f": {c['detail']}" if c["detail"] else ""))
        if not review["concerns"]:
            _print("no concerns")
        return 0
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(
        sub,
        "plan-review",
        cmd_plan_review,
        "have a reviewer read a plan before any code is written",
        run=True,
    )
    sp.add_argument("plan", nargs="?", help="the plan, as text")
    sp.add_argument("--file", help="read the plan from this file")
    sp.add_argument("--json", action="store_true")
