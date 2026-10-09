"""`daedalus` command line: the reference harness and lowest common denominator.

Day-to-day surface is three things: a toggle (`on`/`off`, or `init`), a
contract (`start "goal"` / `run "goal"`), and `status`. Everything else is for
humans deciding things (`approve`, `attest`, ...) or for digging into the audit
trail (`log`, `audit`).

Human-authority commands refuse to run inside an agent session or without an
interactive terminal. That is defence in depth, not a security boundary: see
docs/security-model.md.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from daedalus import __version__
from daedalus.adapters.base import AGENT_MARKER
from daedalus.core.acceptance import Decision
from daedalus.core.config import (
    CONFIG_FILE,
    STATE_DIR,
    detect_checks,
    find_root,
    gate_enabled,
    render_config,
    set_gate,
)
from daedalus.core.errors import AuthorizationError, DaedalusError
from daedalus.core.policy import default_policy_text
from daedalus.core.state_machine import Disposition

SEV_ABBR = {"BLOCKER": "B", "MAJOR": "M", "MINOR": "m", "ADVISORY": "A"}
EXIT = {Disposition.ACCEPTED: 0, Disposition.BLOCKED: 1, Disposition.REJECTED: 3, Disposition.CANCELLED: 4}


# ------------------------------------------------------------------ identity
def _interactive() -> bool:
    return sys.stdin.isatty()


def in_agent_session() -> bool:
    return bool(os.environ.get("CLAUDECODE") or os.environ.get(AGENT_MARKER))


def human_name() -> str:
    name = os.environ.get("DAEDALUS_USER")
    if not name:
        try:
            r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
            name = r.stdout.strip()
        except FileNotFoundError:
            name = ""
    name = name or getpass.getuser()
    return re.sub(r"[^A-Za-z0-9@._+-]", "_", name)


def human_actor(what: str, *, assume_yes: bool = False) -> str:
    """The acting human, after checking a human is plausibly at the keyboard."""
    if in_agent_session():
        raise AuthorizationError(f"`{what}` is a human decision; it cannot run inside an agent session")
    if not _interactive():
        raise AuthorizationError(f"`{what}` is a human decision and needs an interactive terminal")
    actor = f"human:{human_name()}"
    if not assume_yes:
        ans = input(f"{actor}: confirm {what}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            raise DaedalusError("not confirmed; nothing recorded")
    return actor


def starter_actor() -> str:
    """Starting a run is not a privileged decision; record who asked honestly."""
    return "agent:harness" if in_agent_session() else f"human:{human_name()}"


# ------------------------------------------------------------------- helpers
def _ari(args: argparse.Namespace, *, adapter: bool = False):
    from daedalus.adapters import make_adapter
    from daedalus.orchestration.ariadne import Ariadne

    root = find_root(args.dir)
    ad = None
    if adapter:
        from daedalus.core.config import load_config

        cfg = dict(load_config(root).adapter)
        if getattr(args, "adapter", None) and args.adapter != cfg.get("name"):
            cfg = {"name": args.adapter}  # the repository's adapter keys belong to its own adapter
        ad = make_adapter(cfg)
    return Ariadne(root, adapter=ad)


def _run_id(ari, args: argparse.Namespace, *, any_run: bool = False) -> str:
    if getattr(args, "run", None):
        return ari.store.resolve_run_id(args.run)
    rid = ari.active_run_id()
    if rid:
        return rid
    if any_run:
        ids = ari.store.run_ids()
        if ids:
            return ids[-1]
    return ari.require_active()


def _print(s: str = "") -> None:
    print(s, flush=True)


def _spec_from_args(args: argparse.Namespace) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if getattr(args, "contract", None):
        loaded = yaml.safe_load(Path(args.contract).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise DaedalusError("contract file must be a mapping")
        spec.update(loaded)
    if args.goal:
        spec["objective"] = args.goal
    if args.criterion:
        spec["acceptance_criteria"] = list(args.criterion)
    if args.scope:
        spec["scope"] = list(args.scope)
    if args.risk:
        spec["risk_tier"] = args.risk
    if args.check:
        spec["required_checks"] = list(spec.get("required_checks", [])) + list(args.check)
    return spec


def render_decision(d: Decision, *, verbose: bool = True) -> list[str]:
    lines = [f"Candidate: {d.candidate_id[:12]} · risk {d.risk.explain()}"]
    if d.check_states:
        lines.append("Checks:")
        w = max(len(k) for k in d.check_states)
        for cid, (st, detail) in d.check_states.items():
            lines.append(f"  {cid.ljust(w)}  {st.value:<7} {detail if verbose else ''}".rstrip())
    if d.required_reviews:
        lines.append("Required reviews: " + ", ".join(d.required_reviews))
    if d.required_approvals:
        lines.append("Required approvals: " + ", ".join(d.required_approvals))
    head = f"Disposition: {d.disposition.value}" + (" (final)" if d.terminal else "")
    lines.append(head)
    for r in d.reasons:
        lines.append(f"  [{r.fixable_by}] {r.message}")
    return lines


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    root = find_root(args.dir)
    cfg = root / CONFIG_FILE
    if cfg.exists() and not args.force:
        _print(f"{CONFIG_FILE} already exists (use --force to overwrite)")
    else:
        checks = detect_checks(root)
        cfg.write_text(render_config(checks, args.adapter), encoding="utf-8")
        _print(f"wrote {CONFIG_FILE} with checks: {', '.join(checks) or 'NONE — add one before starting a run'}")
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if f"{STATE_DIR}/" not in existing.split():
        gi.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + f"{STATE_DIR}/\n")
        _print(f"added {STATE_DIR}/ to .gitignore")
    if args.claude_code:
        from daedalus.harness.claude_code import install

        for line in install(root):
            _print(line)
    _print(f"Next: review and commit {CONFIG_FILE} (runs only trust committed policy), then `daedalus start \"<goal>\"`.")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    root = find_root(args.dir)
    if args.cmd == "off":
        human_actor("turning the gate off", assume_yes=args.yes)
    set_gate(root, args.cmd == "on")
    _print(f"Daedalus gate {'ON' if args.cmd == 'on' else 'OFF'} for {root} (local setting in {STATE_DIR}/)")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = ari.start(_spec_from_args(args), actor=starter_actor(), mode=args.mode)
        state = ari.state(rid)
        _print(f"run {rid} started (mode {state.mode}, contract v{state.contract.contract_version})")
        _print(f"contract: {state.contract_file}")
        for c in state.contract.acceptance_criteria:
            _print(f"  {c.criterion_id}: {c.description}")
        _print("Work, then `daedalus verify` and `daedalus status`.")
        return 0
    finally:
        ari.close()


def cmd_run(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import run_task

    ari = _ari(args, adapter=True)
    try:
        report = run_task(
            ari,
            _spec_from_args(args),
            actor=starter_actor(),
            adapter=ari.adapter,
            mode=args.mode,
            max_rounds=args.rounds,
            notify=lambda m: _print(f"· {m}"),
        )
        _print()
        _print(f"run {report.run_id} after {report.rounds} round(s)")
        for line in render_decision(report.decision):
            _print(line)
        for n in report.notes:
            _print(f"note: {n}")
        return EXIT[report.decision.disposition]
    finally:
        ari.close()


def cmd_status(args: argparse.Namespace) -> int:
    root = find_root(args.dir)
    ari = _ari(args)
    try:
        enabled = gate_enabled(root)
        try:
            rid = _run_id(ari, args, any_run=True)
        except DaedalusError:
            if args.json:
                _print(json.dumps({"gate": enabled, "run": None}))
            else:
                _print(f"Daedalus gate: {'ON' if enabled else 'OFF'} · no runs yet")
                for note in ari.config().notes:
                    _print(f"note: {note}")
            return 0
        state = ari.state(rid)
        if ari.needs_recovery(rid):
            _print("warning: the last orchestrator stopped mid-operation; run `daedalus reconcile`")
        d = ari.evaluate(rid)
        if args.json:
            _print(
                json.dumps(
                    {
                        "gate": enabled,
                        "run": rid,
                        "execution": state.execution.value,
                        "mode": state.mode,
                        "contract": state.contract.to_dict(),
                        "decision": d.to_dict(),
                    },
                    indent=2,
                )
            )
            return 0
        _print(
            f"Daedalus gate: {'ON' if enabled else 'OFF'} · run {rid} ({state.mode} mode) · {state.execution.value}"
        )
        _print(f"Objective: {state.contract.objective}  [contract v{state.contract.contract_version}]")
        for line in render_decision(d, verbose=not args.brief):
            _print(line)
        return 0
    finally:
        ari.close()


def cmd_verify(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        recs = ari.verify(rid, only=args.only or None)
        for r in recs:
            extra = f" ({r.error})" if r.error else (f" exit {r.exit_code}" if r.result == "FAIL" else "")
            _print(f"{r.check_id}: {r.result} on {r.candidate_id[:12]}{extra}")
        d = ari.evaluate(rid)
        _print(f"Disposition: {d.disposition.value}")
        for r in d.reasons:
            _print(f"  [{r.fixable_by}] {r.message}")
        return 0 if all(r.result == "PASS" for r in recs if r.mandatory) else 1
    finally:
        ari.close()


def cmd_finish(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        d = ari.finish(_run_id(ari, args))
        for line in render_decision(d):
            _print(line)
        return EXIT[d.disposition]
    finally:
        ari.close()


def cmd_review(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import launch_required_reviews, launch_review

    ari = _ari(args, adapter=True)
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


def cmd_cancel(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        state = ari.cancel(_run_id(ari, args), actor=starter_actor(), reason=args.reason or "")
        _print(f"run {state.run_id}: {state.execution.value}" + (f" · {state.disposition.value}" if state.disposition else ""))
        return 0
    finally:
        ari.close()


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
    actor = human_actor(f"lowering risk to `{args.tier}` for the current candidate", assume_yes=args.yes)
    ari = _ari(args)
    try:
        ari.risk_exception(_run_id(ari, args), args.tier, actor=actor, rationale=args.rationale, interactive=True)
        _print(f"risk exception to `{args.tier}` recorded for the current candidate")
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


def cmd_blocker(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        bid = ari.raise_blocker(_run_id(ari, args), args.description, actor=starter_actor())
        _print(f"blocker {bid} raised")
        return 0
    finally:
        ari.close()


def cmd_reconcile(args: argparse.Namespace) -> int:
    if args.action:
        if args.occurred is None:
            raise DaedalusError("--action needs --occurred yes|no")
        actor = human_actor(f"recording that {args.action} {'DID' if args.occurred == 'yes' else 'did NOT'} happen")
        ari = _ari(args)
        try:
            ari.reconcile_action(
                _run_id(ari, args, any_run=True), args.action, occurred=args.occurred == "yes", actor=actor, interactive=True
            )
            _print(f"{args.action} reconciled")
            return 0
        finally:
            ari.close()
    ari = _ari(args, adapter=True)
    try:
        rid = _run_id(ari, args, any_run=True)
        notes = ari.recover(rid)
        for n in notes or ["nothing to reconcile"]:
            _print(n)
        return 0
    finally:
        ari.close()


def cmd_act(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        res = ari.execute_action(_run_id(ari, args, any_run=True), args.action, actor=starter_actor())
        _print(json.dumps(res))
        return 0 if res.get("ok") else 1
    finally:
        ari.close()


def cmd_log(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        if args.cmd == "audit" or args.verify:
            n = ari.store.verify_chain()
            _print(f"audit chain intact: {n} events verified")
        rid = _run_id(ari, args, any_run=True)
        events = ari.store.events(rid)
        if args.json:
            _print(json.dumps([e.to_dict() for e in events], indent=2))
            return 0
        for e in events:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.ts))
            _print(f"{e.seq:>5} {ts} {e.actor:<28} {e.type:<22} {_brief(e.type, e.payload)}")
        return 0
    finally:
        ari.close()


def _brief(type_: str, p: dict[str, Any]) -> str:
    if type_ == "evidence.recorded":
        r = p["record"]
        return f"{r['check_id']} {r['result']} on {r['candidate_id'][:12]}"
    if type_ == "execution.transition":
        return f"{p['from']} -> {p['to']} ({p.get('reason', '')})"
    if type_ == "candidate.recorded":
        return f"{p['candidate_id'][:12]} {p.get('label', '')}".strip()
    if type_ == "disposition.recorded":
        return p["disposition"]
    if type_ == "worker.transition":
        return f"{p['task_id']} -> {p['to']}"
    if type_ == "approval.recorded":
        a = p["approval"]
        return f"{a['decision']} {a['action']} for {a['candidate_id'][:12]}"
    if type_ == "review.recorded":
        r = p["review"]
        return f"{r['perspective']} {r['review_id']}: {len(r.get('findings', []))} finding(s)"
    if type_ == "contract.amended":
        return f"v{p['contract']['contract_version']}: {', '.join(p.get('changed', []))}"
    keys = ("reason", "description", "check_id", "action", "finding_id", "blocker_id", "violation_id", "message")
    return next((str(p[k]) for k in keys if k in p), "")[:80]


def cmd_runs(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        for rid in ari.store.run_ids():
            s = ari.state(rid)
            disp = s.disposition.value if s.disposition else "open"
            _print(f"{rid}  {s.execution.value:<10} {disp:<9} {s.contract.objective[:70]}")
        return 0
    finally:
        ari.close()


def cmd_metrics(args: argparse.Namespace) -> int:
    from daedalus.core.metrics import aggregate, collect

    ari = _ari(args)
    try:
        runs, errors = collect(ari.store)
        agg = aggregate(runs)
        if args.json:
            out = {"runs": [m.to_dict() for m in runs], "errors": errors, "aggregate": agg.to_dict()}
            _print(json.dumps(out, indent=2))
            return 0 if not errors else 1

        def pct(x: float | None) -> str:
            return "n/a" if x is None else f"{x:.0%}"

        def num(x: float | None, unit: str = "") -> str:
            return "n/a" if x is None else f"{x:.2f}{unit}"

        _print(
            f"{'run':<13} {'mode':<7} {'disposition':<11} {'1st':>3} {'wall':>7} {'cost':>6} {'att':>3} "
            f"{'cands':>5} {'fails':>5} {'rev':>3} {'frev':>4} {'stops':>5} {'recov':>5} {'human':>5} "
            f"{'findings':<14} objective"
        )
        for m in runs:
            wall = "open" if m.wall_seconds is None else f"{m.wall_seconds / 60:.1f}m"
            found = " ".join(f"{SEV_ABBR[k]}{m.findings[k]}" for k in SEV_ABBR if m.findings.get(k)) or "-"
            _print(
                f"{m.run_id:<13} {m.mode:<7} {m.disposition:<11} {'yes' if m.first_pass else '-':>3} {wall:>7} "
                f"{m.cost:>6.2f} {m.attempts:>3} {m.candidates_verified:>5} {m.check_failures:>5} {m.reviews:>3} "
                f"{m.failed_reviews:>4} {m.stop_blocks:>5} {m.recoveries:>5} {m.human_interventions:>5} "
                f"{found:<14} {m.objective[:40]}"
            )
        for rid, err in errors.items():
            _print(f"{rid:<13} ERROR       {err}")
        _print()
        _print(
            f"runs {agg.runs} (open {agg.open}) · accepted {agg.accepted} · rejected {agg.rejected} · "
            f"cancelled {agg.cancelled}" + (f" · unreadable {len(errors)}" if errors else "")
        )
        _print(
            f"acceptance rate {pct(agg.acceptance_rate)} · first-pass {pct(agg.first_pass_rate)} · "
            f"rework/accepted {num(agg.rework_per_accepted)}"
        )
        latency = None if agg.median_latency_accepted_s is None else agg.median_latency_accepted_s / 60
        _print(
            f"median latency (accepted) {num(latency, 'm')} · cost, all runs {agg.cost_total:.2f} · "
            f"cost per accepted (finished runs) {num(agg.cost_per_accepted)}"
        )
        _print(
            f"manual interventions {agg.manual_interventions} · recoveries {agg.recoveries} · "
            f"disputes {agg.disputes} · arbitrations {agg.arbitrations}"
        )
        if any(m.findings for m in runs):
            _print("findings: B=BLOCKER M=MAJOR m=MINOR A=ADVISORY")
        return 0 if not errors else 1
    finally:
        ari.close()


def cmd_policy(args: argparse.Namespace) -> int:
    if args.default:
        _print(default_policy_text())
        return 0
    ari = _ari(args)
    try:
        cfg = ari.config()
        _print(f"# effective policy ({cfg.source}), hash {cfg.policy.policy_hash[:12]}")
        _print(yaml.safe_dump(cfg.policy_raw, sort_keys=False))
        return 0
    finally:
        ari.close()


def cmd_hook(args: argparse.Namespace) -> int:
    from daedalus.harness.claude_code import run_hook

    out = run_hook(args.name, sys.stdin.buffer.read().decode("utf-8-sig", "replace"))  # harnesses send UTF-8 JSON
    if out:
        _print(out)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from daedalus.harness.claude_code import install

    root = find_root(args.dir)
    roles = ("gate",) + (("brunel", "socrates", "mozi", "aristotle", "james", "plato") if args.all_skills else ())
    for line in install(root, skills=roles):
        _print(line)
    _print("Claude Code will now stop at the Daedalus gate whenever a run is open and the gate is ON.")
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daedalus", description="Run agent work under a deterministic acceptance gate.")
    p.add_argument("--version", action="version", version=f"daedalus {__version__}")
    p.add_argument("-C", "--dir", default=".", help="repository directory (default: current)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name: str, fn, help_: str, *, run: bool = False, human: bool = False) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        if run:
            sp.add_argument("--run", help="run id or prefix (default: the open run)")
        if human:
            sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt (still needs a TTY)")
        return sp

    def contract_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("goal", nargs="?", help="the objective, in plain words")
        sp.add_argument("--contract", help="YAML file with contract fields")
        sp.add_argument("-c", "--criterion", action="append", help="acceptance criterion (repeatable)")
        sp.add_argument("--scope", action="append", help="path glob the change may touch (repeatable)")
        sp.add_argument("--risk", help="declared risk tier (can only raise the computed tier)")
        sp.add_argument("--check", action="append", help="extra required check from policy (repeatable)")
        sp.add_argument("--mode", choices=("gate", "review", "council"))

    sp = add("init", cmd_init, "write .daedalus.yml with detected checks")
    sp.add_argument("--adapter", help="agent harness for `daedalus run` (e.g. claude-code)")
    sp.add_argument("--claude-code", action="store_true", help="also install the Claude Code hooks and skill")
    sp.add_argument("--force", action="store_true")
    add("on", cmd_toggle, "turn the gate on for this repository")
    add("off", cmd_toggle, "turn the gate off for this repository (human only)", human=True)

    contract_args(add("start", cmd_start, "open a run; you or your harness agent do the work"))
    sp = add("run", cmd_run, "open a run and drive it with the configured agent adapter")
    contract_args(sp)
    sp.add_argument("--rounds", type=int, default=3, help="max build/verify/review rounds (default 3)")
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")

    sp = add("status", cmd_status, "show the gate, the run and what blocks acceptance", run=True)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--brief", action="store_true")
    sp = add("verify", cmd_verify, "run trusted checks against the current working tree", run=True)
    sp.add_argument("--only", action="append", help="run only this check (repeatable)")
    add("finish", cmd_finish, "derive and record the final disposition if it is decided", run=True)
    sp = add("review", cmd_review, "launch an independent review through the adapter", run=True)
    sp.add_argument("perspective", choices=("required", "socrates", "mozi", "aristotle", "james"))
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
    sp = add("cancel", cmd_cancel, "cancel the run", run=True)
    sp.add_argument("--reason")
    sp = add("abandon", cmd_abandon, "declare the run unresolvable: REJECTED (human only)", run=True, human=True)
    sp.add_argument("--reason", required=True)

    for name in ("approve", "deny"):
        sp = add(name, cmd_approve, f"{name} a restricted action for the current candidate (human only)", run=True, human=True)
        sp.add_argument("action")
        sp.add_argument("--rationale")
        if name == "approve":
            sp.add_argument("--expires", type=float, help="seconds until the approval expires")
            sp.add_argument("--reusable", action="store_true", help="allow more than one use")
    sp = add("attest", cmd_attest, "attest an acceptance criterion that has no check (human only)", run=True, human=True)
    sp.add_argument("criterion")
    sp.add_argument("status", choices=("pass", "fail", "PASS", "FAIL"))
    sp.add_argument("--evidence", required=True)
    sp = add("amend", cmd_amend, "version the contract (human only)", run=True, human=True)
    sp.add_argument("--set", action="append", help="field=YAML value (repeatable)")
    sp.add_argument("--file", help="YAML file of changed fields")
    sp.add_argument("--rationale", required=True)
    sp = add("risk-exception", cmd_risk_exception, "lower the effective risk tier (human only)", run=True, human=True)
    sp.add_argument("tier")
    sp.add_argument("--rationale", required=True)
    sp = add("resolve", cmd_resolve, "resolve a finding, blocker or violation (human only)", run=True, human=True)
    sp.add_argument("item")
    sp.add_argument("--note")
    sp = add("dispute", cmd_dispute, "contest an open review finding with a reason", run=True)
    sp.add_argument("finding")
    sp.add_argument("--reason", required=True)
    sp = add("arbitrate", cmd_arbitrate, "have Plato rule on a disputed finding (launched via the adapter)", run=True)
    sp.add_argument("finding")
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
    sp = add("blocker", cmd_blocker, "raise a blocker on the open run", run=True)
    sp.add_argument("description")
    sp = add("reconcile", cmd_reconcile, "recover after a crash, or record a restricted action's outcome", run=True)
    sp.add_argument("--action", help="action id with an unknown outcome")
    sp.add_argument("--occurred", choices=("yes", "no"))
    sp = add("act", cmd_act, "perform an approved restricted action", run=True)
    sp.add_argument("action")

    for name in ("log", "audit"):
        sp = add(name, cmd_log, "show the run's audit trail" + (" after verifying the hash chain" if name == "audit" else ""), run=True)
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--verify", action="store_true", help="verify the hash chain first")
    add("runs", cmd_runs, "list runs")
    sp = add("metrics", cmd_metrics, "measure runs from the audit log (acceptance, rework, cost, latency)")
    sp.add_argument("--json", action="store_true")
    sp = add("policy", cmd_policy, "print the effective policy")
    sp.add_argument("--default", action="store_true", help="print the built-in default policy")
    sp = add("hook", cmd_hook, "harness hook entry point (reads the hook payload on stdin)")
    sp.add_argument("name", choices=("stop", "session-start"))
    sp = add("install", cmd_install, "install harness integration")
    sp.add_argument("harness", choices=("claude-code",))
    sp.add_argument("--all-skills", action="store_true", help="also install the role skills")
    return p


def utf8_output() -> None:
    """Status lines use non-ASCII separators; terminals such as Git Bash expect UTF-8
    while Python defaults to the ANSI code page for pipes on Windows."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except DaedalusError as exc:
        print(f"daedalus: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("daedalus: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
