"""`daedalus propose` and `daedalus confirm`: a contract worth holding the work to.

A run opened by the harness starts from the raw prompt and one generic criterion. The agent, which
has read the request, proposes a short objective and testable criteria; a person confirms them (or
rejects them) with one command. Proposing changes nothing: confirming is a contract amendment, so
it needs `contract_change` authority and makes earlier evidence and reviews stale.
"""

from __future__ import annotations

import argparse
from typing import Any

from daedalus.cli._common import _ari, _print, _run_id, add_command, human_actor, starter_actor
from daedalus.core.errors import DaedalusError


def show_proposal(p: dict[str, Any]) -> list[str]:
    lines = [
        f"Proposed contract {p['proposal_id']} (from {p['proposed_by']}):",
        f"  Objective: {p['objective']}",
    ]
    lines += [f"  - {c}  (proved by the required checks)" for c in p["criteria"]]
    lines += [f"  - {c}  (a person attests it)" for c in p["attest"]]
    if p.get("rationale"):
        lines.append(f"  Why: {p['rationale']}")
    return lines


def cmd_propose(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        pid = ari.propose_contract(
            rid,
            objective=args.objective,
            criteria=args.criterion or (),
            attest=args.attest or (),
            rationale=args.why or "",
            actor=starter_actor(),
        )
        for line in show_proposal(ari.state(rid).proposals[pid] | {"proposal_id": pid}):
            _print(line)
        _print(f"A person applies it with `daedalus confirm {pid}` (or `daedalus confirm --reject`).")
        return 0
    finally:
        ari.close()


def cmd_confirm(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        pending = ari.state(rid).pending_proposal
        if pending is None:
            raise DaedalusError("no contract proposal is waiting")
        for line in show_proposal(pending):
            _print(line)
        # The proposal shown is the one decided: a newer one proposed meanwhile is refused, not applied.
        pid = args.proposal or pending["proposal_id"]
        verb = "rejecting" if args.reject else "confirming"
        actor = human_actor(f"{verb} proposal {pid}", assume_yes=args.yes)
        if args.reject:
            ari.reject_proposal(rid, pid, actor=actor, reason=args.reason or "", interactive=True)
            _print("rejected; the contract is unchanged")
        else:
            new = ari.confirm_proposal(rid, pid, actor=actor, interactive=True)
            _print(f"contract v{new.contract_version}: earlier evidence and reviews are now stale")
        return 0
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(
        sub, "propose", cmd_propose, "propose the run's objective and acceptance criteria", run=True
    )
    sp.add_argument("objective", help="what this run must achieve, in one or two sentences")
    sp.add_argument(
        "-c", "--criterion", action="append", help="a criterion the required checks prove (repeatable)"
    )
    sp.add_argument("--attest", action="append", help="a criterion only a person can attest (repeatable)")
    sp.add_argument("--why", help="why the contract should change")
    sp = add_command(
        sub,
        "confirm",
        cmd_confirm,
        "apply (or --reject) the proposed contract (human only)",
        run=True,
        human=True,
    )
    sp.add_argument("proposal", nargs="?", help="proposal id (default: the pending one)")
    sp.add_argument("--reject", action="store_true")
    sp.add_argument("--reason")
