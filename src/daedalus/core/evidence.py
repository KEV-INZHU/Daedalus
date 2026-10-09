"""Revision-bound evidence (spec §7.2, §7.3, §8).

An evidence record says what a verifier observed, against which candidate,
contract version, policy, check definition and execution environment. A check
is PASS only while every one of those bindings still holds; otherwise the
evidence is STALE. Untrusted verifiers fail closed. Provenance is not
reproducibility: the record proves what was observed, not that the verifier
was sound.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import __version__
from daedalus.core.policy import CheckDefinition, canonical_json, sha256_hex
from daedalus.core.state_machine import CheckState

if TYPE_CHECKING:  # pragma: no cover
    from daedalus.core.acceptance import Observation
    from daedalus.core.run import RunState

RESULTS = ("PASS", "FAIL", "ERROR")


@dataclass(frozen=True)
class EvidenceRecord:
    run_id: str
    check_id: str
    attempt: int
    candidate_id: str
    contract_version: int
    policy_version: str
    policy_hash: str
    definition_hash: str
    command: tuple[str, ...]
    result: str
    exit_code: int | None
    error: str | None
    verifier_id: str
    verifier_trusted: bool
    env_fingerprint: str
    environment: dict[str, Any]
    stdout_sha256: str | None
    stderr_sha256: str | None
    log_path: str | None
    started_at: float
    ended_at: float
    timeout_s: float
    mandatory: bool

    def __post_init__(self) -> None:
        if self.result not in RESULTS:
            raise ValueError(f"evidence result must be one of {RESULTS}")

    def _body(self) -> dict[str, Any]:
        d = asdict(self)
        d["command"] = list(self.command)
        return d

    @property
    def evidence_id(self) -> str:
        return sha256_hex(canonical_json(self._body()))[:32]

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **self._body()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvidenceRecord:
        d = {k: v for k, v in d.items() if k != "evidence_id"}
        d["command"] = tuple(d["command"])
        return cls(**d)


# --------------------------------------------------------------- environment
_EXE_CACHE: dict[tuple[str, int, int], str] = {}


def _executable_hash(path: str) -> str | None:
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
        if key not in _EXE_CACHE:
            from daedalus.repository.candidate import file_sha256

            _EXE_CACHE[key] = file_sha256(Path(path))
        return _EXE_CACHE[key]
    except OSError:
        return None


def environment_fingerprint(check: CheckDefinition, verifier_id: str) -> tuple[str, dict[str, Any]]:
    """Identity of everything outside the candidate that can change a result.

    Environment-variable inputs are recorded as hashes, never raw values.
    """
    exe = shutil.which(check.command[0]) if check.command else None
    environment = {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "executable": exe,
        "executable_sha256": _executable_hash(exe) if exe else None,
        "env_inputs": {n: sha256_hex(os.environ.get(n, "\0unset")) for n in check.env_inputs},
        "daedalus_version": __version__,
        "definition_hash": check.definition_hash,
        "verifier_id": verifier_id,
    }
    return sha256_hex(canonical_json(environment)), environment


# -------------------------------------------------------------------- status
def check_status(state: RunState, obs: Observation, check_id: str) -> tuple[CheckState, str]:
    """Derive one required check's state from recorded evidence and live state."""
    recs = [e for e in state.evidence if e.check_id == check_id]
    if not recs:
        return CheckState.NOT_RUN, f"`{check_id}` has not run in this run. Run `daedalus verify`."
    e = recs[-1]
    cand = obs.candidate.candidate_id
    if e.evidence_id in state.invalidated:
        return (
            CheckState.STALE,
            f"`{check_id}` evidence was invalidated ({state.invalidated[e.evidence_id]}). Run `daedalus verify`.",
        )
    if e.verifier_id not in state.policy.trusted_verifiers:
        return CheckState.ERROR, (
            f"`{check_id}` was reported by untrusted verifier {e.verifier_id!r}; it cannot count as PASS."
        )
    if e.candidate_id != cand:
        return CheckState.STALE, (
            f"`{check_id}` {'passed' if e.result == 'PASS' else 'last ran (' + e.result + ')'} on candidate "
            f"{e.candidate_id[:12]} but the working tree changed afterward (now {cand[:12]}). Run `daedalus verify`."
        )
    if e.contract_version != state.contract.contract_version:
        return CheckState.STALE, (
            f"`{check_id}` ran under contract v{e.contract_version}; the contract is now "
            f"v{state.contract.contract_version}. Run `daedalus verify`."
        )
    defn = state.policy.checks.get(check_id)
    if defn is None:
        return CheckState.ERROR, f"`{check_id}` is not defined by the pinned policy."
    if e.policy_hash != state.policy_hash or e.definition_hash != defn.definition_hash:
        return CheckState.STALE, f"`{check_id}` ran under a different policy or check definition."
    if e.env_fingerprint != obs.env_fingerprints.get(check_id):
        return CheckState.STALE, (
            f"`{check_id}`'s environment or toolchain changed since it ran. Run `daedalus verify`."
        )
    if e.result == "PASS":
        return CheckState.PASS, f"`{check_id}` passed on candidate {cand[:12]}."
    if e.result == "FAIL":
        where = f" Log: {e.log_path}" if e.log_path else ""
        return CheckState.FAIL, f"`{check_id}` failed (exit {e.exit_code}) on candidate {cand[:12]}.{where}"
    return CheckState.ERROR, f"`{check_id}` errored on candidate {cand[:12]}: {e.error}"
