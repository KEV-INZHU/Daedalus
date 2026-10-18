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
from daedalus.core.contracts import bind_text_criteria
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


def _spec_from_args(args: argparse.Namespace, policy: Any = None) -> dict[str, Any]:
    """The contract spec from flags and `--contract`. Criteria given with `-c` are proved by the
    required checks (bound here, when `policy` is given); `--attest` criteria need a person; bare
    strings in a contract file keep their meaning (a person attests them)."""
    spec: dict[str, Any] = {}
    if getattr(args, "contract", None):
        loaded = yaml.safe_load(Path(args.contract).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise DaedalusError("contract file must be a mapping")
        spec.update(loaded)
    if args.goal:
        spec["objective"] = args.goal
    crits: list[Any] = list(args.criterion or [])
    if policy is not None and crits:
        merged = list(spec.get("required_checks") or []) + list(args.check or [])  # file + --check
        crits = bind_text_criteria({"acceptance_criteria": crits, "required_checks": merged}, policy)[
            "acceptance_criteria"
        ]
    crits += [{"description": a, "checks": []} for a in getattr(args, "attest", None) or []]
    if crits:  # flags add to the contract file's criteria rather than replacing them
        spec["acceptance_criteria"] = list(spec.get("acceptance_criteria") or []) + crits
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
    if (args.ci_install or args.require_provenance or args.ci_source) and not args.ci:
        raise DaedalusError("--ci-install, --ci-source and --require-provenance configure the workflow: add --ci")
    workflow = None
    if args.ci:  # render (and so validate) before writing anything
        from daedalus.ci import DEFAULT_SOURCE, render_workflow

        workflow = render_workflow(args.ci_source or DEFAULT_SOURCE, args.ci_install, args.require_provenance)
    root = find_root(args.dir)
    cfg = root / CONFIG_FILE
    if cfg.exists() and (args.ci or not args.force):  # with --ci, --force refreshes only the workflow
        _print(f"{CONFIG_FILE} already exists" + (" (kept; --ci --force refreshes only the workflow)" if args.ci
                                                  else " (use --force to overwrite)"))
    else:
        checks = detect_checks(root)
        cfg.write_text(render_config(checks, args.adapter), encoding="utf-8")
        _print(f"wrote {CONFIG_FILE} with checks: {', '.join(checks) or 'NONE — add one before starting a run'}")
    if _ensure_ignored(root, f"{STATE_DIR}/"):
        _print(f"added {STATE_DIR}/ to .gitignore")
    if not args.no_hooks:
        from daedalus.harness.claude_code import install
        from daedalus.harness.provenance import install_git_hook

        for line in install(root):
            _print(line)
        try:
            _print(install_git_hook(root))
        except DaedalusError as exc:
            _print(f"note: {exc}")
        _ensure_ignored(root, ".claude/settings.local.json")
    if workflow is not None:
        wf = root / ".github" / "workflows" / "daedalus.yml"
        if wf.exists() and not args.force:
            _print(f"{wf.relative_to(root).as_posix()} already exists (use --force to overwrite)")
        else:
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(workflow, encoding="utf-8")
            _print(f"wrote {wf.relative_to(root).as_posix()}: pull requests now run `daedalus ci`")
    _print(f"Next: review and commit {CONFIG_FILE}" + ("" if args.no_hooks else " and .claude/")
           + (" and .github/workflows/daedalus.yml" if args.ci else "") + ".")
    _print("Then just work: in Claude Code the first edit opens a run and the gate decides when it is done.")
    return 0


def _ensure_ignored(root: Path, entry: str) -> bool:
    """Add `entry` to .gitignore unless present. Returns True when it was added."""
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if entry in existing.split():
        return False
    gi.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + entry + "\n")
    return True


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
        spec = _spec_from_args(args, ari.config().policy)
        rid = ari.start(spec, actor=starter_actor(), mode=args.mode)
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
            _spec_from_args(args, ari.config().policy),
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
        passed = all(r.result == "PASS" for r in recs if r.mandatory)
        from daedalus.harness.claude_code import launched_by_daedalus

        if passed and not args.no_review and not args.only and not launched_by_daedalus():
            from daedalus.harness.claude_code import settle

            d, notes, _ = settle(ari, rid, notify=lambda m: _print(f"· {m}"))
            for n in notes:
                _print(f"note: {n}")
            for f in ari.state(rid).open_findings:
                _print(f"  {f.finding_id} [{f.severity.value}] {f.title}: {f.required_change}"[:300])
        d = ari.evaluate(rid)
        _print(f"Disposition: {d.disposition.value}")
        for r in d.reasons:
            _print(f"  [{r.fixable_by}] {r.message}")
        return 0 if passed else 1
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
            f"median latency (accepted) {num(latency, 'm')} · cost, all runs {agg.cost_total:.2f} "
            f"(cash {agg.cash_total:.2f}; the rest is API-equivalent estimate) · "
            f"cost per accepted (finished runs) {num(agg.cost_per_accepted)}"
        )
        _print(
            f"manual interventions {agg.manual_interventions} · recoveries {agg.recoveries} · "
            f"disputes {agg.disputes} · arbitrations {agg.arbitrations}"
        )
        _print(
            f"reviews {agg.reviews} ({agg.delta_reviews} delta) · reviews/accepted {num(agg.reviews_per_accepted)} · "
            f"late findings {agg.late_findings} (raised by a re-review of code already reviewed)"
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
    if args.name == "pre-push":  # a git hook: exit status 3 (and only 3) refuses the push
        from daedalus.harness.provenance import pre_push_hook

        return pre_push_hook(list(args.args), sys.stdin.buffer.read().decode("utf-8", "replace"))
    from daedalus.harness.claude_code import run_hook

    stdin = "" if args.name == "commit-msg" else sys.stdin.buffer.read().decode("utf-8-sig", "replace")  # UTF-8 JSON
    out = run_hook(args.name, stdin, list(args.args)) if args.args else run_hook(args.name, stdin)
    if out:
        _print(out)
    return 0


def cmd_ci(args: argparse.Namespace) -> int:
    from daedalus.ci import run_ci

    report = run_ci(find_root(args.dir), args.base, require_provenance=args.require_provenance)
    _print(report.markdown())
    return 0 if report.ok else 1


def cmd_restore(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import restore_followup

    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        for n in restore_followup(ari, rid, notify=lambda m: _print(f"· {m}")):
            _print(n)
        d = ari.evaluate(rid)
        _print(f"Disposition: {d.disposition.value}")
        for r in d.reasons:
            _print(f"  [{r.fixable_by}] {r.message}")
        return 0
    finally:
        ari.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    from daedalus.doctor import diagnose, render

    findings = diagnose(find_root(args.dir))
    for line in render(findings):
        _print(line)
    return 0 if all(f.ok for f in findings) else 1


def cmd_statusline(args: argparse.Namespace) -> int:
    from daedalus.harness.claude_code import statusline

    try:
        payload = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except ValueError:
        payload = {}
    if not payload.get("cwd") and not (payload.get("workspace") or {}).get("current_dir"):
        payload["cwd"] = args.dir
    line = statusline(payload)
    if line:
        _print(line)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from daedalus.harness.claude_code import install

    root = find_root(args.dir)
    from daedalus.harness.provenance import install_git_hook

    roles = ("gate",) + (("brunel", "socrates", "mozi", "aristotle", "james", "plato", "vitruvius") if args.all_skills else ())
    for line in install(root, skills=roles):
        _print(line)
    try:
        _print(install_git_hook(root))
    except DaedalusError as exc:
        _print(f"note: {exc}")
    _print("Commit .claude/ so every session works under the gate: the first edit opens a run, and the")
    _print("gate verifies, reviews and decides when the agent stops.")
    return 0


# ------------------------------------------------------------------- factory
def _factory_ctx(args: argparse.Namespace, *, adapter: bool = False):
    from daedalus.factory.line import factory_config

    ari = _ari(args, adapter=adapter)
    return ari, factory_config(ari.root)


def cmd_factory_add(args: argparse.Namespace) -> int:
    from daedalus.factory.orders import slug, write_order

    ari, cfg = _factory_ctx(args)
    try:
        body: dict[str, Any] = {} if args.title is None else {"title": args.title}
        body["objective"] = args.objective
        if args.criterion:
            body["acceptance_criteria"] = list(args.criterion)
        if args.scope:
            body["scope"] = list(args.scope)
        if args.depends_on:
            body["depends_on"] = list(args.depends_on)
        if args.priority:
            body["priority"] = args.priority
        if args.mode:
            body["mode"] = args.mode
        if args.rounds:
            body["rounds"] = args.rounds
        oid = args.id or slug(args.objective)
        p = write_order(ari.root, cfg.orders_dir, oid, body, ari.config().policy)
        rel = p.relative_to(ari.root).as_posix()
        _print(f"order {oid} written to {rel}")
        _print(f"Review it, then commit it (the line only builds committed orders): git add {rel} && git commit")
        return 0
    finally:
        ari.close()


def cmd_factory_plan(args: argparse.Namespace) -> int:
    from daedalus.factory.orders import write_order
    from daedalus.factory.planner import plan_orders

    ari, cfg = _factory_ctx(args, adapter=True)
    try:
        plan = plan_orders(ari, ari.adapter, args.goal, max_orders=args.max, notify=lambda m: _print(f"· {m}"))
        if plan.summary:
            _print(f"plan: {plan.summary}")
        policy = ari.config().policy
        for oid, spec in plan.orders:
            deps = f" (after {', '.join(spec['depends_on'])})" if spec.get("depends_on") else ""
            if args.dry_run:
                _print(f"  {oid}{deps}: {spec['objective']}")
                continue
            p = write_order(ari.root, cfg.orders_dir, oid, spec, policy)
            _print(f"  wrote {p.relative_to(ari.root).as_posix()}{deps}")
        for d in plan.dropped:
            _print(f"  dropped {d}")
        _print(f"planner cost {plan.cost:.2f}")
        if plan.orders and not args.dry_run:
            _print(f"Review and edit the drafts in {cfg.orders_dir}/, commit the ones you want, then `daedalus factory run`.")
        return 0 if plan.orders else 1
    finally:
        ari.close()


def cmd_factory_run(args: argparse.Namespace) -> int:
    from daedalus.factory.line import Line

    ari = _ari(args, adapter=True)
    try:
        line = Line(
            ari,
            ari.adapter,
            actor=starter_actor(),
            into=args.into,
            max_orders=args.max_orders,
            budget=args.budget,
            notify=lambda m: _print(f"· {m}"),
        )
        report = line.run()
        _print()
        _print(f"line stopped: {report.stopped}")
        _print(
            f"shipped {len(report.shipped)}" + (f" ({', '.join(report.shipped)})" if report.shipped else "")
            + f" · parked {len(report.parked)}" + (f" ({', '.join(report.parked)})" if report.parked else "")
            + f" · cost {report.cost:.2f} · integration branch {report.into}"
        )
        if report.waiting:
            from daedalus.factory.ledger import Ledger

            rec = Ledger(ari.root).records()[report.waiting]
            _print(f"order {report.waiting} (run {rec.run_id}) is waiting on branch {rec.branch}:")
            for r in rec.reasons:
                _print(f"  {r}")
            _print(
                "Decide (e.g. `daedalus approve merge`, `daedalus attest C1 pass --evidence ...`), then "
                f"`daedalus factory run` to ship it; or `daedalus factory park {report.waiting}` to set it aside."
            )
        for n in report.notes:
            _print(f"note: {n}")
        return 1 if report.waiting else 0
    finally:
        ari.close()


def cmd_factory_status(args: argparse.Namespace) -> int:
    from daedalus.factory.escapes import escaped_orders
    from daedalus.factory.ledger import Ledger, summarize
    from daedalus.factory.line import blocked_by, ready_orders
    from daedalus.factory.orders import load_orders

    ari, cfg = _factory_ctx(args)
    try:
        orders, errors = load_orders(ari.root, cfg.orders_dir, ari.config().policy)
        recs = Ledger(ari.root).records()
        ids = [o.order_id for o in orders] + [oid for oid in recs if oid not in {o.order_id for o in orders}]
        summary = summarize(ids, recs)
        ready = ready_orders(orders, recs)
        escaped = escaped_orders(ari.root, recs)

        def shown(o) -> str:
            st = recs[o.order_id].status if o.order_id in recs else "queued"
            if st == "queued":
                return "blocked" if blocked_by(o, recs) else "ready"
            return st

        if args.json:
            _print(json.dumps({
                "orders": [
                    {"id": o.order_id, "status": shown(o), "priority": o.priority, "depends_on": list(o.depends_on),
                     "title": o.title, "objective": o.spec.get("objective"), "escaped": o.order_id in escaped, **({k: v for k, v in vars(recs[o.order_id]).items() if k != "order_id"}
                                              if o.order_id in recs else {})}
                    for o in orders
                ],
                "next": ready[0].order_id if ready else None,
                "invalid": errors,
                "summary": {
                    **vars(summary),
                    "escaped": len(escaped),
                    "escaped_orders": escaped,
                    "escape_rate": len(escaped) / summary.shipped if summary.shipped else None,
                },
            }, indent=2, default=str))
            return 0
        _print(f"Factory · {len(orders)} order(s) in {cfg.orders_dir}/")
        if orders:
            w = max(8, *(len(o.order_id) for o in orders))
            _print(f"{'order'.ljust(w)}  {'status':<9} {'pri':>4} {'tries':>5} {'cost':>6}  {'run':<12}  objective")
            rank = {"waiting": 0, "building": 1, "ready": 2, "blocked": 3, "parked": 4, "rejected": 5,
                    "cancelled": 6, "shipped": 7}
            for o in sorted(orders, key=lambda o: (rank.get(shown(o), 9), -o.priority, o.order_id)):
                r = recs.get(o.order_id)
                st = shown(o)
                cost = f"{r.cost:.2f}" if r and r.cost else "-"
                _print(
                    f"{o.order_id.ljust(w)}  {st:<9} {o.priority:>4} {(r.attempts if r else 0):>5} {cost:>6}  "
                    f"{(r.run_id if r and r.run_id else '-'):<12}  {o.title}"
                )
                if st == "blocked":
                    _print(f"{''.ljust(w)}  waits on {', '.join(blocked_by(o, recs))}")
                elif r and st in ("waiting", "parked", "rejected") and r.reasons:
                    for reason in r.reasons[:3 if st == "waiting" else 1]:
                        _print(f"{''.ljust(w)}  {reason[:110]}")
        for path, err in errors.items():
            _print(f"invalid {path}: {err}")
        _print()
        _print(f"next: {ready[0].order_id}" if ready else "next: nothing is ready")

        def pct(x: float | None) -> str:
            return "n/a" if x is None else f"{x:.0%}"

        cycle = "n/a" if summary.median_cycle_s is None else f"{summary.median_cycle_s / 60:.1f}m"
        per = "n/a" if summary.cost_per_shipped is None else f"{summary.cost_per_shipped:.2f}"
        _print(
            f"shipped {summary.shipped} of {summary.attempted} attempted · yield {pct(summary.yield_rate)} · "
            f"first-pass yield {pct(summary.first_pass_yield)} · cost {summary.cost_total:.2f} "
            f"({per} per shipped) · median cycle {cycle}"
        )
        rate = pct(len(escaped) / summary.shipped) if summary.shipped else "n/a"
        _print(f"escaped {len(escaped)} of {summary.shipped} shipped · escape rate {rate}")
        if escaped:
            _print(f"escaped orders: {', '.join(escaped)}")
        return 0
    finally:
        ari.close()


def cmd_factory_retry(args: argparse.Namespace) -> int:
    from daedalus.factory.line import requeue

    ari = _ari(args)
    try:
        for oid in args.order:
            requeue(ari, oid, actor=starter_actor())
            _print(f"order {oid} queued again; its branch keeps the earlier work")
        return 0
    finally:
        ari.close()


def cmd_factory_park(args: argparse.Namespace) -> int:
    from daedalus.factory.line import park_waiting

    ari = _ari(args)
    try:
        into = park_waiting(ari, args.order, actor=starter_actor())
        _print(f"order {args.order} parked; its work is kept on its branch" + (f"; back on {into}" if into else ""))
        _print(f"`daedalus factory retry {args.order}` queues it again.")
        return 0
    finally:
        ari.close()


def cmd_factory_stop(args: argparse.Namespace) -> int:
    from daedalus.factory.ledger import Ledger

    Ledger(find_root(args.dir)).request_stop()
    _print("the line stops after the current order (to stop that order too: `daedalus cancel`)")
    return 0


def add_factory_parser(sub: Any) -> None:
    fp = sub.add_parser(
        "factory",
        help="run a queue of work orders through the gate",
        description="A lightweight software factory: committed work orders in, gated commits out.",
    )
    fsub = fp.add_subparsers(dest="factory_cmd", required=True, metavar="factory-command")

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = fsub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("add", cmd_factory_add, "write a new work order file")
    sp.add_argument("objective")
    sp.add_argument("--id", help="order id (default: a slug of the objective)")
    sp.add_argument("--title", help="commit subject and board label, one line of at most 72 characters "
                    "(default: the objective's first sentence)")
    sp.add_argument("-c", "--criterion", action="append", help="acceptance criterion, proved by a test (repeatable)")
    sp.add_argument("--scope", action="append", help="path glob the order may change (repeatable)")
    sp.add_argument("--depends-on", action="append", help="order that must ship first (repeatable)")
    sp.add_argument("--priority", type=int, default=0, help="higher runs first (default 0)")
    sp.add_argument("--mode", choices=("gate", "review", "council"))
    sp.add_argument("--rounds", type=int, help="build/verify/review rounds before parking")
    sp = add("plan", cmd_factory_plan, "have the planner break a large goal into draft work orders")
    sp.add_argument("goal")
    sp.add_argument("--max", type=int, default=8, help="most orders to plan (default 8)")
    sp.add_argument("--dry-run", action="store_true", help="print the plan without writing files")
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
    sp = add("run", cmd_factory_run, "build ready orders one after another and ship the accepted ones")
    sp.add_argument("--into", help="integration branch (default: the current branch)")
    sp.add_argument("--max-orders", type=int, help="stop after this many orders")
    sp.add_argument("--budget", type=float, help="stop once this line has spent this much")
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
    sp = add("status", cmd_factory_status, "show the order board and line yield")
    sp.add_argument("--json", action="store_true")
    sp = add("retry", cmd_factory_retry, "queue parked, rejected or cancelled orders again")
    sp.add_argument("order", nargs="+")
    sp = add("park", cmd_factory_park, "set aside the order the line is waiting on and return to the integration branch")
    sp.add_argument("order")
    add("stop", cmd_factory_stop, "ask a running line to stop after its current order")


# -------------------------------------------------------------------- parser
class _Parser(argparse.ArgumentParser):
    """argparse swallows OSError while printing `--help`, so a closed pipe would exit 0
    with the output silently lost; let a BrokenPipeError on stdout reach main() instead.
    Errors on other streams (a usage message to a closed stderr) stay swallowed so the
    usage-error status is kept. This overrides a private hook; tests/unit/test_cli_pipe.py pins it."""

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            file = file or sys.stderr
            try:
                file.write(message)
            except BrokenPipeError:
                if file is sys.stdout:
                    global _stdout_failed
                    _stdout_failed = True
                    raise
            except (AttributeError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="daedalus", description="Run agent work under a deterministic acceptance gate.")
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
        sp.add_argument("-c", "--criterion", action="append", help="acceptance criterion, proved by the required checks (repeatable)")
        sp.add_argument("--attest", action="append", metavar="CRITERION",
                        help="acceptance criterion only a person can judge; needs `daedalus attest` (repeatable)")
        sp.add_argument("--scope", action="append", help="path glob the change may touch (repeatable)")
        sp.add_argument("--risk", help="declared risk tier (can only raise the computed tier)")
        sp.add_argument("--check", action="append", help="extra required check from policy (repeatable)")
        sp.add_argument("--mode", choices=("gate", "review", "council"))

    sp = add("init", cmd_init, "write .daedalus.yml with detected checks")
    sp.add_argument("--adapter", help="agent harness for `daedalus run` (e.g. claude-code)")
    sp.add_argument("--claude-code", action="store_true", help=argparse.SUPPRESS)  # the default now
    sp.add_argument("--no-hooks", action="store_true", help="do not install the Claude Code hooks and git hook")
    sp.add_argument("--ci", action="store_true", help="also write .github/workflows/daedalus.yml")
    sp.add_argument("--ci-install", metavar="CMD", help="command the workflow runs to install what your checks need")
    sp.add_argument("--ci-source", metavar="SPEC",
                    help="what the workflow pip-installs as Daedalus; pin it, e.g. 'git+https://...@<tag>'")
    sp.add_argument("--require-provenance", action="store_true",
                    help="the workflow fails pull requests whose commits were not accepted by the gate")
    sp.add_argument("--force", action="store_true")
    add("doctor", cmd_doctor, "check this repository is set up for the gate and say how to fix what is not")
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
    sp.add_argument("--no-review", action="store_true",
                    help="do not launch the reviews the run requires once checks pass")
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
    sp.add_argument("name", choices=("stop", "session-start", "prompt", "pre-edit", "commit-msg", "pre-push"))
    sp.add_argument("args", nargs="*", help="arguments a git hook passes through")
    add("statusline", cmd_statusline, "one status line for Claude Code (reads its JSON on stdin)")
    add("restore", cmd_restore, "undo a follow-up round: put back the candidate that was acceptable before it", run=True)
    sp = add("ci", cmd_ci, "gate a branch in CI: fresh checks, risk for the whole diff, commit provenance")
    sp.add_argument("--base", default="origin/main", help="branch or commit to compare against (default origin/main)")
    sp.add_argument("--require-provenance", action="store_true",
                    help="fail when a commit lacks a Daedalus-Run trailer (it was not accepted by the gate)")
    sp = add("install", cmd_install, "install harness integration")
    sp.add_argument("harness", choices=("claude-code",))
    sp.add_argument("--all-skills", action="store_true", help="also install the role skills")
    add_factory_parser(sub)
    return p


def utf8_output() -> None:
    """Status lines use non-ASCII separators; terminals such as Git Bash expect UTF-8
    while Python defaults to the ANSI code page for pipes on Windows."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    global _stdout_failed
    _stdout_failed = False
    utf8_output()
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:
            # `--help` and `--version` exit with their output still buffered; a usage
            # error keeps its own status even when stdout has gone away.
            if _flush_stdout() and not exc.code:
                return 128 + SIGPIPE
            raise
        code = int(args.fn(args) or 0)
    except DaedalusError as exc:
        _warn(f"daedalus: {exc}")
        code = 2
    except KeyboardInterrupt:
        _warn("daedalus: interrupted")
        code = 130
    except BrokenPipeError:
        if not _stdout_broken():
            raise  # some other pipe broke; that is a real failure
        _silence_stdout()
        return 128 + SIGPIPE
    # Flush on every exit path so a reader that closed early surfaces here,
    # not as an "Exception ignored" message at interpreter shutdown.
    if _flush_stdout() and code == 0:
        return 128 + SIGPIPE
    return code


_stdout_failed = False  # set by _Parser._print_message when a write to stdout breaks
SIGPIPE = 13  # signal.SIGPIPE on POSIX; Windows lacks the constant but the convention holds.


def _flush_stdout() -> bool:
    """Flush stdout; if its reader has gone away, silence it and return True."""
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()
        return True
    return False


def _stdout_broken() -> bool:
    """Whether stdout's reader has gone away, as opposed to some other pipe breaking."""
    if _stdout_failed:
        return True  # _Parser._print_message saw the write to stdout fail
    try:
        sys.stdout.flush()  # output that failed to reach the reader stays buffered
    except BrokenPipeError:
        return True
    except (OSError, ValueError, AttributeError):
        return False
    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        return False  # no descriptor, so the pipe that broke was not stdout
    import select

    if not hasattr(select, "poll"):
        # Windows has no poll() for pipes. Keep the old behaviour and treat the error as a
        # closed reader: the flush above already catches the usual case, and a traceback
        # for `daedalus ... | more` is the worse mistake there.
        return True
    try:
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        events = poller.poll(0)
    except (OSError, ValueError):
        return False
    # A pipe whose readers have all closed reports POLLERR on Linux and POLLHUP on macOS/BSD.
    return any(mask & (select.POLLERR | select.POLLHUP) for _, mask in events)


def _warn(message: str) -> None:
    """Report on stderr; a closed stderr must not cost the exit status or the stdout flush."""
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError, AttributeError):
        pass


def _silence_stdout() -> None:
    """The reader went away (`daedalus status | head`). Point stdout at devnull so the
    final flush at exit cannot raise again; see the Python `signal` docs on SIGPIPE."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        # stdout is not backed by a file descriptor (e.g. replaced in tests).
        pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
