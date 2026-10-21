"""`daedalus demo`: a hermetic tour of the gate catching a cheating agent.

The demo itself lives in `daedalus.demo`; this is only the wiring, and it imports lazily so a
build without that module still starts and says so.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from daedalus.cli._common import add_command


def cmd_demo(args: argparse.Namespace) -> int:
    try:
        from daedalus import demo
    except ModuleNotFoundError as exc:
        if exc.name != "daedalus.demo":  # a real import bug inside the demo must not look like a missing feature
            raise
        print("demo not available in this build", file=sys.stderr)
        return 2
    return int(demo.run(args) or 0)


def register(sub: Any) -> None:
    sp = add_command(sub, "demo", cmd_demo, "watch the gate catch an agent that fakes a fix (no API key, about 20s)")
    sp.add_argument("--fast", action="store_true", help="no pauses between lines")
    sp.add_argument("--keep", action="store_true", help="leave the demo repository on disk and print its path")
