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
from daedalus.verification.evidence_cache import check_key, inputs_digest, reused_record

if TYPE_CHECKING:  # pragma: no cover
    from daedalus.orchestration.ariadne import Ariadne

SYSTEM = "system:proof-gate"


def _retryable(defn: CheckDefinition, out: CheckOutcome) -> bool:
    if out.timed_out:
        return defn.retry_on_timeout
    return out.result == "FAIL" and out.exit_code in defn.transient_exit_codes


def _reuse(ari: Ariadne, cache, state, defn: CheckDefinition, required: set[str]) -> EvidenceRecord | None:
    """Record an earlier PASS of this exact check on this exact candidate as evidence of this run.
    The audit log says where it came from; `check_status` judges it like any other record."""
    cand = ari.capture(state)
    if not cand.complete:
        return None  # an unreadable file means the hash does not describe the tree
    fp, _ = environment_fingerprint(defn, ari.verifier_id)
    key = check_key(cand.candidate_id, defn.check_id, defn.command, defn.timeout_s, defn.env_inputs, fp,
                    state.policy_hash,
                    inputs_digest=inputs_digest(ari.root, defn.cache_inputs))
    found = cache.find_check(key, policy=state.policy, candidate_id=cand.candidate_id)
    if found is None:
        return None
    src, source = found
    rec = reused_record(src, run_id=state.run_id, contract_version=state.contract.contract_version,
                        mandatory=defn.check_id in required, now=ari.clock())
    ari._append(state.run_id, ev.EVIDENCE_RECORDED, SYSTEM,
                {"record": rec.to_dict(), "reused_from": source.to_dict(key)})
    return rec


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
        # Reuse needs a Daedalus-run check on a trusted verifier and an intact audit chain.
        cache = ari.evidence_cache("checks") if trusted else None

        for check_id in order:
            defn = policy.checks[check_id]
            if cache is not None and defn.cache and (hit := _reuse(ari, cache, state, defn, required)):
                produced.append(hit)
                continue
            for attempt in range(1, defn.max_attempts + 1):
                state = ari.state(run_id)
                if budget_exhausted(state, ari.clock()):
                    return produced
                before = ari.capture(state)
                digest = inputs_digest(ari.root, defn.cache_inputs)
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
                # The `cache_inputs` digest is kept only if those files held still while the check ran.
                stable = digest == inputs_digest(ari.root, defn.cache_inputs)
                payload = {"key": key, "record": rec.to_dict()}
                if stable:
                    payload["cache_inputs_digest"] = digest
                ari._append(run_id, ev.EVIDENCE_RECORDED, SYSTEM, payload)
                produced.append(rec)
                if not (trusted and result != "PASS" and _retryable(defn, out)):
                    break
    return produced
