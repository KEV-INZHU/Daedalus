"""Review commands: launch a review, dispute a finding, arbitrate it."""

from __future__ import annotations

import argparse
from typing import Any

from daedalus.cli._common import (
    _ari,
    _print,
    _run_id,
    add_command,
    starter_actor,
)


def cmd_review(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import launch_required_reviews, launch_review

    ari = _ari(args, adapter=True)
    ari.no_cache = args.no_cache
    try:
        rid = _run_id(ari, args)
        note = lambda m: _print(f"· {m}")
        if args.perspective == "required":
            ids = launch_required_reviews(ari, rid, ari.adapter, note)
        else:
            one = launch_review(ari, rid, args.perspective, ari.adapter, note)
            ids = [one] if one else []
        state = ari.state(rid)
        for f in state.findings.values():
            if any(r.review_id == f.review_id for r in state.reviews if r.review_id in ids):
                _print(f"  {f.finding_id} [{f.severity.value}] {f.title}")
        return 0 if ids else 1
    finally:
        ari.close()


def cmd_dispute(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        ari.dispute(_run_id(ari, args), args.finding, actor=starter_actor(), reason=args.reason)
        _print(f"{args.finding} disputed; run `daedalus arbitrate {args.finding}` to have Plato rule on it")
        return 0
    finally:
        ari.close()


def cmd_arbitrate(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import arbitrate

    ari = _ari(args, adapter=True)
    try:
        decision = arbitrate(ari, _run_id(ari, args), args.finding, ari.adapter, lambda m: _print(f"· {m}"))
        f = ari.state(_run_id(ari, args, any_run=True)).findings[args.finding]
        if decision:
            _print(f"{args.finding}: {decision} — {(f.ruling or {}).get('rationale', '')}")
            if f.resolved_by:
                _print(f"{args.finding} resolved by arbitration (policy `arbitration_resolves_findings`)")
        return 0 if decision else 1
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(sub, "review", cmd_review, "launch an independent review through the adapter", run=True)
    sp.add_argument("perspective", choices=("required", "socrates", "mozi", "aristotle", "james"))
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
    sp.add_argument("--no-cache", action="store_true", help="launch the review even if an identical one is on record")
    sp = add_command(sub, "dispute", cmd_dispute, "contest an open review finding with a reason", run=True)
    sp.add_argument("finding")
    sp.add_argument("--reason", required=True)
    sp = add_command(sub, "arbitrate", cmd_arbitrate,
                     "have Plato rule on a disputed finding (launched via the adapter)", run=True)
    sp.add_argument("finding")
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
