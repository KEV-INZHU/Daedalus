"""The Proof Gate: run trusted checks and bind their results to the candidate (spec §7, §8, §13).

The gate produces evidence; it never accepts a run. For each check it records
its intent (`check.started`) before running anything, so a crash leaves a
traceable in-flight check that recovery turns into ERROR rather than a
presumed PASS. The candidate is captured before and after each check: if the
working tree moved while the check ran, the result describes no single
candidate and is recorded as ERROR. An untrusted verifier is never run at all;
its "evidence" would fail closed anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from daedalus.core import run as ev
from daedalus.core.acceptance import budget_exhausted
from daedalus.core.evidence import EvidenceRecord, environment_fingerprint
from daedalus.core.policy import CheckDefinition
from daedalus.verification.check_runner import CheckOutcome, run_check

if TYPE_CHECKING:  # pragma: no cover
    from daedalus.orchestration.ariadne import Ariadne

SYSTEM = "system:proof-gate"


def _retryable(defn: CheckDefinition, out: CheckOutcome) -> bool:
    if out.timed_out:
        return defn.retry_on_timeout
    return out.result == "FAIL" and out.exit_code in defn.transient_exit_codes


def verify(ari: Ariadne, run_id: str, *, only: set[str] | None = None) -> list[EvidenceRecord]:
    """Run the run's checks against the current candidate. Returns the new evidence."""
    produced: list[EvidenceRecord] = []
    with ari.lock:
        state = ari._running(run_id)
        ari.note_violations(state, ari.observe(state))
        ari.record_candidate(run_id, actor=SYSTEM, authored=False, label="verify")
        state = ari.state(run_id)
        policy = state.policy
        required = set(state.contract.required_checks)
        # Required checks first; optional policy checks are recorded but never block.
        order = list(state.contract.required_checks) + [c for c in policy.checks if c not in required]
        if only is not None:
            unknown = only - set(order)
            if unknown:
                from daedalus.core.errors import ContractError

                raise ContractError(f"unknown checks: {sorted(unknown)}")
            order = [c for c in order if c in only]
        trusted = ari.verifier_id in policy.trusted_verifiers
        log_dir = ari.state_dir / "runs" / run_id / "logs"

        for check_id in order:
            defn = policy.checks[check_id]
            for attempt in range(1, defn.max_attempts + 1):
                state = ari.state(run_id)
                if budget_exhausted(state, ari.clock()):
                    return produced
                before = ari.capture(state)
                key = f"{check_id}#{state.last_seq + 1}"
                fp, env = environment_fingerprint(defn, ari.verifier_id)
                ari._append(
                    run_id,
                    ev.CHECK_STARTED,
                    SYSTEM,
                    {
                        "key": key,
                        "check_id": check_id,
                        "attempt": attempt,
                        "candidate_id": before.candidate_id,
                        "started_at": ari.clock(),
                    },
                )
                if trusted:
                    out = run_check(
                        defn, ari.root, log_dir, clock=ari.clock, log_name=f"{check_id}-{key.split('#')[1]}"
                    )
                    error = out.error
                    result = out.result
                    after = ari.capture(state)
                    if after.candidate_id != before.candidate_id:
                        result = "ERROR"
                        error = (
                            f"the working tree changed while `{check_id}` ran "
                            f"({before.short} -> {after.short}); the result describes no single candidate"
                        )
                else:
                    now = ari.clock()
                    out = CheckOutcome("ERROR", None, None, False, None, None, None, now, now)
                    result = "ERROR"
                    error = f"untrusted verifier {ari.verifier_id!r}: check not executed"
                rec = EvidenceRecord(
                    run_id=run_id,
                    check_id=check_id,
                    attempt=attempt,
                    candidate_id=before.candidate_id,
                    contract_version=state.contract.contract_version,
                    policy_version=policy.policy_version,
                    policy_hash=state.policy_hash,
                    definition_hash=defn.definition_hash,
                    command=defn.command,
                    result=result,
                    exit_code=out.exit_code,
                    error=error,
                    verifier_id=ari.verifier_id,
                    verifier_trusted=trusted,
                    env_fingerprint=fp,
                    environment=env,
                    stdout_sha256=out.stdout_sha256,
                    stderr_sha256=out.stderr_sha256,
                    log_path=out.log_path,
                    started_at=out.started_at,
                    ended_at=out.ended_at,
                    timeout_s=defn.timeout_s,
                    mandatory=check_id in required,
                )
                ari._append(run_id, ev.EVIDENCE_RECORDED, SYSTEM, {"key": key, "record": rec.to_dict()})
                produced.append(rec)
                if not (trusted and result != "PASS" and _retryable(defn, out)):
                    break
    return produced
