"""The factory ledger: what happened to each order, as an append-only event file.

Like run state, order state is never stored directly. It is a fold over the
events in `.daedalus/factory/ledger.jsonl`. The ledger only routes work; the
authoritative record of every build, check and review stays in the run's
hash-chained audit log, and the ledger points at it by run id.

Order statuses:

    queued    not started, or sent back by `factory retry`
    building  the line is working on it (or stopped mid-order; the next line resumes it)
    waiting   blocked on a human decision; its run is still open, the line halted on it
    shipped   accepted and committed to the integration branch
    parked    the agents could not get it accepted; work in progress kept on its branch
    rejected  its run was REJECTED (budget, violation, declared unresolvable)
    cancelled its run was cancelled
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daedalus.core.config import STATE_DIR

LEDGER = "ledger.jsonl"
STOP_FILE = "STOP"

STARTED = "order.started"
RUN_LINKED = "order.run"
OUTCOME = "order.outcome"
REQUEUED = "order.requeued"
LINE_STARTED = "line.started"
LINE_STOPPED = "line.stopped"

OUTCOMES = ("shipped", "parked", "rejected", "cancelled", "waiting")
DONE = ("shipped",)
FAILED = ("parked", "rejected")


def factory_dir(root: Path) -> Path:
    return root / STATE_DIR / "factory"


@dataclass
class OrderRecord:
    order_id: str
    status: str = "queued"
    attempts: int = 0
    runs: list[str] = field(default_factory=list)
    branch: str | None = None
    into: str | None = None
    into_head: str | None = None
    run_costs: dict[str, float] = field(default_factory=dict)  # latest reported spend per run
    reasons: list[str] = field(default_factory=list)
    commit: str | None = None
    first_started: float | None = None
    finished_at: float | None = None
    first_pass: bool | None = None  # shipped on its first attempt, in its first round

    @property
    def run_id(self) -> str | None:
        return self.runs[-1] if self.runs else None

    @property
    def cost(self) -> float:
        return round(sum(self.run_costs.values()), 4)

    @property
    def cycle_seconds(self) -> float | None:
        if self.status != "shipped" or self.first_started is None or self.finished_at is None:
            return None
        return self.finished_at - self.first_started


class Ledger:
    def __init__(self, root: Path, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.clock = clock
        self.path = factory_dir(self.root) / LEDGER
        self.skipped = 0  # unreadable lines seen by the last read

    # ------------------------------------------------------------ writing
    def append(self, type_: str, **payload: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": self.clock(), "type": type_, **payload}, sort_keys=True)
        with open(self.path, "a+b") as f:
            f.seek(0, 2)
            torn = False
            if f.tell():
                f.seek(-1, 2)
                torn = f.read(1) != b"\n"  # a crash mid-write left a partial record
            f.seek(0, 2)
            f.write((("\n" if torn else "") + line + "\n").encode("utf-8"))

    # ------------------------------------------------------------ reading
    def events(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        self.skipped = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                # `append` always starts a record on a fresh line, so a line that does not parse is
                # a record torn by a crash mid-write (or damage). It is skipped and counted, never
                # allowed to take later records with it.
                self.skipped += 1
                continue
            out.append(e)
        return out

    def records(self) -> dict[str, OrderRecord]:
        return fold(self.events())

    def order_for_run(self, run_id: str) -> str | None:
        for e in reversed(self.events()):
            if e["type"] in (RUN_LINKED, OUTCOME) and e.get("run") == run_id:
                return e["order"]
        return None

    # -------------------------------------------------------- stop request
    def request_stop(self) -> None:
        p = factory_dir(self.root) / STOP_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stop\n", encoding="utf-8")

    def take_stop(self) -> bool:
        """True (once) when someone asked the line to stop."""
        p = factory_dir(self.root) / STOP_FILE
        if p.exists():
            p.unlink(missing_ok=True)
            return True
        return False

    def clear_stop(self) -> None:
        (factory_dir(self.root) / STOP_FILE).unlink(missing_ok=True)


def fold(events: Iterable[dict[str, Any]]) -> dict[str, OrderRecord]:
    recs: dict[str, OrderRecord] = {}
    for e in events:
        oid = e.get("order")
        if not oid:
            continue
        r = recs.setdefault(oid, OrderRecord(oid))
        t = e["type"]
        if t == STARTED:
            r.status = "building"
            r.attempts += 1
            r.branch, r.into, r.into_head = e.get("branch"), e.get("into"), e.get("into_head")
            r.reasons = []
            if r.first_started is None:
                r.first_started = e["ts"]
        elif t == RUN_LINKED:
            if e["run"] not in r.runs:
                r.runs.append(e["run"])
        elif t == OUTCOME:
            r.status = e["outcome"]
            r.reasons = list(e.get("reasons") or [])
            if e.get("run"):  # a run reports its total so far; a waiting run reports again later
                r.run_costs[e["run"]] = float(e.get("cost") or 0.0)
            r.commit = e.get("commit") or r.commit
            if r.status != "waiting":
                r.finished_at = e["ts"]
            if r.status == "shipped":
                r.first_pass = r.attempts == 1 and int(e.get("rounds") or 0) == 1
        elif t == REQUEUED:
            r.status = "queued"
            r.reasons = []
    return recs


@dataclass(frozen=True)
class LineSummary:
    orders: int
    by_status: dict[str, int]
    shipped: int
    attempted: int  # orders that reached a final outcome at least once
    yield_rate: float | None  # shipped / attempted
    first_pass_yield: float | None  # shipped on the first attempt and round / attempted
    cost_total: float
    cost_per_shipped: float | None
    median_cycle_s: float | None


def summarize(order_ids: Iterable[str], recs: dict[str, OrderRecord]) -> LineSummary:
    ids = list(order_ids)
    by: dict[str, int] = {}
    for oid in ids:
        st = recs[oid].status if oid in recs else "queued"
        by[st] = by.get(st, 0) + 1
    rs = [recs[o] for o in ids if o in recs]
    attempted = [r for r in rs if r.status in ("shipped", "parked", "rejected", "cancelled")]
    shipped = [r for r in rs if r.status == "shipped"]
    cost = round(sum(r.cost for r in rs), 4)
    cycles = sorted(c for r in shipped if (c := r.cycle_seconds) is not None)
    median = None
    if cycles:
        mid = len(cycles) // 2
        median = cycles[mid] if len(cycles) % 2 else (cycles[mid - 1] + cycles[mid]) / 2
    return LineSummary(
        orders=len(ids),
        by_status=by,
        shipped=len(shipped),
        attempted=len(attempted),
        yield_rate=len(shipped) / len(attempted) if attempted else None,
        first_pass_yield=sum(1 for r in shipped if r.first_pass) / len(attempted) if attempted else None,
        cost_total=cost,
        cost_per_shipped=round(cost / len(shipped), 4) if shipped else None,
        median_cycle_s=median,
    )
