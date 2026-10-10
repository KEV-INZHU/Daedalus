"""Versioned task contracts (spec §4).

A contract is built from a short spec (often just an objective) and validated
against the pinned policy. Mandatory checks are always added; a contract can
add required checks but never remove one. Amendments create a new version and
make every artifact bound to the previous version stale.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

from daedalus.core.errors import ContractError, PolicyError
from daedalus.core.policy import Policy, canonical_json, sha256_hex

MODES = ("gate", "review", "council")
PERSPECTIVES = ("socrates", "mozi", "aristotle", "james")

# Reviews a mode adds on top of the tier floor. Modes only ever add; the tier
# floor from policy always applies (spec §11: Mode A skips Council stages but
# never bypasses verification, policy floors or authorization).
MODE_REVIEWS: dict[str, tuple[str, ...]] = {
    "gate": (),
    "review": ("aristotle",),
    "council": PERSPECTIVES,
}

AMENDABLE = (
    "objective",
    "scope",
    "acceptance_criteria",
    "invariants",
    "risk_tier",
    "required_checks",
    "verification_plan",
    "approval_requirements",
    "budget",
    "hard_constraints",
    "design_decisions",
)


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    description: str
    checks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.criterion_id, "description": self.description, "checks": list(self.checks)}


@dataclass(frozen=True)
class Budget:
    max_attempts: int = 20
    max_wall_seconds: float = 86400.0
    max_cost: float = 50.0
    max_workers: int = 1
    max_cash: float = 0.0  # money actually billed (cash routes); 0 allows none

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskContract:
    run_id: str
    contract_version: int
    objective: str
    scope: tuple[str, ...]
    acceptance_criteria: tuple[Criterion, ...]
    invariants: tuple[str, ...]
    risk_tier: str
    required_checks: tuple[str, ...]
    base_revision: str | None
    verification_plan: str
    approval_requirements: tuple[str, ...]
    budget: Budget
    hard_constraints: tuple[str, ...] = ()
    design_decisions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "contract_version": self.contract_version,
            "objective": self.objective,
            "scope": list(self.scope),
            "acceptance_criteria": [c.to_dict() for c in self.acceptance_criteria],
            "invariants": list(self.invariants),
            "risk_tier": self.risk_tier,
            "required_checks": list(self.required_checks),
            "base_revision": self.base_revision,
            "verification_plan": self.verification_plan,
            "approval_requirements": list(self.approval_requirements),
            "budget": self.budget.to_dict(),
            "hard_constraints": list(self.hard_constraints),
            "design_decisions": list(self.design_decisions),
        }

    def spec(self) -> dict[str, Any]:
        d = self.to_dict()
        return {k: d[k] for k in AMENDABLE}

    @property
    def contract_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def criterion(self, criterion_id: str) -> Criterion:
        for c in self.acceptance_criteria:
            if c.criterion_id == criterion_id:
                return c
        raise ContractError(f"unknown acceptance criterion {criterion_id!r}")


# ---------------------------------------------------------------------- build
def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list")
    return tuple(str(v) for v in value)


def _criteria(items: Any, required: tuple[str, ...]) -> tuple[Criterion, ...]:
    if not items:
        return (Criterion("C1", "All required checks pass on the final candidate", required),)
    if not isinstance(items, (list, tuple)):
        raise ContractError("acceptance_criteria must be a list")
    out: list[Criterion] = []
    seen: set[str] = set()
    for i, item in enumerate(items, 1):
        if isinstance(item, str):
            c = Criterion(f"C{i}", item)
        elif isinstance(item, dict):
            desc = item.get("description") or item.get("text")
            if not desc:
                raise ContractError(f"acceptance criterion {i} needs a description")
            c = Criterion(str(item.get("id", f"C{i}")), str(desc), _str_tuple(item.get("checks"), "checks"))
        else:
            raise ContractError(f"acceptance criterion {i} must be a string or mapping")
        if c.criterion_id in seen:
            raise ContractError(f"duplicate acceptance criterion id {c.criterion_id!r}")
        for chk in c.checks:
            if chk not in required:
                raise ContractError(
                    f"criterion {c.criterion_id} references {chk!r}, which is not a required check"
                )
        seen.add(c.criterion_id)
        out.append(c)
    return tuple(out)


def _budget(raw: Any, policy: Policy) -> Budget:
    names = {f.name for f in fields(Budget)}
    merged: dict[str, Any] = asdict(Budget())
    merged.update({k: v for k, v in policy.default_budget.items() if k in names})
    if raw:
        if not isinstance(raw, dict):
            raise ContractError("budget must be a mapping")
        unknown = set(raw) - names
        if unknown:
            raise ContractError(f"unknown budget fields: {sorted(unknown)}")
        merged.update(raw)
    try:
        budget = Budget(
            max_attempts=int(merged["max_attempts"]),
            max_wall_seconds=float(merged["max_wall_seconds"]),
            max_cost=float(merged["max_cost"]),
            max_workers=int(merged["max_workers"]),
            max_cash=float(merged["max_cash"]),
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid budget: {exc}") from exc
    if budget.max_attempts < 1 or not budget.max_wall_seconds > 0 or not budget.max_cost >= 0:
        raise ContractError("budget limits must be positive")
    if not 0 <= budget.max_cash < math.inf:  # NaN fails this too, so it can never disable the cap
        raise ContractError("budget.max_cash must be a finite, non-negative amount")
    cap = int(policy.default_budget.get("max_workers", 1))
    if not 1 <= budget.max_workers <= cap:
        raise ContractError(f"max_workers must be between 1 and the policy cap ({cap})")
    return budget


def build_contract(
    run_id: str,
    spec: dict[str, Any],
    policy: Policy,
    base_revision: str | None,
    version: int = 1,
) -> TaskContract:
    if not isinstance(spec, dict):
        raise ContractError("contract spec must be a mapping")
    unknown = set(spec) - set(AMENDABLE)
    if unknown:
        raise ContractError(f"unknown contract fields: {sorted(unknown)}")
    objective = str(spec.get("objective") or "").strip()
    if not objective:
        raise ContractError("contract needs an objective")

    tier = str(spec.get("risk_tier") or policy.tiers[0])
    try:
        policy.tier_rank(tier)
    except PolicyError as exc:
        raise ContractError(str(exc)) from exc

    requested = _str_tuple(spec.get("required_checks"), "required_checks")
    missing = [c for c in requested if c not in policy.checks]
    if missing:
        raise ContractError(f"required checks not defined by policy: {missing}")
    required = tuple(dict.fromkeys(policy.mandatory_checks() + requested))

    approvals = _str_tuple(spec.get("approval_requirements"), "approval_requirements")
    for a in approvals:
        if a not in policy.authorities:
            raise ContractError(f"approval requirement {a!r} has no authority in policy")

    scope = _str_tuple(spec.get("scope"), "scope") or ("**",)

    return TaskContract(
        run_id=run_id,
        contract_version=version,
        objective=objective,
        scope=scope,
        acceptance_criteria=_criteria(spec.get("acceptance_criteria"), required),
        invariants=_str_tuple(spec.get("invariants"), "invariants"),
        risk_tier=tier,
        required_checks=required,
        base_revision=base_revision,
        verification_plan=str(spec.get("verification_plan") or ""),
        approval_requirements=approvals,
        budget=_budget(spec.get("budget"), policy),
        hard_constraints=_str_tuple(spec.get("hard_constraints"), "hard_constraints"),
        design_decisions=_str_tuple(spec.get("design_decisions"), "design_decisions"),
    )


def contract_from_dict(d: dict[str, Any]) -> TaskContract:
    """Rehydrate a contract that was already validated when it was recorded."""
    return TaskContract(
        run_id=d["run_id"],
        contract_version=int(d["contract_version"]),
        objective=d["objective"],
        scope=tuple(d["scope"]),
        acceptance_criteria=tuple(
            Criterion(c["id"], c["description"], tuple(c.get("checks", ()))) for c in d["acceptance_criteria"]
        ),
        invariants=tuple(d.get("invariants", ())),
        risk_tier=d["risk_tier"],
        required_checks=tuple(d["required_checks"]),
        base_revision=d.get("base_revision"),
        verification_plan=d.get("verification_plan", ""),
        approval_requirements=tuple(d.get("approval_requirements", ())),
        budget=Budget(**d["budget"]),
        hard_constraints=tuple(d.get("hard_constraints", ())),
        design_decisions=tuple(d.get("design_decisions", ())),
    )


def amend_contract(
    old: TaskContract, changes: dict[str, Any], policy: Policy
) -> tuple[TaskContract, list[str]]:
    """Return the next contract version and the list of fields that changed."""
    bad = set(changes) - set(AMENDABLE)
    if bad:
        raise ContractError(f"fields cannot be amended: {sorted(bad)}")
    spec = old.spec()
    spec.update(changes)
    new = build_contract(old.run_id, spec, policy, old.base_revision, old.contract_version + 1)
    before, after = old.to_dict(), new.to_dict()
    changed = [k for k in AMENDABLE if before[k] != after[k]]
    if not changed:
        raise ContractError("amendment makes no material change")
    return new, changed


def removed_hard_constraints(old: TaskContract, new: TaskContract) -> list[str]:
    return [c for c in old.hard_constraints if c not in new.hard_constraints]
