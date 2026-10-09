"""Versioned, hash-pinned policy (spec §8, §9, §10).

Policy is trusted repository configuration. A run pins the policy hash at
creation; if the policy in force differs, acceptance is impossible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from daedalus.core.errors import PolicyError
from daedalus.core.state_machine import Disposition

AGENT_PREFIX = "agent:"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def is_agent(principal: str) -> bool:
    return principal.startswith(AGENT_PREFIX)


@dataclass(frozen=True)
class CheckDefinition:
    check_id: str
    command: tuple[str, ...]
    timeout_s: float = 600.0
    max_attempts: int = 2
    mandatory: bool = True
    env_inputs: tuple[str, ...] = ()
    transient_exit_codes: tuple[int, ...] = ()
    retry_on_timeout: bool = False

    @property
    def definition_hash(self) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "check_id": self.check_id,
                    "command": list(self.command),
                    "timeout_s": self.timeout_s,
                    "env_inputs": list(self.env_inputs),
                }
            )
        )


@dataclass(frozen=True)
class TierRequirement:
    reviews: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    independent_review: bool = False


@dataclass(frozen=True)
class ActionDefinition:
    """A restricted action. `command` is optional: without one, Daedalus only
    records the authorized intent and a human performs the action."""

    name: str
    command: tuple[str, ...] | None = None
    requires_acceptance: bool = True


@dataclass(frozen=True)
class Floor:
    name: str
    patterns: tuple[str, ...]
    tier: str


@dataclass(frozen=True)
class Policy:
    policy_version: str
    tiers: tuple[str, ...]
    tier_requirements: dict[str, TierRequirement]
    path_floors: tuple[Floor, ...]
    change_floors: tuple[Floor, ...]
    behavioral_floors: tuple[Floor, ...]
    checks: dict[str, CheckDefinition]
    trusted_verifiers: frozenset[str]
    authorities: dict[str, tuple[str, ...]]
    restricted_actions: frozenset[str]
    violation_disposition: Disposition
    default_budget: dict[str, Any]
    candidate_exclude: tuple[str, ...]
    lockfile_patterns: tuple[str, ...]
    blocking_severities: frozenset[str] = frozenset({"BLOCKER"})
    actions: dict[str, ActionDefinition] = field(default_factory=dict)
    approval_ttl_seconds: float = 86400.0
    allowed_degradations: frozenset[str] = frozenset()
    # Whether a Plato ruling that overrules a disputed finding resolves it. Off by
    # default: the ruling is advisory and a human resolves the finding.
    arbitration_resolves_findings: bool = False
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    # ------------------------------------------------------------------ hashing
    @property
    def policy_hash(self) -> str:
        return sha256_hex(canonical_json(self.raw))

    # -------------------------------------------------------------------- tiers
    def tier_rank(self, tier: str) -> int:
        try:
            return self.tiers.index(tier)
        except ValueError as exc:
            raise PolicyError(f"unknown risk tier {tier!r}") from exc

    def max_tier(self, *tiers: str) -> str:
        return max(tiers, key=self.tier_rank)

    @property
    def highest_tier(self) -> str:
        return self.tiers[-1]

    def requirement(self, tier: str) -> TierRequirement:
        return self.tier_requirements.get(tier, TierRequirement())

    # ------------------------------------------------------------ authorities
    def has_authority(self, principal: str, authority: str) -> bool:
        """Agents never hold authority, whatever the policy file says."""
        if is_agent(principal):
            return False
        return any(fnmatchcase(principal, p) for p in self.authorities.get(authority, ()))

    def mandatory_checks(self) -> tuple[str, ...]:
        return tuple(c.check_id for c in self.checks.values() if c.mandatory)


# ---------------------------------------------------------------------- loading
def _floors(items: Any, where: str) -> tuple[Floor, ...]:
    if items is None:
        return ()
    if isinstance(items, dict):
        items = [{"name": k, **v} for k, v in items.items()]
    out = []
    for i, f in enumerate(items):
        if "tier" not in f or "patterns" not in f:
            raise PolicyError(f"{where}[{i}] needs 'patterns' and 'tier'")
        out.append(Floor(str(f.get("name", f"{where}-{i}")), tuple(f["patterns"]), str(f["tier"])))
    return tuple(out)


def _strict_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise PolicyError(f"{key} must be true or false (got {value!r})")
    return value


def policy_from_dict(raw: dict[str, Any]) -> Policy:
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a mapping")
    for key in ("policy_version", "tiers", "checks"):
        if key not in raw:
            raise PolicyError(f"policy missing required key {key!r}")
    tiers = tuple(str(t) for t in raw["tiers"])
    if len(tiers) == 0 or len(set(tiers)) != len(tiers):
        raise PolicyError("tiers must be a non-empty list of unique names")

    reqs = {}
    for tier, r in (raw.get("tier_requirements") or {}).items():
        if tier not in tiers:
            raise PolicyError(f"tier_requirements names unknown tier {tier!r}")
        r = r or {}
        reqs[tier] = TierRequirement(
            reviews=tuple(r.get("reviews", ())),
            approvals=tuple(r.get("approvals", ())),
            independent_review=bool(r.get("independent_review", False)),
        )

    checks = {}
    for cid, c in (raw.get("checks") or {}).items():
        cmd = c.get("command")
        if not cmd or not isinstance(cmd, list):
            raise PolicyError(f"check {cid!r} needs a command list")
        attempts = int(c.get("max_attempts", 2))
        if attempts < 1:
            raise PolicyError(f"check {cid!r} max_attempts must be >= 1")
        checks[cid] = CheckDefinition(
            check_id=cid,
            command=tuple(str(x) for x in cmd),
            timeout_s=float(c.get("timeout_s", 600)),
            max_attempts=attempts,
            mandatory=bool(c.get("mandatory", True)),
            env_inputs=tuple(c.get("env_inputs", ())),
            transient_exit_codes=tuple(int(x) for x in c.get("transient_exit_codes", ())),
            retry_on_timeout=bool(c.get("retry_on_timeout", False)),
        )

    floors_path = _floors(raw.get("path_floors"), "path_floors")
    floors_change = _floors(raw.get("change_floors"), "change_floors")
    floors_behavior = _floors(raw.get("behavioral_floors"), "behavioral_floors")
    for f in floors_path + floors_change + floors_behavior:
        if f.tier not in tiers:
            raise PolicyError(f"floor {f.name!r} names unknown tier {f.tier!r}")

    try:
        vdisp = Disposition(raw.get("violation_disposition", "REJECTED"))
    except ValueError as exc:
        raise PolicyError("violation_disposition must be REJECTED or BLOCKED") from exc
    if vdisp not in (Disposition.REJECTED, Disposition.BLOCKED):
        raise PolicyError("violation_disposition must be REJECTED or BLOCKED")

    authorities = {k: tuple(v or ()) for k, v in (raw.get("authorities") or {}).items()}
    for k, principals in authorities.items():
        for p in principals:
            if is_agent(p) or p in ("*", "agent*"):
                raise PolicyError(f"authority {k!r} may not be granted to agents ({p!r})")

    restricted = frozenset(raw.get("restricted_actions", ()))
    for a in restricted:
        if a not in authorities:
            raise PolicyError(f"restricted action {a!r} has no authority defined")
    actions = {}
    for name, a in (raw.get("actions") or {}).items():
        if name not in restricted:
            raise PolicyError(f"action {name!r} is not listed in restricted_actions")
        a = a or {}
        cmd = a.get("command")
        actions[name] = ActionDefinition(
            name=name,
            command=tuple(str(x) for x in cmd) if cmd else None,
            requires_acceptance=bool(a.get("requires_acceptance", True)),
        )

    severities = frozenset(str(s) for s in raw.get("blocking_severities", ["BLOCKER"]))
    if "BLOCKER" not in severities:
        raise PolicyError("blocking_severities must include BLOCKER")

    candidate = raw.get("candidate") or {}
    lockfiles = ()
    for f in floors_change:
        if f.name == "dependency":
            lockfiles = f.patterns

    return Policy(
        policy_version=str(raw["policy_version"]),
        tiers=tiers,
        tier_requirements=reqs,
        path_floors=floors_path,
        change_floors=floors_change,
        behavioral_floors=floors_behavior,
        checks=checks,
        trusted_verifiers=frozenset(raw.get("trusted_verifiers", ())),
        authorities=authorities,
        restricted_actions=restricted,
        violation_disposition=vdisp,
        default_budget=dict(raw.get("default_budget") or {}),
        candidate_exclude=tuple(candidate.get("exclude", ())),
        lockfile_patterns=lockfiles,
        blocking_severities=severities,
        actions=actions,
        approval_ttl_seconds=float(raw.get("approval_ttl_seconds", 86400)),
        allowed_degradations=frozenset(raw.get("allowed_degradations", ())),
        arbitration_resolves_findings=_strict_bool(raw, "arbitration_resolves_findings"),
        raw=raw,
    )


def load_policy(path: str | Path) -> Policy:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {path}") from exc
    return policy_from_dict(raw)


def default_policy_text() -> str:
    return (Path(__file__).resolve().parent.parent / "data" / "default_policy.yaml").read_text(
        encoding="utf-8"
    )
