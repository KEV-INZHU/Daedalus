"""The factory: a queue of work orders through the gate."""

from __future__ import annotations

import argparse
import json
from typing import Any

from daedalus.cli._common import (
    _ari,
    _print,
    starter_actor,
)
from daedalus.core.config import (
    find_root,
)


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


def register(sub: Any) -> None:
    fp = sub.add_parser(
        "factory",
        help="run a queue of work orders through the gate",
        description="run a queue of work orders through the gate",
        epilog="A lightweight software factory: committed work orders in, gated commits out.",
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
