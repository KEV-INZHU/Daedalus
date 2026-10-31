"""`daedalus bench`: run the planted-bug benchmark against a reviewer (see daedalus/bench.py).

    daedalus bench                         the configured adapter, full review prompt
    daedalus bench --lite                  the lite prompt (and lite model, if one is set)
    daedalus bench --reviewer-cmd "CMD"    any reviewer: CMD reads the prompt on stdin, prints review JSON
    daedalus bench --cases my.yaml --repeat 2 --json
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from daedalus.cli._common import _print, add_command
from daedalus.core.config import STATE_DIR, find_root, load_config
from daedalus.core.errors import DaedalusError


def cmd_bench(args: argparse.Namespace) -> int:
    from daedalus.adapters import make_adapter
    from daedalus.bench import ShellReviewer, load_cases, render, run_bench

    root = find_root(args.dir)
    cases = load_cases(args.cases)
    if args.only:
        wanted = set(args.only)
        cases = [c for c in cases if c.name in wanted]
        if missing := wanted - {c.name for c in cases}:
            raise DaedalusError(f"no such case(s): {', '.join(sorted(missing))}")
    if args.reviewer_cmd:
        adapter: Any = ShellReviewer(args.reviewer_cmd)
    else:
        cfg = load_config(root)
        adapter = make_adapter(dict(cfg.adapter))
        if not adapter.capabilities.launch_agent:
            raise DaedalusError(f"the `{adapter.name}` adapter cannot launch a reviewer: set `adapter:` in "
                                ".daedalus.yml or pass --reviewer-cmd")
        if args.lite:
            from daedalus.orchestration.lifecycle import LITE_DEFAULT_MODEL

            model = cfg.lite_model or (LITE_DEFAULT_MODEL if adapter.name == "claude-code" else None)
            if model and hasattr(adapter, "review_model"):
                adapter.review_model = model
        if adapter.expected_cost_basis() == "cash" and not args.yes:
            raise DaedalusError(f"{len(cases) * args.repeat} review(s) would be billed to an API key; "
                                "pass --yes to run them")
    n = len(cases) * args.repeat
    _print(f"benchmark: {n} review(s) of {len(cases)} case(s)")
    summary = run_bench(cases, adapter, lite=args.lite, repeat=args.repeat,
                        notify=(lambda m: None) if args.json else (lambda m: _print("  " + m)))
    out_dir = root / STATE_DIR / "bench"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except OSError:
        path = None
    if args.json:
        _print(json.dumps(summary, indent=2))
    else:
        _print("\n".join(render(summary)))
        if path:
            _print(f"results: {path.relative_to(root)}")
    return 0 if not summary["failed"] else 1


def register(sub: Any) -> None:
    sp = add_command(sub, "bench", cmd_bench,
                     "benchmark a reviewer on planted bugs: recall, false alarms, calibration, cost")
    sp.add_argument("--lite", action="store_true", help="use the lite review prompt and model")
    sp.add_argument("--reviewer-cmd", metavar="CMD", help="benchmark this command instead of the adapter")
    sp.add_argument("--cases", metavar="FILE", help="a cases file instead of the built-in corpus")
    sp.add_argument("--only", nargs="+", metavar="CASE", help="run only these cases")
    sp.add_argument("--repeat", type=int, default=1, help="run every case this many times")
    sp.add_argument("--yes", action="store_true", help="allow reviews billed to an API key")
    sp.add_argument("--json", action="store_true")
