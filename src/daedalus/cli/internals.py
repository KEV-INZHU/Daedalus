"""Advanced and internal: harness hooks, the status line, crash recovery, restricted actions."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from daedalus.cli._common import (
    _ari,
    _print,
    _run_id,
    add_command,
    human_actor,
    starter_actor,
)
from daedalus.core.errors import DaedalusError


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


def register(sub: Any) -> None:
    sp = add_command(sub, "reconcile", cmd_reconcile, "recover after a crash, or record a restricted action's outcome",
                     run=True)
    sp.add_argument("--action", help="action id with an unknown outcome")
    sp.add_argument("--occurred", choices=("yes", "no"))
    sp = add_command(sub, "act", cmd_act, "perform an approved restricted action", run=True)
    sp.add_argument("action")
    sp = add_command(sub, "hook", cmd_hook, "harness hook entry point (reads the hook payload on stdin)")
    sp.add_argument("name", choices=("stop", "session-start", "prompt", "pre-edit", "commit-msg", "pre-push"))
    sp.add_argument("args", nargs="*", help="arguments a git hook passes through")
    add_command(sub, "statusline", cmd_statusline, "one status line for Claude Code (reads its JSON on stdin)")
