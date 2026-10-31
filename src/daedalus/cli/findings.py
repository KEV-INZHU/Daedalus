"""`daedalus findings`: review findings as a worklist the agent works through.

    daedalus findings                         the run's open findings, one per line
    daedalus findings --all                   resolved ones too
    daedalus findings answer F3 --fixed "…"   say what changed; the next review checks the claim
    daedalus findings answer F3 --dispute "…" contest it with evidence; `daedalus arbitrate F3` rules
    daedalus findings judge F3 invalid "…"    a person's verdict, for `daedalus calibration`; gates nothing

Answering resolves nothing. Only the perspective that raised a finding (in a later review) or a
person with `attest` authority resolves it.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from daedalus.cli._common import _ari, _print, _run_id, add_command, display_name, human_actor, starter_actor
from daedalus.core.errors import DaedalusError


def finding_status(f: Any) -> str:
    if f.resolved_by is not None:
        return f"resolved by {f.resolved_by}"
    if f.ruling:
        return f"ruled: {f.ruling.get('decision', '?')}"
    if f.disputes:
        return "disputed"
    if f.answers:
        return "answered: fixed, awaiting re-review"
    return "open"


def row(f: Any) -> dict[str, Any]:
    return {
        "id": f.finding_id,
        "severity": f.severity.value,
        "review": display_name(f.perspective),
        "title": f.title,
        "required_change": f.required_change,
        "status": finding_status(f),
        "answers": f.answers,
        "disputes": f.disputes,
        "confidence": f.confidence,
        "against": f.against,
        "failing_test": f.failing_test,
        "judgment": f.judgment,
    }


def cmd_findings(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = _run_id(ari, args, any_run=True)
        if args.findings_cmd == "judge":
            actor = human_actor(f"judging {args.finding} {args.verdict}", assume_yes=True)
            ari.judge_finding(rid, args.finding, actor=actor, verdict=args.verdict, note=args.note or "")
            _print(f"{args.finding}: judged {args.verdict}; `daedalus calibration` scores the reviewer with it")
            return 0
        if args.findings_cmd == "answer":
            if bool(args.fixed) == bool(args.dispute):
                raise DaedalusError("answer with exactly one of --fixed or --dispute")
            actor = starter_actor()
            if args.fixed:
                ari.answer_finding(rid, args.finding, actor=actor, note=args.fixed)
                _print(f"{args.finding}: noted as fixed; the next review of that perspective checks it")
            else:
                ari.dispute(rid, args.finding, actor=actor, reason=args.dispute)
                _print(
                    f"{args.finding}: disputed; `daedalus arbitrate {args.finding}` has the arbiter rule on it"
                )
            return 0
        state = ari.state(rid)
        rows = [row(f) for f in state.findings.values() if args.all or f.resolved_by is None]
        if args.json:
            _print(json.dumps(rows, indent=1))
        elif not rows:
            _print("no open findings" if not args.all else "no findings")
        else:
            for r in rows:
                conf = f", {r['confidence']:.0%} sure" if r["confidence"] is not None else ""
                _print(f"{r['id']} [{r['severity']}] {r['title']}  ({r['review']}{conf}; {r['status']})")
                if r["required_change"]:
                    _print(f"    change: {r['required_change']}")
        return 0
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(sub, "findings", cmd_findings, "list review findings and answer them", run=True)
    sp.add_argument("--all", action="store_true", help="include resolved findings")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(findings_cmd="list")
    cmds = sp.add_subparsers(dest="findings_cmd")
    p = cmds.add_parser(
        "answer", help="say a finding is fixed (with what changed) or dispute it (with evidence)"
    )
    p.add_argument("finding")
    p.add_argument("--fixed", metavar="NOTE")
    p.add_argument("--dispute", metavar="EVIDENCE")
    j = cmds.add_parser(
        "judge", help="say whether a finding was real (valid) or a false alarm (invalid); scores reviewers only"
    )
    j.add_argument("finding")
    j.add_argument("verdict", choices=("valid", "invalid"))
    j.add_argument("note", nargs="?", help="why, e.g. 'the test was already failing on main'")
