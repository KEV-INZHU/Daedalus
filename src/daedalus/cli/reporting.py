"""Health and reporting: doctor, ci, metrics, policy, and the audit trail."""

from __future__ import annotations

import argparse
import difflib
import json
import time
from typing import Any

import yaml

from daedalus.cli._common import SEV_ABBR, _ari, _print, _run_id, add_command, display_name, hidden_checks
from daedalus.core.config import PRESETS, find_root, load_config
from daedalus.core.errors import DaedalusError
from daedalus.core.policy import default_policy_text


def cmd_doctor(args: argparse.Namespace) -> int:
    from daedalus.doctor import diagnose, render

    findings = diagnose(find_root(args.dir))
    for line in render(findings):
        _print(line)
    return 0 if all(f.ok for f in findings) else 1


def cmd_ci(args: argparse.Namespace) -> int:
    from daedalus.ci import run_ci, write_result

    result_out = args.result_out
    head = args.head_sha or ""
    try:
        report = run_ci(find_root(args.dir), args.base, require_provenance=args.require_provenance,
                        markdown_out=args.markdown_out, head_sha=head)
    except BaseException as exc:
        # Whatever went wrong, the result file says "error": exit code 1 means "blocked" and a crash
        # (a traceback exits 1 too) must never be mistaken for it.
        if result_out:
            try:
                write_result(result_out, "error", head_sha=head, detail=f"{type(exc).__name__}: {exc}")
            except OSError:
                pass
        raise
    _print(report.markdown())
    if args.annotations:
        for line in report.annotations():
            _print(line)
    if result_out:
        try:
            write_result(result_out, "passed" if report.ok else "blocked", head_sha=report.head_sha)
        except OSError as exc:
            raise DaedalusError(f"cannot write the result file {result_out}: {exc}") from exc
    return 0 if report.ok else 1


def cmd_metrics(args: argparse.Namespace) -> int:
    from daedalus.core.metrics import aggregate, collect

    ari = _ari(args)
    try:
        from daedalus.repository.escapes import escaped_runs

        runs, errors = collect(ari.store)
        agg = aggregate(runs)
        escaped = escaped_runs(ari.root, [m.run_id for m in runs if m.accepted])
        escape_rate = round(len(escaped) / agg.accepted, 4) if agg.accepted else None
        if args.json:
            out = {
                "runs": [{**m.to_dict(), "escaped_by": escaped.get(m.run_id, [])} for m in runs],
                "errors": errors,
                "aggregate": {**agg.to_dict(), "escaped": len(escaped), "escape_rate": escape_rate},
            }
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
        _print(
            f"evidence cache: {agg.cache_hits} hit(s) ({agg.cache_check_hits} check, {agg.cache_review_hits} review) "
            f"· saved {agg.cache_seconds_saved / 60:.1f}m and {agg.cache_cost_saved:.2f} "
            f"(cash {agg.cache_cash_saved:.2f}; the rest is API-equivalent estimate)"
        )
        if agg.review_spend:
            parts = []
            for p, row in agg.review_spend.items():
                per = "no findings" if row["cost_per_finding"] is None else f"{row['cost_per_finding']:.2f}/finding"
                parts.append(f"{display_name(p)} {row['cost']:.2f} for {int(row['findings'])} ({per})")
            _print("review spend: " + " · ".join(parts))
        if agg.parallel_runs:
            _print(
                f"parallel work: {agg.parallel_runs} run(s) · saved {agg.parallel_saved_s / 60:.1f}m against running "
                f"the packages one by one · integration took {agg.integration_s / 60:.1f}m · "
                f"{agg.integration_failures} integration failure(s)"
            )
        _print(
            f"escaped defects {len(escaped)} of {agg.accepted} accepted · escape rate {pct(escape_rate)} "
            "(accepted runs a later `Fixes-Run: <run id>` commit says it fixes)"
        )
        if any(m.findings for m in runs):
            _print("findings: B=BLOCKER M=MAJOR m=MINOR A=ADVISORY")
        return 0 if not errors else 1
    finally:
        ari.close()


def _effective_view(cfg: Any) -> str:
    """The resolved policy plus the UX keys a preset also sets, as text a diff can compare."""
    view = {"mode": cfg.mode, "cache": {"checks": cfg.cache_checks, "reviews": cfg.cache_reviews},
            "harness": cfg.harness, **cfg.policy_raw}
    return yaml.safe_dump(view, sort_keys=False)


def _preset_diff(args: argparse.Namespace) -> int:
    """What `--preset X` would change in this repository's effective policy. Keys the repository
    sets itself win over a preset, so they never show up as changes."""
    root = find_root(args.dir)
    current = load_config(root)
    candidate = load_config(root, preset=args.preset)
    old, new = _effective_view(current), _effective_view(candidate)
    label = f"current ({current.preset or 'no preset'})"
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), label, f"preset {args.preset}",
                                     lineterm="", n=2))
    if not diff:
        _print(f"preset {args.preset} changes nothing here: the effective policy is already the same")
    else:
        for line in diff:
            _print(line)
        _print(f"# policy hash {current.policy.policy_hash[:12]} -> {candidate.policy.policy_hash[:12]}")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    if args.default:
        _print(default_policy_text())
        return 0
    if args.preset:
        return _preset_diff(args)
    if args.diff:
        raise DaedalusError("--diff compares against a preset: add --preset solo|standard|strict")
    ari = _ari(args)
    try:
        cfg = ari.config()
        _print(f"# effective policy ({cfg.source}), hash {cfg.policy.policy_hash[:12]}")
        _print(yaml.safe_dump(cfg.policy_raw, sort_keys=False))
        return 0
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
        hidden = hidden_checks(ari.state(rid))
        if args.json:
            if hidden:  # every event's raw payload, the pinned policy (with the holdout's command) included
                raise DaedalusError("this run has held-out checks; `daedalus log --json` is for a person to run")
            _print(json.dumps([e.to_dict() for e in events], indent=2))
            return 0
        for e in events:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.ts))
            brief = _brief(e.type, e.payload)
            check = e.payload.get("check_id") or (e.payload.get("record") or {}).get("check_id")
            if check in hidden:
                brief = "a held-out check"
            _print(f"{e.seq:>5} {ts} {e.actor:<28} {e.type:<22} {brief}")
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


def register(sub: Any) -> None:
    add_command(sub, "doctor", cmd_doctor,
                "check this repository is set up for the gate and say how to fix what is not")
    for name in ("log", "audit"):
        sp = add_command(sub, name, cmd_log,
                         "show the run's audit trail" + (" after verifying the hash chain" if name == "audit" else ""),
                         run=True)
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--verify", action="store_true", help="verify the hash chain first")
    sp = add_command(sub, "metrics", cmd_metrics, "measure runs from the audit log (acceptance, rework, cost, latency)")
    sp.add_argument("--json", action="store_true")
    sp = add_command(sub, "policy", cmd_policy, "print the effective policy")
    sp.add_argument("--default", action="store_true", help="print the built-in default policy")
    sp.add_argument("--preset", choices=PRESETS, help="show what this preset would change in the effective policy")
    sp.add_argument("--diff", action="store_true", help="with --preset: the default output (a diff)")
    sp = add_command(sub, "ci", cmd_ci, "gate a branch in CI: fresh checks, risk for the whole diff, commit provenance")
    sp.add_argument("--base", default="origin/main", help="branch or commit to compare against (default origin/main)")
    sp.add_argument("--require-provenance", action="store_true",
                    help="fail when a commit lacks a Daedalus-Run trailer (it was not accepted by the gate)")
    sp.add_argument("--markdown-out", metavar="FILE", help="also write a compact pull request comment body to FILE")
    sp.add_argument("--result-out", metavar="FILE",
                    help='write the verdict as JSON ({"verdict": "passed"|"blocked"|"error", ...}) to FILE; '
                         "an exit code of 1 alone cannot tell a block from a crash")
    sp.add_argument("--head-sha", metavar="SHA", help="the commit to name in the report (default: HEAD)")
    sp.add_argument("--annotations", action="store_true",
                    help="print GitHub workflow commands so signals and failing checks show inline on the PR")
