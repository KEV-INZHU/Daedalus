"""`daedalus calibration`: how far to trust each reviewer, scored from the audit log.

    daedalus calibration              Brier score and calibration table, by review perspective
    daedalus calibration --by model   ... by the model reviews ran on (or --by severity)
    daedalus calibration --json

A finding counts once its outcome is known: a later review confirmed the fix, the arbiter ruled, or a
person ran `daedalus findings judge F# valid|invalid`. A review's residual risk counts once its run is
accepted, and is scored against whether a later commit says it fixes that run (`Fixes-Run:`).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from daedalus.cli._common import _ari, _print, add_command, display_name


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def render(rep: dict[str, Any], errors: dict[str, str]) -> list[str]:
    lines: list[str] = []
    label = rep["grouped_by"]
    for kind, title in (("findings", "Findings: is each one real?"),
                        ("residual", "Residual risk: does a missed defect remain?")):
        sec = rep[kind]
        a = sec["all"]
        lines.append(f"{title}  ({a['n']} scored)")
        if not a["n"]:
            lines.append("  nothing to score yet")
            lines.append("")
            continue
        lines.append(f"  Brier {_num(a['brier'])} (always saying the base rate scores {_num(a['reference'])}; "
                     f"skill {_num(a['skill'])}, above 0 beats it)")
        lines.append(f"  stated {_pct(a['mean_forecast'])} on average, {_pct(a['base_rate'])} came true · "
                     f"reliability {_num(a['reliability'])} (lower is better) · resolution {_num(a['resolution'])} "
                     "(higher is better)")
        lines.append("  forecast   n   came true")
        for b in a["bins"]:
            lines.append(f"  {b['range']:<9} {b['n']:>3}   {_pct(b['observed']):>5}  (said {_pct(b['mean_forecast'])})")
        if len(sec["by"]) > 1:
            lines.append(f"  by {label}:")
            for k, s in sec["by"].items():
                name = display_name(k) if label == "perspective" else k
                lines.append(f"    {name:<28} n {s['n']:>3} · Brier {_num(s['brier'])} · said {_pct(s['mean_forecast'])}"
                             f", true {_pct(s['base_rate'])}")
        if a["n"] < rep["min_sample"]:
            lines.append(f"  (only {a['n']} scored; under {rep['min_sample']} these numbers say little)")
        lines.append("")
    gaps = rep["unscored"]
    lines.append(f"Not scored: {gaps['findings_unresolved']} finding(s) with no known outcome "
                 "(`daedalus findings judge F# valid|invalid` settles one), "
                 f"{gaps['findings_without_confidence']} with no stated confidence, "
                 f"{gaps['reviews_without_residual']} review(s) with no residual risk.")
    if rep["confident_false_alarms"]:
        lines.append("Most confident false alarms:")
        for f in rep["confident_false_alarms"][:5]:
            lines.append(f"  {f['run'][:12]} {f['finding']} {_pct(f['confidence'])} {display_name(f['perspective'])}: "
                         f"{f['title'][:90]} ({f['why']})")
    if errors:
        lines.append(f"{len(errors)} run(s) could not be read: " + ", ".join(sorted(errors)[:5]))
    return lines


def cmd_calibration(args: argparse.Namespace) -> int:
    from daedalus.core.calibration import forecasts, load_states, report
    from daedalus.core.state_machine import Disposition
    from daedalus.repository.escapes import escaped_runs

    ari = _ari(args)
    try:
        states, errors = load_states(ari.store)
        accepted = [s.run_id for s in states if s.disposition is Disposition.ACCEPTED]
        escaped = set(escaped_runs(ari.root, accepted))
        fs, gaps = forecasts(states, escaped)
        rep = report(fs, gaps, by=args.by)
        if args.json:
            _print(json.dumps({**rep, "errors": errors}, indent=2))
        else:
            _print("\n".join(render(rep, errors)))
        return 0 if not errors else 1
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(sub, "calibration", cmd_calibration,
                     "score reviewers' stated confidence against what happened (Brier score, calibration)")
    sp.add_argument("--by", choices=("perspective", "model", "severity"), default="perspective")
    sp.add_argument("--json", action="store_true")
