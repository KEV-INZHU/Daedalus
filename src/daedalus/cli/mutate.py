"""`daedalus mutate -- <test command>`: how many deliberate breaks in the changed code do the tests catch?

See daedalus/verification/mutation.py. Exit 0 when the score meets `--min-score` (or there is nothing
to mutate), 1 when it does not, 2 when the tests fail before any mutation.
"""

from __future__ import annotations

import argparse
from typing import Any

from daedalus.cli._common import _print, add_command
from daedalus.core.config import find_root
from daedalus.core.errors import DaedalusError


def cmd_mutate(args: argparse.Namespace) -> int:
    from daedalus.verification.mutation import run

    cmd = list(args.test_command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise DaedalusError(
            "give the test command after `--`, e.g. `daedalus mutate -- python -m pytest -q -x`"
        )
    if not 0 <= args.min_score <= 1:
        raise DaedalusError("--min-score must be between 0 and 1")
    result = run(find_root(args.dir), cmd, base=args.base, max_mutants=args.max, timeout_s=args.timeout)
    if result.baseline_error:
        _print(f"mutation: {result.baseline_error}; fix that first")
        return 2
    if result.score is None:
        _print("mutation: no changed Python lines to mutate")
        return 0
    killed = len(result.mutants) - len(result.survived)
    _print(
        f"mutation score {result.score:.0%}: the tests caught {killed} of {len(result.mutants)} deliberate breaks"
    )
    for m in result.survived:
        _print(f"  survived: {m.path}:{m.line}  {m.what}  (no test fails when this line is broken this way)")
    return 0 if result.score >= args.min_score else 1


def register(sub: Any) -> None:
    sp = add_command(
        sub, "mutate", cmd_mutate, "test strength: mutate changed lines, report what the tests miss"
    )
    sp.add_argument("--base", help="compare with this revision (default $DAEDALUS_BASE_REVISION, else HEAD)")
    sp.add_argument("--max", type=int, default=30, help="at most this many mutants (default 30)")
    sp.add_argument("--min-score", type=float, default=0.8, help="fail below this share caught (default 0.8)")
    sp.add_argument("--timeout", type=float, default=300.0, help="seconds per test run (default 300)")
    sp.add_argument("test_command", nargs=argparse.REMAINDER, help="-- then the test command")
