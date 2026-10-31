"""The assembly line: run ready work orders through the gate, one at a time.

For each order the line:

1. cuts a branch (`factory/<id>`) from the integration branch, or brings a
   parked attempt's branch up to date with it;
2. opens a run with the order's contract and drives it (build, verify, review),
   exactly as `daedalus run` does;
3. settles the outcome:
   - ACCEPTED: commits exactly the accepted candidate as one commit on top of
     the integration branch (fast-forward only), and deletes the order branch;
   - agents ran out of rounds: cancels the run, keeps the work in progress as a
     commit on the order branch, and moves on (`parked`);
   - only a human (or the system) can unblock it: leaves the run open on its
     branch and halts the line (`waiting`). Decide, then `factory run` resumes it;
   - REJECTED or CANCELLED: keeps the work on the branch, records it.

The line stops when nothing is ready, when asked (`factory stop`), at
`--max-orders` or the line budget, when an order needs a human, or when the
andon cord pulls: `andon` orders in a row failed, which usually means the
problem is the line (a broken check, a bad environment), not the orders.

Orders run sequentially because the core allows one open run per repository.
That keeps integration trivial: every order starts from the latest shipped
state, so a shipped commit is always a fast-forward of what was accepted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from daedalus.adapters.base import Adapter, require
from daedalus.core.acceptance import Decision
from daedalus.core.config import CONFIG_FILE
from daedalus.core.contracts import MODES
from daedalus.core.errors import DaedalusError, IllegalTransition
from daedalus.core.state_machine import Disposition, ExecutionState
from daedalus.factory import gitops
from daedalus.factory.ledger import (
    LINE_PUSHED,
    LINE_STARTED,
    LINE_STOPPED,
    OUTCOME,
    REQUEUED,
    RUN_LINKED,
    STARTED,
    Ledger,
    OrderRecord,
    factory_dir,
)
from daedalus.factory.orders import FactoryError, WorkOrder, load_orders
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import drive
from daedalus.orchestration.lock import OrchestratorLock

Notify = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


SHIP_TIMEOUT_S = 120.0  # each push or pull-request step of the optional ship step

# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class FactoryConfig:
    orders_dir: str = "factory/orders"
    branch_prefix: str = "factory/"
    mode: str | None = None  # None: the repository's default mode
    rounds: int = 3
    andon: int = 3  # consecutive failed orders that stop the line
    budget: float | None = None  # spend cap for one `factory run`, across orders
    # Optional ship step: after a line ships orders, push the integration branch to this remote and,
    # with `pull_request`, open a pull request into `base` (needs the `gh` CLI). Off by default.
    push: str | None = None
    pull_request: bool = False
    base: str | None = None


def factory_config(root: Any) -> FactoryConfig:
    """The `factory:` section of .daedalus.yml (all keys optional)."""
    try:
        cfg = yaml.safe_load((root / CONFIG_FILE).read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    except yaml.YAMLError as exc:
        raise FactoryError(f"{CONFIG_FILE} is not valid YAML: {exc}") from exc
    raw = cfg.get("factory") or {} if isinstance(cfg, dict) else {}
    if not isinstance(raw, dict):
        raise FactoryError("`factory:` in .daedalus.yml must be a mapping")
    unknown = set(raw) - {"orders", "branch_prefix", "mode", "rounds", "andon", "budget", "ship"}
    if unknown:
        raise FactoryError(f"unknown `factory:` keys: {sorted(unknown)}")
    mode = raw.get("mode")
    if mode is not None and mode not in MODES:
        raise FactoryError(f"factory.mode must be one of {MODES}")
    orders = str(raw.get("orders", "factory/orders")).strip("/")
    if not orders or orders.startswith(".") or ".." in orders.split("/"):
        raise FactoryError("factory.orders must be a directory inside the repository")
    ship = raw.get("ship") or {}
    if not isinstance(ship, dict) or set(ship) - {"push", "pull_request", "base"}:
        raise FactoryError("factory.ship must be a mapping with `push` (a remote), `pull_request` and `base`")
    if ship.get("pull_request") and not ship.get("push"):
        raise FactoryError("factory.ship.pull_request needs factory.ship.push (the branch must be on the remote)")
    if not isinstance(ship.get("pull_request", False), bool):
        raise FactoryError("factory.ship.pull_request must be true or false")
    for key in ("push", "base"):
        v = ship.get(key)
        # Passed to git and gh as arguments: never something they would read as an option.
        if v is not None and (not isinstance(v, str) or not v.strip() or v.strip().startswith("-")):
            raise FactoryError(f"factory.ship.{key} must be a name that does not start with '-'")
    try:
        out = FactoryConfig(
            push=str(ship["push"]) if ship.get("push") else None,
            pull_request=bool(ship.get("pull_request", False)),
            base=str(ship["base"]) if ship.get("base") else None,
            orders_dir=orders,
            branch_prefix=str(raw.get("branch_prefix", "factory/")),
            mode=mode,
            rounds=int(raw.get("rounds", 3)),
            andon=int(raw.get("andon", 3)),
            budget=None if raw.get("budget") is None else float(raw["budget"]),
        )
    except (TypeError, ValueError) as exc:
        raise FactoryError(f"invalid `factory:` value: {exc}") from exc
    if not out.branch_prefix.strip("/"):
        raise FactoryError("factory.branch_prefix must not be empty (order branches need their own namespace)")
    if out.rounds < 1 or out.andon < 1 or (out.budget is not None and out.budget < 0):
        raise FactoryError("factory.rounds and factory.andon must be at least 1, factory.budget non-negative")
    return out


# ---------------------------------------------------------------- picking
def blocked_by(order: WorkOrder, recs: dict[str, OrderRecord]) -> list[str]:
    """Dependencies that have not shipped yet."""
    return [d for d in order.depends_on if (recs.get(d) and recs[d].status) != "shipped"]


def ready_orders(orders: list[WorkOrder], recs: dict[str, OrderRecord]) -> list[WorkOrder]:
    """Queued orders whose dependencies have all shipped, in the order the line takes them."""
    ready = [
        o for o in orders if (recs[o.order_id].status if o.order_id in recs else "queued") == "queued"
        and not blocked_by(o, recs)
    ]
    return sorted(ready, key=lambda o: (-o.priority, o.order_id))


# ------------------------------------------------------------------- line
@dataclass
class LineReport:
    into: str
    shipped: list[str] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    waiting: str | None = None
    stopped: str = ""
    cost: float = 0.0
    notes: list[str] = field(default_factory=list)


class Line:
    def __init__(
        self,
        ari: Ariadne,
        adapter: Adapter,
        *,
        actor: str,
        into: str | None = None,
        max_orders: int | None = None,
        budget: float | None = None,
        notify: Notify = _quiet,
    ):
        self.ari = ari
        self.root = ari.root
        self.adapter = adapter
        self.actor = actor
        self.cfg = factory_config(self.root)
        self.ledger = Ledger(self.root, clock=ari.clock)
        self.into = into
        self.max_orders = max_orders
        self.budget = budget if budget is not None else self.cfg.budget
        self.notify = notify
        self._policy = ari.config().policy

    # ------------------------------------------------------------ helpers
    def _exclude(self) -> tuple[str, ...]:
        return self._policy.candidate_exclude

    def _require_clean(self, what: str) -> None:
        dirty = gitops.dirty_paths(self.root, self._exclude())
        if dirty:
            shown = ", ".join(dirty[:5]) + (" ..." if len(dirty) > 5 else "")
            raise FactoryError(f"{what}: the working tree has uncommitted changes ({shown}); commit or stash them")

    def _outcome(self, oid: str, rid: str | None, outcome: str, reasons: list[str], **extra: Any) -> None:
        cost = self.ari.state(rid).cost_used if rid else 0.0
        self.ledger.append(OUTCOME, order=oid, run=rid, outcome=outcome, reasons=reasons, cost=cost, **extra)

    def _orders(self) -> dict[str, WorkOrder]:
        orders, _ = load_orders(self.root, self.cfg.orders_dir, self._policy)
        return {o.order_id: o for o in orders}

    # --------------------------------------------------------------- run
    def run(self) -> LineReport:
        if not gitops.ok(self.root, "rev-parse", "--is-inside-work-tree"):
            raise FactoryError("the factory needs a git repository")
        gitops.head(self.root)
        require(self.adapter.capabilities, {"launch_agent": True}, self._policy.allowed_degradations)
        with line_lock(self.root):
            return self._run()

    def _run(self) -> LineReport:
        self.ledger.clear_stop()
        report = LineReport(into="")
        resumed = self._resume(report)
        if resumed == "waiting" or resumed == "cancelled":
            return report
        if not report.into:
            into = self.into or gitops.current_branch(self.root)
            if not into:
                raise FactoryError("HEAD is detached; check out the integration branch (or pass --into)")
            if into.startswith(self.cfg.branch_prefix):
                raise FactoryError(
                    f"{into} is an order branch; check out the integration branch (or pass --into)"
                )
            if not gitops.branch_exists(self.root, into):
                raise FactoryError(f"integration branch {into!r} does not exist")
            self._require_clean("cannot start the line")
            if gitops.current_branch(self.root) != into:
                gitops.git(self.root, "checkout", "-q", into)
            report.into = into
        self.into = report.into
        self.ledger.append(LINE_STARTED, into=self.into, by=self.actor)
        self.notify(f"line started on {self.into}")
        try:
            self._loop(report)
            if report.shipped and self.cfg.push:
                self._push_shipped(report)
        finally:
            self.ledger.append(
                LINE_STOPPED, reason=report.stopped or "interrupted", shipped=report.shipped, cost=report.cost
            )
        return report

    def _push_shipped(self, report: LineReport) -> None:
        """Push the integration branch, and open a pull request when configured. A failure here is
        reported and never undoes what shipped: the orders are on the integration branch either way."""
        remote, branch = self.cfg.push or "", self.into or ""
        # Never wait on a person: no terminal prompts, no inherited stdin, and a bound on each step.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

        def call(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env,
                                      stdin=subprocess.DEVNULL, timeout=SHIP_TIMEOUT_S, **kw)
            except subprocess.TimeoutExpired:
                return None

        r = call(["git", "-C", str(self.root), "push", "-u", "--", remote, branch])
        if r is None or r.returncode != 0:
            why = f"timed out after {SHIP_TIMEOUT_S:g}s" if r is None else (r.stderr or r.stdout).strip()[-300:]
            report.notes.append(f"push to {remote} failed: {why}")
            self.notify(report.notes[-1])
            return
        pr = ""
        if self.cfg.pull_request:
            base = self.cfg.base or "main"
            if shutil.which("gh") is None:
                report.notes.append("pushed; no pull request opened: the `gh` CLI is not installed")
            else:
                body = "Orders shipped by the Daedalus factory line:\n\n" + "\n".join(f"- {o}" for o in report.shipped)
                p = call(
                    ["gh", "pr", "create", f"--head={branch}", f"--base={base}",
                     f"--title=Factory: {len(report.shipped)} order(s) from {branch}", f"--body={body}"],
                    cwd=str(self.root),
                )
                pr = p.stdout.strip() if p is not None and p.returncode == 0 else ""
                if not pr:
                    why = f"timed out after {SHIP_TIMEOUT_S:g}s" if p is None else (p.stderr or p.stdout).strip()[-300:]
                    report.notes.append(f"pushed; opening a pull request failed: {why}")
        self.ledger.append(LINE_PUSHED, into=branch, remote=remote, shipped=report.shipped, pull_request=pr or None)
        msg = f"pushed {branch} to {remote}" + (f"; pull request {pr}" if pr else "")
        report.notes.append(msg)
        self.notify(msg)

    def _loop(self, report: LineReport) -> None:
        consecutive = 0
        reported_errors: set[str] = set()
        done = 0
        while True:
            if self.ledger.take_stop():
                report.stopped = "stop requested"
                return
            if self.max_orders is not None and done >= self.max_orders:
                report.stopped = f"reached --max-orders {self.max_orders}"
                return
            if self.budget is not None and report.cost >= self.budget:
                report.stopped = f"line budget spent ({report.cost:.2f} of {self.budget:.2f})"
                return
            orders, errors = load_orders(self.root, self.cfg.orders_dir, self._policy)
            for path, err in errors.items():
                if path not in reported_errors:
                    reported_errors.add(path)
                    report.notes.append(f"skipped {path}: {err}")
                    self.notify(f"skipped {path}: {err}")
            recs = self.ledger.records()
            ready = ready_orders(orders, recs)
            if not ready:
                report.stopped = self._idle_reason(orders, recs)
                return
            order = ready[0]
            outcome, cost = self._process(order)
            done += 1
            report.cost = round(report.cost + cost, 4)
            if outcome == "shipped":
                report.shipped.append(order.order_id)
                consecutive = 0
            elif outcome == "waiting":
                report.waiting = order.order_id
                report.stopped = f"order {order.order_id} needs a human decision"
                return
            elif outcome == "cancelled":
                report.stopped = f"order {order.order_id} was cancelled"
                return
            else:
                report.parked.append(order.order_id)
                consecutive += 1
                if consecutive >= self.cfg.andon:
                    report.stopped = (
                        f"andon: {consecutive} orders in a row did not ship; check the line "
                        "(checks, environment, order quality) before spending more"
                    )
                    return

    @staticmethod
    def _idle_reason(orders: list[WorkOrder], recs: dict[str, OrderRecord]) -> str:
        queued = [o for o in orders if (recs[o.order_id].status if o.order_id in recs else "queued") == "queued"]
        if not queued:
            return "no queued orders"
        stuck = sorted({d for o in queued for d in blocked_by(o, recs)})
        return f"{len(queued)} queued order(s) wait on dependencies that have not shipped: {', '.join(stuck)}"

    # ------------------------------------------------------------ resume
    def _resume(self, report: LineReport) -> str | None:
        """Pick up an order a previous line left mid-way. Returns its outcome, or None."""
        open_rid = self.ari.active_run_id()
        recs = self.ledger.records()
        if open_rid:
            oid = self.ledger.order_for_run(open_rid)
            if oid is None:
                raise FactoryError(
                    f"run {open_rid} is open and is not linked to a factory order (it may have been started "
                    "by hand, or the line was interrupted while opening it); finish, cancel or abandon it first"
                )
        else:
            # An order left mid-way, or one a human finished (`daedalus finish`) while the line waited.
            unsettled = [r for r in recs.values() if r.status in ("building", "waiting")]
            if not unsettled:
                return None
            oid = unsettled[0].order_id
        rec = recs[oid]
        if gitops.current_branch(self.root) != rec.branch:
            raise FactoryError(
                f"order {oid} was left {rec.status} on branch {rec.branch}; "
                f"`git checkout {rec.branch}` to resume it (or `daedalus factory park {oid}`)"
            )
        report.into = rec.into or ""
        self.into = rec.into
        self.notify(f"resuming order {oid}" + (f" (run {rec.run_id})" if rec.run_id else ""))
        rid = rec.run_id if rec.run_id and (open_rid == rec.run_id or not open_rid) else None
        if rid is None:
            outcome = self._park(oid, None, ["the line stopped before the order's run started"])
        else:
            if self.ari.needs_recovery(rid):
                for n in self.ari.recover(rid):
                    self.notify(f"recovered: {n}")
            state = self.ari.state(rid)
            order = self._orders().get(oid)
            if state.execution is ExecutionState.RUNNING:
                d = self.ari.evaluate(rid)
                if not d.acceptable and any(
                    r.fixable_by == "agent" or r.code.startswith("review:") for r in d.reasons
                ):
                    rounds = (order.rounds if order and order.rounds else None) or self.cfg.rounds
                    drive(self.ari, rid, adapter=self.adapter, max_rounds=rounds, notify=self.notify)
            outcome = self._settle(oid, rid, rounds=0)  # resumed: not a first pass
        # Only what this line spent counts against its budget, not an earlier line's spend.
        cost = (self.ari.state(rid).cost_used - rec.run_costs.get(rid, 0.0)) if rid else 0.0
        report.cost = round(report.cost + max(0.0, cost), 4)
        if outcome == "shipped":
            report.shipped.append(oid)
        elif outcome == "waiting":
            report.waiting = oid
            report.stopped = f"order {oid} still needs a human decision"
        elif outcome == "cancelled":
            report.stopped = f"order {oid} was cancelled"
        else:
            report.parked.append(oid)
        return outcome

    # ----------------------------------------------------------- process
    def _process(self, order: WorkOrder) -> tuple[str, float]:
        oid = order.order_id
        branch = f"{self.cfg.branch_prefix}{oid}"
        if not gitops.valid_branch_name(self.root, branch):
            self.ledger.append(STARTED, order=oid, branch=None, into=self.into, into_head=None)
            self._outcome(oid, None, "parked", [f"{branch!r} is not a valid branch name"])
            return "parked", 0.0
        into_head = gitops.head(self.root)
        if gitops.branch_exists(self.root, branch):
            gitops.git(self.root, "checkout", "-q", branch)
            merged = gitops.ok(self.root, "merge", "-q", "--no-edit", "--no-verify", self.into or "")
            if not merged:
                gitops.ok(self.root, "merge", "--abort")
                gitops.git(self.root, "checkout", "-q", self.into or "")
                self.ledger.append(STARTED, order=oid, branch=branch, into=self.into, into_head=into_head)
                self._outcome(
                    oid, None, "parked",
                    [f"{branch} conflicts with {self.into}; resolve it by hand, then `daedalus factory retry {oid}`"],
                )
                self.notify(f"{oid}: parked (its branch conflicts with {self.into})")
                return "parked", 0.0
            # Start the run from the integration branch with the earlier work as uncommitted
            # changes: the candidate (and every review of it) then covers everything that ships.
            gitops.git(self.root, "reset", "-q", "--mixed", into_head)
            self.notify(f"{oid}: resuming earlier work on {branch}")
        else:
            gitops.git(self.root, "checkout", "-q", "-b", branch)
        self.ledger.append(STARTED, order=oid, branch=branch, into=self.into, into_head=into_head)
        self.notify(f"{oid}: building on {branch} — {order.title}")

        try:
            rid = self.ari.start(
                order.contract_spec(self._policy), actor=self.actor, mode=order.mode or self.cfg.mode
            )
        except DaedalusError as exc:
            return self._park(oid, None, [f"could not open a run: {exc}"]), 0.0
        self.ledger.append(RUN_LINKED, order=oid, run=rid)
        rounds = order.rounds or self.cfg.rounds
        report = drive(self.ari, rid, adapter=self.adapter, max_rounds=rounds, notify=self.notify)
        outcome = self._settle(oid, rid, rounds=report.rounds, decision=report.decision)
        return outcome, self.ari.state(rid).cost_used

    # ------------------------------------------------------------ settle
    def _settle(self, oid: str, rid: str, *, rounds: int, decision: Decision | None = None) -> str:
        state = self.ari.state(rid)
        if decision is None or not decision.terminal:
            try:
                decision = self.ari.finish(rid) if state.execution is ExecutionState.RUNNING else self.ari.evaluate(rid)
            except IllegalTransition:
                decision = self.ari.evaluate(rid)
        disp = decision.disposition
        # An open blocker is why the work stopped; everything else follows from that.
        ordered = sorted(decision.reasons, key=lambda r: not r.code.startswith("blocker:"))
        reasons = [f"[{r.fixable_by}] {r.message}" for r in ordered]
        if disp is Disposition.ACCEPTED:
            return self._ship(oid, rid, decision, rounds)
        if disp is Disposition.BLOCKED:
            who = {r.fixable_by for r in decision.reasons}
            if "agent" not in who:
                self._outcome(oid, rid, "waiting", reasons)
                self.notify(f"{oid}: waiting for a human (run {rid}); the line halts here")
                return "waiting"
            try:
                blocked = any(r.code.startswith("blocker:") for r in decision.reasons)
                why = (f"a blocker was raised; fix the order or decide, then `daedalus factory retry {oid}`"
                       if blocked else "the agents ran out of rounds")
                self.ari.cancel(rid, actor=self.actor, reason=f"parked by the factory: {why}")
            except IllegalTransition:
                pass
            return self._park(oid, rid, reasons)
        outcome = "rejected" if disp is Disposition.REJECTED else "cancelled"
        self._keep_wip(oid, rid, outcome)
        self._return_to_into()
        self._outcome(oid, rid, outcome, reasons)
        self.notify(f"{oid}: {outcome}")
        return outcome

    def _park(self, oid: str, rid: str | None, reasons: list[str]) -> str:
        self._keep_wip(oid, rid, "parked")
        self._return_to_into()
        self._outcome(oid, rid, "parked", reasons)
        self.notify(f"{oid}: parked" + (f" — {reasons[0]}" if reasons else ""))
        return "parked"

    def _keep_wip(self, oid: str, rid: str | None, why: str) -> None:
        """Commit whatever the agents left on the order branch, so the next order starts clean."""
        dirty = gitops.dirty_paths(self.root, self._exclude())
        if not dirty or gitops.current_branch(self.root) == self.into:
            return
        gitops.stage_exact(self.root, dirty)
        if gitops.has_staged_changes(self.root):
            gitops.commit(self.root, f"factory: {oid} {why} (work in progress)\n\nDaedalus-Order: {oid}\n"
                          + (f"Daedalus-Run: {rid}\n" if rid else ""))

    def _return_to_into(self) -> None:
        if self.into and gitops.current_branch(self.root) != self.into:
            gitops.git(self.root, "checkout", "-q", self.into)

    # -------------------------------------------------------------- ship
    def _ship(self, oid: str, rid: str, decision: Decision, rounds: int) -> str:
        """Commit exactly the accepted candidate on top of the integration branch."""
        state = self.ari.state(rid)
        cand = self.ari.capture(state)
        # The candidate the disposition was recorded for. (`decision.candidate_id` describes the
        # tree as it is now, which after acceptance may include unreviewed edits.)
        accepted = state.accepted_candidate_id
        if not accepted or cand.candidate_id != accepted:
            return self._park(oid, rid, ["the working tree changed after acceptance; nothing was shipped"])
        rec = self.ledger.records()[oid]
        into_head = rec.into_head or gitops.head(self.root)
        if not cand.manifest["files"]:
            # Acceptance only says nothing is wrong; an empty change ships nothing. Counting it
            # would hide a Builder that does no work behind passing pre-existing checks.
            return self._park(
                oid, rid,
                ["accepted, but the order changed nothing; if it is already done, delete its order file"],
            )
        # Build the commit from into_head plus exactly the accepted files: whatever the Builder
        # staged or committed itself (possibly paths the candidate never covers) is left out.
        files = set(cand.manifest["files"])
        gitops.git(self.root, "reset", "-q", "--mixed", into_head)
        gitops.stage_exact(self.root, sorted(files))
        if not gitops.has_staged_changes(self.root):  # F6: the candidate is not empty, yet nothing differs
            return self._park(oid, rid, ["the accepted changes are already on the integration branch; nothing was shipped"])
        sha = gitops.commit(self.root, self._message(oid, rid, decision))
        shipped = set(gitops.git(self.root, "diff", "--name-only", "--no-renames", into_head, sha).splitlines())
        if shipped != files:
            why = f"the shipped commit would differ from the accepted candidate ({sorted(shipped ^ files)[:5]})"
            return self._park(oid, rid, [why + "; nothing was shipped"])
        branch = gitops.current_branch(self.root)
        gitops.git(self.root, "checkout", "-q", self.into or "")
        if branch and branch != self.into:
            if not gitops.ok(self.root, "merge", "-q", "--ff-only", branch):
                self._outcome(
                    oid, rid, "parked",
                    [(f"{self.into} moved since the order started; the accepted commit is on {branch}. "
                      f"`daedalus factory retry {oid}` rebuilds it on the new base")],
                )
                return "parked"
            gitops.ok(self.root, "branch", "-q", "-d", branch)
        self._outcome(oid, rid, "shipped", [], commit=sha, rounds=rounds)
        self.notify(f"{oid}: shipped as {sha[:10]}")
        return "shipped"

    def _message(self, oid: str, rid: str, d: Decision) -> str:
        order = self._orders().get(oid)
        title = (order.title if order else oid) or oid
        lines = [title, ""]
        if order:
            crits = order.spec.get("acceptance_criteria") or []
            for c in crits:
                lines.append(f"- {c if isinstance(c, str) else c.get('description', '')}")
            if crits:
                lines.append("")
        lines += [
            f"Daedalus-Order: {oid}",
            f"Daedalus-Run: {rid}",
            f"Daedalus-Candidate: {self.ari.state(rid).accepted_candidate_id}",
            f"Daedalus-Risk: {d.risk.effective_tier}",
        ]
        return "\n".join(lines) + "\n"


# ------------------------------------------------------- human operations
@contextmanager
def line_lock(root: Path) -> Iterator[None]:
    """Held by a running line, and by anything that moves an order's branch or run under it."""
    factory_dir(root).mkdir(parents=True, exist_ok=True)
    lock = OrchestratorLock(factory_dir(root) / "line.lock")
    try:
        lock.acquire()
    except DaedalusError as exc:
        raise FactoryError(
            f"a factory line is running ({exc}); use `daedalus factory stop` or `daedalus cancel` first"
        ) from None
    try:
        yield
    finally:
        lock.release()


def requeue(ari: Ariadne, oid: str, *, actor: str) -> None:
    """Send a parked, rejected or cancelled order back to the queue."""
    ledger = Ledger(ari.root, clock=ari.clock)
    rec = ledger.records().get(oid)
    if rec is None or rec.status == "queued":
        raise FactoryError(f"order {oid} is already queued")
    if rec.status not in ("parked", "rejected", "cancelled"):
        raise FactoryError(f"order {oid} is {rec.status}; only parked, rejected or cancelled orders can be retried")
    ledger.append(REQUEUED, order=oid, by=actor)


def park_waiting(ari: Ariadne, oid: str, *, actor: str) -> str:
    """Set aside an order the line halted on: cancel its open run, keep its work, return to the integration branch."""
    with line_lock(ari.root):
        return _park_waiting(ari, oid, actor=actor)


def _park_waiting(ari: Ariadne, oid: str, *, actor: str) -> str:
    ledger = Ledger(ari.root, clock=ari.clock)
    rec = ledger.records().get(oid)
    if rec is None or rec.status not in ("waiting", "building"):
        raise FactoryError(f"order {oid} is not waiting or building")
    root = ari.root
    if gitops.current_branch(root) not in (rec.branch, rec.into):
        raise FactoryError(f"check out {rec.branch} or {rec.into} first")
    policy = ari.config().policy
    if gitops.current_branch(root) == rec.into and gitops.dirty_paths(root, policy.candidate_exclude):
        raise FactoryError(
            f"the working tree has uncommitted changes on {rec.into}; `git checkout {rec.branch}` first so "
            "the order's work is kept on its branch"
        )
    rid = rec.run_id
    if rid and not ari.state(rid).is_finished:
        ari.cancel(rid, actor=actor, reason="parked by a human from the factory")
    if gitops.current_branch(root) == rec.branch:
        dirty = gitops.dirty_paths(root, policy.candidate_exclude)
        if dirty:
            gitops.stage_exact(root, dirty)
            if gitops.has_staged_changes(root):
                gitops.commit(root, f"factory: {oid} parked by a human (work in progress)\n\nDaedalus-Order: {oid}\n")
        if rec.into:
            gitops.git(root, "checkout", "-q", rec.into)
    cost = ari.state(rid).cost_used if rid else 0.0
    ledger.append(OUTCOME, order=oid, run=rid, outcome="parked", reasons=[f"parked by {actor}"], cost=cost)
    return rec.into or ""
