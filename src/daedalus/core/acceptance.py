"""The acceptance predicate and disposition derivation (spec §5.5, §6).

`evaluate` is a pure function of persisted run state plus a live observation
(current candidate, current policy hash, contract file hash, environment
fingerprints, time). No agent input reaches it except through recorded,
validated events. It returns every unmet condition as an actionable reason,
tagged with who can fix it, so harnesses can route agent-fixable blockers
back to the agent and human-only ones to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daedalus.core.authorization import authorization_status
from daedalus.core.contracts import MODE_REVIEWS
from daedalus.core.evidence import check_status
from daedalus.core.paths import match_any
from daedalus.core.risk import RiskAssessment, assess
from daedalus.core.run import RunState
from daedalus.core.state_machine import AuthorizationState, CheckState, Disposition
from daedalus.repository.candidate import CandidateManifest

AGENT, HUMAN, SYSTEM = "agent", "human", "system"


@dataclass(frozen=True)
class Observation:
    candidate: CandidateManifest
    policy_hash: str | None  # None when the current policy cannot be loaded
    policy_error: str | None
    contract_file_hash: str | None
    env_fingerprints: dict[str, str]
    now: float


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    fixable_by: str  # agent | human | system

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fixable_by": self.fixable_by}


@dataclass
class Decision:
    disposition: Disposition
    reasons: list[Reason]
    acceptable: bool  # the predicate holds apart from recording FINISHED
    terminal: bool
    candidate_id: str
    risk: RiskAssessment
    check_states: dict[str, tuple[CheckState, str]] = field(default_factory=dict)
    required_reviews: tuple[str, ...] = ()
    required_approvals: tuple[str, ...] = ()

    @property
    def agent_fixable(self) -> list[Reason]:
        return [r for r in self.reasons if r.fixable_by == AGENT]

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "acceptable": self.acceptable,
            "terminal": self.terminal,
            "candidate_id": self.candidate_id,
            "risk_tier": self.risk.effective_tier,
            "risk": self.risk.explain(),
            "checks": {k: {"state": v[0].value, "detail": v[1]} for k, v in self.check_states.items()},
            "required_reviews": list(self.required_reviews),
            "required_approvals": list(self.required_approvals),
            "reasons": [r.to_dict() for r in self.reasons],
        }


# ------------------------------------------------------------------- helpers
def valid_exception_tier(state: RunState, candidate_id: str) -> str | None:
    for e in reversed(state.risk_exceptions):
        if e["candidate_id"] == candidate_id and state.policy.has_authority(e["by"], "risk_exception"):
            return e["tier"]
    return None


def risk_for(state: RunState, candidate: CandidateManifest) -> RiskAssessment:
    return assess(
        state.policy,
        candidate.changed_paths,
        state.contract.risk_tier,
        capture_complete=candidate.complete,
        exception_tier=valid_exception_tier(state, candidate.candidate_id),
    )


def required_reviews(state: RunState, tier: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(state.policy.requirement(tier).reviews + MODE_REVIEWS[state.mode]))


def required_approvals(state: RunState, tier: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(state.policy.requirement(tier).approvals + state.contract.approval_requirements)
    )


def review_satisfied(state: RunState, perspective: str, candidate_id: str, independent: bool) -> bool:
    for r in state.reviews:
        if (
            r.perspective == perspective
            and r.candidate_id == candidate_id
            and r.contract_version == state.contract.contract_version
            and r.reviewer not in state.candidate_authors
            and (not independent or (r.source == "launched" and not r.exposed_to))
        ):
            return True
    return False


def observed_violations(state: RunState, obs: Observation) -> list[tuple[str, str, str]]:
    """(key, message, fixable_by) for violations visible in the live state."""
    out = []
    if obs.policy_hash != state.policy_hash:
        now = f"unloadable: {obs.policy_error}" if obs.policy_hash is None else obs.policy_hash[:12]
        out.append(
            (
                "policy_drift",
                f"the effective policy changed after the run started (pinned {state.policy_hash[:12]}, now {now}). "
                "A changed policy cannot certify this run: revert the change or start a new run.",
                HUMAN,
            )
        )
    if obs.contract_file_hash != state.contract_file_hash:
        out.append(
            (
                "contract_drift",
                "the contract file was modified outside `daedalus amend`. Unauthorized contract drift cannot "
                "yield acceptance.",
                HUMAN,
            )
        )
    outside = [p for p in obs.candidate.changed_paths if not match_any(p, state.contract.scope)]
    if outside:
        shown = ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else "")
        out.append(
            (
                "scope:" + ",".join(outside),
                f"changes outside the contract scope {list(state.contract.scope)}: {shown}",
                AGENT,
            )
        )
    return out


def budget_exhausted(state: RunState, now: float) -> list[str]:
    b = state.contract.budget
    out = []
    if state.attempts_used >= b.max_attempts:
        out.append(f"attempts {state.attempts_used}/{b.max_attempts}")
    if now - state.created_at >= b.max_wall_seconds:
        out.append(f"wall time {int(now - state.created_at)}s/{int(b.max_wall_seconds)}s")
    if state.cost_used >= b.max_cost:
        out.append(f"cost {state.cost_used:.2f}/{b.max_cost:.2f}")
    return out


# ------------------------------------------------------------------ predicate
def evaluate(state: RunState, obs: Observation, *, finishing: bool = False) -> Decision:
    policy = state.policy
    cand = obs.candidate
    cid = cand.candidate_id
    risk = risk_for(state, cand)
    tier = risk.effective_tier
    reviews_needed = required_reviews(state, tier)
    approvals_needed = required_approvals(state, tier)
    checks = {c: check_status(state, obs, c) for c in state.contract.required_checks}

    def decide(
        disposition: Disposition, reasons: list[Reason], acceptable: bool = False, terminal: bool = False
    ) -> Decision:
        return Decision(
            disposition, reasons, acceptable, terminal, cid, risk, checks, reviews_needed, approvals_needed
        )

    # 1. A valid terminal acceptance is preserved; later edits need a new run.
    if state.disposition is Disposition.ACCEPTED:
        notes = []
        if cid != state.accepted_candidate_id:
            notes.append(
                Reason(
                    "post_acceptance_change",
                    f"the working tree changed after candidate {(state.accepted_candidate_id or '')[:12]} was accepted; "
                    "start a new run to verify these changes",
                    HUMAN,
                )
            )
        return decide(Disposition.ACCEPTED, notes, acceptable=True, terminal=True)
    if state.disposition is not None:
        recorded = [Reason(r["code"], r["message"], r["fixable_by"]) for r in state.disposition_reasons]
        return decide(state.disposition, recorded, terminal=True)

    # 2. Cancellation in progress resolves to CANCELLED once work drains.
    if state.cancel_requested:
        return decide(
            Disposition.BLOCKED,
            [Reason("cancelling", "cancellation in progress; waiting for active work to stop", SYSTEM)],
        )

    rejecting: list[Reason] = []
    reasons: list[Reason] = []

    # 3. Denied required authorization rejects.
    approval_states = {a: authorization_status(state, a, cid, obs.now) for a in approvals_needed}
    for action, (st, msg, _) in approval_states.items():
        if st is AuthorizationState.DENIED:
            rejecting.append(Reason(f"authorization_denied:{action}", msg, HUMAN))

    # 4. Declared-unresolvable mandatory condition rejects.
    if state.unresolvable:
        rejecting.append(Reason("unresolvable", f"declared unresolvable: {state.unresolvable}", HUMAN))

    # Unauthorized scope, policy or contract changes (recorded or live).
    seen = set()
    violation_reasons = []
    for vid, v in state.open_violations.items():
        seen.add(v["key"])
        violation_reasons.append(Reason(f"violation:{vid}", v["message"], v.get("fixable_by", HUMAN)))
    for key, msg, who in observed_violations(state, obs):
        if key not in seen:
            violation_reasons.append(Reason("violation:" + key.split(":")[0], msg, who))
    if violation_reasons:
        if policy.violation_disposition is Disposition.REJECTED:
            rejecting.extend(violation_reasons)
        else:
            reasons.extend(violation_reasons)

    # 5. Everything else that keeps the predicate from holding.
    if not state.contract.required_checks:
        reasons.append(
            Reason(
                "no_checks",
                "no required checks are configured; acceptance needs at least one trusted check. "
                "Add one under `checks:` in .daedalus.yml.",
                HUMAN,
            )
        )
    if state.active_workers:
        reasons.append(Reason("active_work", f"{len(state.active_workers)} worker(s) still running", SYSTEM))
    if state.checks_in_flight:
        reasons.append(
            Reason(
                "checks_in_flight",
                "verification is still in progress (or was interrupted; run `daedalus reconcile`)",
                SYSTEM,
            )
        )

    for check_id, (st, msg) in checks.items():
        if st is not CheckState.PASS:
            untrusted = st is CheckState.ERROR and "untrusted verifier" in msg
            reasons.append(Reason(f"check:{check_id}", msg, HUMAN if untrusted else AGENT))

    for crit in state.contract.acceptance_criteria:
        if crit.checks:
            continue  # disposed by its checks, already reported above
        att = [
            a
            for a in state.attestations
            if a["criterion_id"] == crit.criterion_id
            and a["candidate_id"] == cid
            and a["contract_version"] == state.contract.contract_version
            and policy.has_authority(a["by"], "attest")
        ]
        if not att:
            reasons.append(
                Reason(
                    f"criterion:{crit.criterion_id}",
                    f"criterion {crit.criterion_id} ({crit.description!r}) has no check and no attestation for "
                    f"candidate {cid[:12]}. A human runs `daedalus attest {crit.criterion_id} pass --evidence ...`.",
                    HUMAN,
                )
            )
        elif att[-1]["status"] != "PASS":
            reasons.append(
                Reason(
                    f"criterion:{crit.criterion_id}",
                    f"criterion {crit.criterion_id} was attested FAIL by {att[-1]['by']}: {att[-1]['evidence']}",
                    AGENT,
                )
            )

    for f in state.open_findings:
        if f.severity.value in policy.blocking_severities:
            reasons.append(
                Reason(
                    f"finding:{f.finding_id}",
                    f"{f.severity.value} from {f.perspective} ({f.finding_id}): {f.title}. "
                    f"Required change: {f.required_change or 'see finding'}",
                    AGENT,
                )
            )
    for bid, b in state.open_blockers.items():
        reasons.append(Reason(f"blocker:{bid}", f"blocker {bid}: {b['description']}", AGENT))

    independent = policy.requirement(tier).independent_review
    for p in reviews_needed:
        if not review_satisfied(state, p, cid, independent):
            why = f"tier {tier}" if p in policy.requirement(tier).reviews else f"{state.mode} mode"
            article = "an" if p[:1] in ("a", "e", "i", "o", "u") else "a"
            kind = "an independent, Daedalus-launched" if independent else article
            reasons.append(
                Reason(
                    f"review:{p}",
                    f"{why} requires {kind} {p} review of candidate {cid[:12]}. Run `daedalus review {p}`.",
                    AGENT,
                )
            )

    for action, (st, msg, _) in approval_states.items():
        if st not in (AuthorizationState.APPROVED, AuthorizationState.DENIED):
            reasons.append(Reason(f"approval:{action}", msg, HUMAN))

    for aid, a in state.unknown_actions.items():
        reasons.append(
            Reason(
                f"action_unknown:{aid}",
                f"restricted action `{a['action']}` ({aid}) may have executed before an interruption; "
                f"a human must run `daedalus reconcile --action {aid} --occurred yes|no` before anything retries it",
                HUMAN,
            )
        )

    if rejecting:
        return decide(Disposition.REJECTED, rejecting + reasons)

    # Budget exhaustion with anything unmet rejects; with nothing unmet the
    # run is simply done (exhaustion means no more work, not "throw it away").
    spent = budget_exhausted(state, obs.now)
    if reasons and spent:
        return decide(
            Disposition.REJECTED,
            [
                Reason(
                    "budget_exhausted",
                    "budget exhausted (" + ", ".join(spent) + ") with conditions unmet",
                    HUMAN,
                )
            ]
            + reasons,
        )
    if reasons:
        return decide(Disposition.BLOCKED, reasons)

    # 6. The complete predicate holds.
    if finishing:
        return decide(Disposition.ACCEPTED, [], acceptable=True)
    return decide(
        Disposition.BLOCKED,
        [Reason("not_finished", "every condition holds; run `daedalus finish` to record acceptance", AGENT)],
        acceptable=True,
    )
