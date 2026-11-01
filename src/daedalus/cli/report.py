"""`daedalus report`: one run's verdict, checks, reviews, findings and cost, ready to share."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from daedalus.cli._common import _ari, _print, _run_id, add_command, hidden_checks
from daedalus.core.errors import DaedalusError


def cmd_report(args: argparse.Namespace) -> int:
    from daedalus.report import build_report, render_html, render_markdown

    ari = _ari(args)
    try:
        rid = _run_id(ari, args, any_run=True)
        if hidden_checks(ari.state(rid)):
            raise DaedalusError("this run has held-out checks; its report names them, so a person runs it")
        report = build_report(ari, rid)
    finally:
        ari.close()
    if args.format == "json":
        text = json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
    elif args.format == "html":
        text = render_html(report)
    else:
        text = render_markdown(report)
    if args.output:
        try:
            Path(args.output).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        except OSError as exc:
            raise DaedalusError(f"cannot write {args.output}: {exc}") from exc
        _print(f"wrote {args.format} report for run {report.run_id} to {args.output}")
    else:
        _print(text)
    return 0


def register(sub: Any) -> None:
    sp = add_command(sub, "report", cmd_report, "write a shareable report of one run (markdown, html or json)", run=True)
    sp.add_argument("--format", choices=("md", "html", "json"), default="md", help="output format (default: md)")
    sp.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")
