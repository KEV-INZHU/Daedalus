"""Work orders: the factory's input.

An order is a Daedalus contract plus routing. It lives as a YAML file in the
orders directory (default `factory/orders/`), and the file name is its id:

    # factory/orders/rate-limit.yml
    objective: Add rate limiting to the public API
    acceptance_criteria:
      - Requests over 100/min from one key get HTTP 429
      - The limit is configurable per key
    scope: ["src/api/**", "tests/api/**"]
    depends_on: [api-keys]     # ship these first
    priority: 10               # higher runs first (default 0)
    mode: review               # gate | review | council (default: the factory's mode)
    rounds: 3                  # build/verify/review rounds before parking

Every other contract field (`invariants`, `hard_constraints`, `design_decisions`,
`risk_tier`, `required_checks`, `budget`, `verification_plan`, ...) passes
through unchanged.

Plain-text criteria are bound to the order's required checks: the Builder must
prove each one with a test those checks run, and in review mode a reviewer
judges whether the tests really do. That keeps the line lights-out. A criterion
written as a mapping with `checks: []` still needs a human attestation, and an
order that uses one will wait for a human.

Orders are trusted input, like policy: the line only runs orders that are
committed, so every spec it builds has been seen by a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from daedalus.core.contracts import MODES, build_contract
from daedalus.core.errors import ContractError, DaedalusError
from daedalus.core.policy import Policy

ORDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ROUTING = ("depends_on", "priority", "mode", "rounds")
SUFFIXES = (".yml", ".yaml")

DEFAULT_VERIFICATION = (
    "Every acceptance criterion is demonstrated by an automated test that the required checks run."
)


class FactoryError(DaedalusError):
    """The factory refused to start, continue, or accept an order."""


@dataclass(frozen=True)
class WorkOrder:
    order_id: str
    path: str  # repository-relative
    spec: dict[str, Any]  # contract fields only
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    mode: str | None = None
    rounds: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def title(self) -> str:
        """A commit subject: the objective's first sentence, at most 72 characters, cut at a word."""
        text = " ".join(str(self.spec.get("objective", "")).split())
        first = text.split(". ", 1)[0].rstrip(".")
        if len(first) <= 72:
            return first
        return first[:71].rsplit(" ", 1)[0].rstrip(",;:") + "…"

    def contract_spec(self, policy: Policy) -> dict[str, Any]:
        """The contract this order runs under, with text criteria bound to its checks."""
        spec = dict(self.spec)
        requested = [str(c) for c in spec.get("required_checks") or []]
        checks = list(dict.fromkeys(policy.mandatory_checks() + tuple(requested)))
        crits = spec.get("acceptance_criteria")
        if isinstance(crits, list):
            spec["acceptance_criteria"] = [
                {"description": c, "checks": checks} if isinstance(c, str) else c for c in crits
            ]
        spec.setdefault("verification_plan", DEFAULT_VERIFICATION)
        return spec


def parse_order(order_id: str, raw: Any, path: str, policy: Policy) -> WorkOrder:
    """Validate one order. The contract part is checked by the same code a run uses."""
    if not ORDER_ID.match(order_id) or ".." in order_id:
        raise ContractError(
            f"{path}: order id {order_id!r} must be lowercase letters, digits, '.', '_' or '-' "
            "(it names a git branch)"
        )
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: an order must be a mapping")
    raw = dict(raw)
    deps = raw.get("depends_on") or []
    if isinstance(deps, str):
        deps = [deps]
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise ContractError(f"{path}: depends_on must be a list of order ids")
    if order_id in deps:
        raise ContractError(f"{path}: an order cannot depend on itself")
    mode = raw.get("mode")
    if mode is not None and mode not in MODES:
        raise ContractError(f"{path}: mode must be one of {MODES}")
    try:
        priority = int(raw.get("priority", 0))
        rounds = None if raw.get("rounds") is None else int(raw["rounds"])
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{path}: priority and rounds must be integers") from exc
    if rounds is not None and not 1 <= rounds <= 20:
        raise ContractError(f"{path}: rounds must be between 1 and 20")
    spec = {k: v for k, v in raw.items() if k not in ROUTING}
    order = WorkOrder(order_id, path, spec, tuple(deps), priority, mode, rounds, raw)
    try:
        build_contract("validate", order.contract_spec(policy), policy, None)
    except ContractError as exc:
        raise ContractError(f"{path}: {exc}") from exc
    return order


def load_orders(root: Path, orders_dir: str, policy: Policy) -> tuple[list[WorkOrder], dict[str, str]]:
    """Every order in the directory, plus the files that failed validation (path -> error).

    A broken order never stops the line: it is reported and skipped, as is any
    order that depends on an id that does not exist.
    """
    d = root / orders_dir
    orders: dict[str, WorkOrder] = {}
    errors: dict[str, str] = {}
    if not d.is_dir():
        return [], errors
    for p in sorted(d.iterdir()):
        if p.suffix not in SUFFIXES or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            order = parse_order(p.stem, raw, rel, policy)
        except (yaml.YAMLError, OSError, DaedalusError) as exc:
            errors[rel] = str(exc)
            continue
        if order.order_id in orders:
            errors[rel] = f"duplicate order id {order.order_id!r}"
            continue
        orders[order.order_id] = order
    for o in list(orders.values()):
        missing = [d for d in o.depends_on if d not in orders]
        if missing:
            errors[o.path] = f"depends on unknown orders {missing}"
            del orders[o.order_id]
    cyclic = _cycles(orders)
    for oid in cyclic:
        errors[orders[oid].path] = "part of a dependency cycle"
        del orders[oid]
    return list(orders.values()), errors


def _cycles(orders: dict[str, WorkOrder]) -> set[str]:
    """Ids on (or downstream of) a dependency cycle: they can never become ready."""
    state: dict[str, int] = {}  # 1 visiting, 2 ok, 3 doomed
    doomed: set[str] = set()

    def visit(oid: str) -> bool:
        if state.get(oid) == 1:
            return False
        if oid in state:
            return state[oid] == 2
        state[oid] = 1
        ok = all(visit(d) for d in orders[oid].depends_on if d in orders)
        state[oid] = 2 if ok else 3
        if not ok:
            doomed.add(oid)
        return ok

    for oid in orders:
        visit(oid)
    return doomed


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:48].rstrip("-") or "order")


def write_order(root: Path, orders_dir: str, order_id: str, body: dict[str, Any], policy: Policy) -> Path:
    """Write a new order file after validating it. Never overwrites an existing order."""
    d = root / orders_dir
    rel = f"{orders_dir}/{order_id}.yml"
    parse_order(order_id, body, rel, policy)
    if any((d / f"{order_id}{s}").exists() for s in SUFFIXES):
        raise FactoryError(f"order {order_id!r} already exists in {orders_dir}/")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{order_id}.yml"
    p.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    return p
