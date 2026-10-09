"""Risk classification (spec §9).

effective_tier = max(declared, path_floor, change_floor, behavioral_floor)

The declared tier is the builder's proposal and can only raise the result.
Incomplete candidate capture escalates to the highest tier: uncertainty never
yields a low classification. Only an authorized, candidate-bound risk
exception can lower the effective tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from daedalus.core.paths import match_any
from daedalus.core.policy import Policy


@dataclass(frozen=True)
class RiskContribution:
    source: str  # declared | path | change | behavioral | uncertainty | exception
    name: str
    tier: str
    path: str | None = None


@dataclass(frozen=True)
class RiskAssessment:
    effective_tier: str
    computed_tier: str
    contributions: tuple[RiskContribution, ...]
    exception_applied: bool = False

    def explain(self) -> str:
        top = [c for c in self.contributions if c.tier == self.computed_tier and c.source != "declared"]
        if not top:
            return f"tier {self.effective_tier}"
        c = top[0]
        where = f" via {c.path}" if c.path else ""
        extra = f" (+{len(top) - 1} more)" if len(top) > 1 else ""
        note = " — lowered by an authorized risk exception" if self.exception_applied else ""
        return f"tier {self.effective_tier}: {c.source} floor `{c.name}`{where}{extra}{note}"


def assess(
    policy: Policy,
    changed_paths: tuple[str, ...] | list[str],
    declared_tier: str,
    *,
    capture_complete: bool = True,
    exception_tier: str | None = None,
) -> RiskAssessment:
    contributions = [RiskContribution("declared", "contract", declared_tier)]
    for source, floors in (
        ("path", policy.path_floors),
        ("change", policy.change_floors),
        ("behavioral", policy.behavioral_floors),
    ):
        for floor in floors:
            for p in changed_paths:
                if match_any(p, floor.patterns):
                    contributions.append(RiskContribution(source, floor.name, floor.tier, p))
    if not capture_complete:
        contributions.append(RiskContribution("uncertainty", "incomplete-capture", policy.highest_tier))

    computed = policy.max_tier(*(c.tier for c in contributions))
    if exception_tier is None:
        return RiskAssessment(computed, computed, tuple(contributions))
    effective = policy.max_tier(exception_tier, declared_tier)
    return RiskAssessment(
        effective,
        computed,
        tuple(contributions) + (RiskContribution("exception", "risk-exception", exception_tier),),
        exception_applied=policy.tier_rank(effective) < policy.tier_rank(computed),
    )
