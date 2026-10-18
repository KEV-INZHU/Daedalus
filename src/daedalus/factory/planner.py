"""The planning station: one large goal in, draft work orders out.

Vitruvius reads the repository (read-only) and proposes orders. Every proposal
is validated exactly like a hand-written order; anything invalid is dropped and
reported, never repaired. The result is written as draft files and nothing
runs until a human reviews and commits them: the line only builds committed
orders.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from daedalus.adapters.base import Adapter, AgentRequest, require
from daedalus.core.errors import DaedalusError
from daedalus.factory import gitops
from daedalus.factory.ledger import Ledger
from daedalus.factory.line import factory_config
from daedalus.factory.orders import ORDER_ID, FactoryError, WorkOrder, _cycles, load_orders, parse_order, slug
from daedalus.orchestration import prompts
from daedalus.orchestration.ariadne import Ariadne
from daedalus.repository.candidate import capture

PLAN_TIMEOUT_S = 1800.0
FIELDS = ("title", "objective", "acceptance_criteria", "scope", "depends_on", "priority", "invariants",
          "design_decisions", "risk_tier")


@dataclass
class Plan:
    summary: str
    orders: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    cost: float = 0.0


def _snapshot(root: Any, exclude: tuple[str, ...]) -> tuple[str, str]:
    """HEAD plus the content hash of every uncommitted change: a commit, an edit to an already
    dirty file and a new file all change it. (Paths the candidate excludes are not covered.)"""
    head = gitops.head(root)
    return head, capture(root, head, exclude).candidate_id


def plan_orders(
    ari: Ariadne,
    adapter: Adapter,
    goal: str,
    *,
    max_orders: int = 8,
    notify: Callable[[str], None] = lambda _: None,
) -> Plan:
    if not goal.strip():
        raise FactoryError("describe the goal to plan")
    root = ari.root
    policy = ari.config().policy
    require(
        adapter.capabilities,
        {"launch_agent": True, "structured_results": True},
        policy.allowed_degradations,
    )
    cfg = factory_config(root)
    existing, _ = load_orders(root, cfg.orders_dir, policy)
    recs = Ledger(root).records()
    listing = [
        {"id": o.order_id, "status": recs[o.order_id].status if o.order_id in recs else "queued",
         "title": o.title}
        for o in existing
    ]
    before = _snapshot(root, policy.candidate_exclude)
    prompt = prompts.factory_plan_prompt(goal, listing, sorted(policy.checks), max_orders)
    notify("Vitruvius is planning work orders")
    res = adapter.run_agent(AgentRequest("factory", "plan", "vitruvius", prompt, root, PLAN_TIMEOUT_S, read_only=True))
    if _snapshot(root, policy.candidate_exclude) != before:
        raise FactoryError("the planner changed the working tree; its plan was discarded")
    if res.status != "COMPLETED":
        raise FactoryError(f"planner {res.status.lower()}: {res.error or 'no result'}")
    body = res.structured
    if not isinstance(body, dict) or not isinstance(body.get("orders"), list):
        raise FactoryError("the planner returned no valid JSON plan")
    plan = Plan(summary=str(body.get("summary", "")), cost=res.cost)

    taken = {o.order_id for o in existing}
    proposed: dict[str, tuple[dict[str, Any], WorkOrder]] = {}
    renamed: dict[str, str] = {}
    for i, item in enumerate(body["orders"][:max_orders], 1):
        if not isinstance(item, dict):
            plan.dropped.append(f"#{i}: not a mapping")
            continue
        asked = str(item.get("id") or "")
        oid = asked if ORDER_ID.match(asked) else slug(asked or str(item.get("objective", "")))
        base, n = oid, 2
        while oid in taken or oid in proposed:
            oid, n = f"{base}-{n}", n + 1
        if asked and asked not in taken:  # a dependency on an existing id means the existing order
            renamed[asked] = oid
        if isinstance(item.get("title"), str):  # a blank title is no title: the objective names the order
            item["title"] = item["title"].strip()
        spec = {k: item[k] for k in FIELDS if item.get(k) not in (None, [], "")}
        try:
            order = parse_order(oid, spec, f"{cfg.orders_dir}/{oid}.yml", policy)
        except DaedalusError as exc:
            plan.dropped.append(f"{oid}: {exc}")
            continue
        proposed[oid] = (spec, order)

    # Dependencies may name the planner's own ids (possibly renamed) or existing orders.
    for oid, (spec, _) in list(proposed.items()):
        deps = [renamed.get(d, d) for d in spec.get("depends_on", [])]
        unknown = [d for d in deps if d not in proposed and d not in taken]
        if unknown:
            plan.dropped.append(f"{oid}: depends on unknown orders {unknown}")
            del proposed[oid]
            continue
        if deps:
            spec["depends_on"] = deps
        proposed[oid] = (spec, parse_order(oid, spec, f"{cfg.orders_dir}/{oid}.yml", policy))
    changed = True
    while changed:  # dropping an order dooms whatever depended on it
        changed = False
        for oid, (spec, _) in list(proposed.items()):
            if any(d not in proposed and d not in taken for d in spec.get("depends_on", [])):
                plan.dropped.append(f"{oid}: a dependency was dropped")
                del proposed[oid]
                changed = True
    for oid in _cycles({k: v[1] for k, v in proposed.items()}):
        plan.dropped.append(f"{oid}: part of a dependency cycle")
        proposed.pop(oid, None)
    plan.orders = [(oid, spec) for oid, (spec, _) in proposed.items()]
    return plan
