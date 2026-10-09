"""Scoped authorization (spec §10).

An approval binds to an action, a candidate, a contract version, an approver
holding the matching authority under the pinned policy, an expiry, and
single-use semantics. Validity is re-derived every time it is needed; a
record that was valid when granted is not presumed valid now. Ariadne
enforces authorization but can never manufacture it: agents hold no
authority whatever the policy file says.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from daedalus.core.state_machine import AuthorizationState

if TYPE_CHECKING:  # pragma: no cover
    from daedalus.core.run import RunState


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    run_id: str
    action: str
    decision: str  # APPROVED | DENIED
    approver: str
    candidate_id: str
    contract_version: int
    granted_at: float
    expires_at: float | None
    single_use: bool
    interactive: bool
    rationale: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApprovalRecord:
        return cls(**d)


def authorization_status(
    state: RunState, action: str, candidate_id: str, now: float
) -> tuple[AuthorizationState, str, ApprovalRecord | None]:
    recs = [a for a in state.approvals if a.action == action]
    ask = f"A human runs `daedalus approve {action}`."
    if not recs:
        return AuthorizationState.PENDING, f"`{action}` approval is required. {ask}", None
    a = recs[-1]
    if not state.policy.has_authority(a.approver, action):
        return AuthorizationState.PENDING, f"{a.approver} lacks `{action}` authority. {ask}", a
    if a.decision == "DENIED":
        why = f": {a.rationale}" if a.rationale else ""
        return AuthorizationState.DENIED, f"`{action}` was denied by {a.approver}{why}", a
    if a.contract_version != state.contract.contract_version:
        return (
            AuthorizationState.PENDING,
            (
                f"`{action}` approval was for contract v{a.contract_version}; the contract is now "
                f"v{state.contract.contract_version}. {ask}"
            ),
            a,
        )
    if a.candidate_id != candidate_id:
        return (
            AuthorizationState.PENDING,
            (
                f"`{action}` was approved for candidate {a.candidate_id[:12]}; the candidate is now "
                f"{candidate_id[:12]}. {ask}"
            ),
            a,
        )
    if a.expires_at is not None and now >= a.expires_at:
        return AuthorizationState.EXPIRED, f"`{action}` approval expired. {ask}", a
    if a.single_use and a.approval_id in state.consumed_approvals:
        return AuthorizationState.PENDING, f"`{action}` approval was already used. {ask}", a
    return AuthorizationState.APPROVED, f"`{action}` approved by {a.approver}.", a
