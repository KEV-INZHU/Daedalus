"""Human decisions: approve/deny, attest, amend, risk-exception, resolve, abandon, blocker."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

import yaml

from daedalus.cli._common import (
    EXIT,
    _ari,
    _print,
    _run_id,
    add_command,
    human_actor,
    starter_actor,
)
from daedalus.core.errors import DaedalusError


def cmd_abandon(args: argparse.Namespace) -> int:
    actor = human_actor("declaring this run unresolvable (REJECTED)", assume_yes=args.yes)
    ari = _ari(args)
    try:
        d = ari.abandon(_run_id(ari, args), actor=actor, reason=args.reason)
        _print(f"Disposition: {d.disposition.value}")
        return EXIT[d.disposition]
    finally:
        ari.close()


def cmd_approve(args: argparse.Namespace) -> int:
    verb = "deny" if args.cmd == "deny" else "approve"
    actor = human_actor(f"{verb} `{args.action}` for the current candidate", assume_yes=args.yes)
    ari = _ari(args)
    try:
        rid = _run_id(ari, args, any_run=True)
        if verb == "deny":
            rec = ari.deny(rid, args.action, actor=actor, rationale=args.rationale or "", interactive=True)
        else:
            rec = ari.approve(
                rid,
                args.action,
                actor=actor,
                expires_in=args.expires,
                single_use=not args.reusable,
                rationale=args.rationale or "",
                interactive=True,
            )
        until = f", expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.expires_at))}" if rec.expires_at else ""
        _print(f"{rec.decision} `{rec.action}` for candidate {rec.candidate_id[:12]} ({rec.approval_id}{until})")
        return 0
    finally:
        ari.close()


def cmd_attest(args: argparse.Namespace) -> int:
    actor = human_actor(f"attesting {args.criterion} {args.status.upper()}", assume_yes=args.yes)
    ari = _ari(args)
    try:
        ari.attest(_run_id(ari, args), args.criterion, args.status, actor=actor, evidence=args.evidence, interactive=True)
        _print(f"{args.criterion}: {args.status.upper()} recorded")
        return 0
    finally:
        ari.close()


def cmd_amend(args: argparse.Namespace) -> int:
    changes: dict[str, Any] = {}
    if args.file:
        loaded = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise DaedalusError("amendment file must be a mapping")
        changes.update(loaded)
    for item in args.set or ():
        key, sep, value = item.partition("=")
        if not sep:
            raise DaedalusError(f"--set expects key=value, got {item!r}")
        changes[key.strip()] = yaml.safe_load(value)
    if not changes:
        raise DaedalusError("nothing to amend: use --set key=value or --file")
    actor = human_actor(f"amending the contract ({', '.join(changes)})", assume_yes=args.yes)
    ari = _ari(args)
    try:
        new = ari.amend(_run_id(ari, args), changes, actor=actor, rationale=args.rationale, interactive=True)
        _print(f"contract v{new.contract_version}; evidence and reviews for the previous version are now stale")
        return 0
    finally:
        ari.close()


def cmd_risk_exception(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        decision = ari.evaluate(rid)
        shown = decision.integrity
        what = f"lowering risk to `{args.tier}` for the current candidate"
        flagged = shown is not None and shown.flagged
        if flagged:
            what += f", and acknowledging these test-integrity signals: {shown.summary()}"
            _print(f"test-integrity signals this exception would acknowledge: {shown.summary()}")  # shown even with -y
        actor = human_actor(what, assume_yes=args.yes)
        ari.risk_exception(
            rid, args.tier, actor=actor, rationale=args.rationale, interactive=True,
            expected_candidate=decision.candidate_id,
            expected_integrity=tuple(shown.digests()) if shown is not None else (),
        )
        _print(f"risk exception to `{args.tier}` recorded for the current candidate")
        if flagged:
            _print(f"it acknowledges the test-integrity signals: {shown.summary()}")
        return 0
    finally:
        ari.close()


def cmd_resolve(args: argparse.Namespace) -> int:
    actor = human_actor(f"resolving {args.item}", assume_yes=args.yes)
    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        kind = args.item[:1].upper()
        if kind == "F":
            ari.resolve_finding(rid, args.item, actor=actor, note=args.note or "")
        elif kind == "B":
            ari.resolve_blocker(rid, args.item, actor=actor)
        elif kind == "V":
            ari.resolve_violation(rid, args.item, actor=actor, note=args.note or "")
        else:
            raise DaedalusError("item must be a finding (F…), blocker (B…) or violation (V…) id")
        _print(f"{args.item} resolved")
        return 0
    finally:
        ari.close()


def cmd_blocker(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        bid = ari.raise_blocker(_run_id(ari, args), args.description, actor=starter_actor())
        _print(f"blocker {bid} raised")
        return 0
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(sub, "abandon", cmd_abandon, "declare the run unresolvable: REJECTED (human only)",
                     run=True, human=True)
    sp.add_argument("--reason", required=True)
    for name in ("approve", "deny"):
        sp = add_command(sub, name, cmd_approve, f"{name} a restricted action for the current candidate (human only)",
                         run=True, human=True)
        sp.add_argument("action")
        sp.add_argument("--rationale")
        if name == "approve":
            sp.add_argument("--expires", type=float, help="seconds until the approval expires")
            sp.add_argument("--reusable", action="store_true", help="allow more than one use")
    sp = add_command(sub, "attest", cmd_attest, "attest an acceptance criterion that has no check (human only)",
                     run=True, human=True)
    sp.add_argument("criterion")
    sp.add_argument("status", choices=("pass", "fail", "PASS", "FAIL"))
    sp.add_argument("--evidence", required=True)
    sp = add_command(sub, "amend", cmd_amend, "version the contract (human only)", run=True, human=True)
    sp.add_argument("--set", action="append", help="field=YAML value (repeatable)")
    sp.add_argument("--file", help="YAML file of changed fields")
    sp.add_argument("--rationale", required=True)
    sp = add_command(sub, "risk-exception", cmd_risk_exception, "lower the effective risk tier (human only)",
                     run=True, human=True)
    sp.add_argument("tier")
    sp.add_argument("--rationale", required=True)
    sp = add_command(sub, "resolve", cmd_resolve, "resolve a finding, blocker or violation (human only)",
                     run=True, human=True)
    sp.add_argument("item")
    sp.add_argument("--note")
    sp = add_command(sub, "blocker", cmd_blocker, "raise a blocker on the open run", run=True)
    sp.add_argument("description")
