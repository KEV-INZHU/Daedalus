"""Content-addressed evidence reuse across runs (spec §7, §8).

A check that passed, or a review that approved, a byte-identical candidate under the same
policy, contract, check definition, toolchain and Daedalus version has already answered the
question. Re-running it spends minutes or dollars to learn the same thing.

The cache has no storage of its own. Its only source of truth is the hash-chained audit log:
a lookup scans prior events, and trusts them only after the whole chain verifies. A broken
chain, or any error while reading, means "no hit" (re-run), never a guess. A hit is recorded
as a new event that names its source (`reused_from`), so the log shows where every reused
result came from, and the new record binds to the *current* candidate and run like any
other evidence, which keeps the acceptance predicate unchanged.

Only results Daedalus produced itself are candidates: PASS evidence from a trusted verifier,
and launched, unexposed, first-pass, full-scope reviews with no blocking finding. FAIL, ERROR and
timeouts are never reused, and a check may opt out with `cache: false`.

Two limits are stated, not hidden. The candidate hash covers the repository's files, not what is
installed around them (site-packages, node_modules), so a source older than `cache: {max_age_s}`
(one day by default) is not reused, and a check can list `cache_inputs` (a lockfile, `.venv/pyvenv.cfg`)
whose contents join its key.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import __version__
from daedalus.audit.store import Event, EventStore
from daedalus.core import run as ev
from daedalus.core.errors import AuditIntegrityError
from daedalus.core.evidence import EvidenceRecord
from daedalus.core.policy import DEFAULT_MAX_AGE_S, canonical_json, sha256_hex
from daedalus.repository.candidate import file_sha256

if TYPE_CHECKING:  # pragma: no cover
    from daedalus.core.contracts import TaskContract
    from daedalus.core.policy import Policy

KEY_SCHEMA = 2  # bump to invalidate every earlier entry when the key's meaning changes


@dataclass(frozen=True)
class Source:
    """Where a reused result came from, as written into the new event."""

    run_id: str
    seq: int
    hash: str
    duration_s: float
    cost: float = 0.0
    basis: str = "api_equivalent"

    def to_dict(self, key: str) -> dict[str, Any]:
        return {"run_id": self.run_id, "seq": self.seq, "hash": self.hash, "cache_key": key,
                "duration_s": round(self.duration_s, 3), "cost": self.cost, "basis": self.basis}


# ---------------------------------------------------------------------- keys
def check_key(candidate_id: str, check_id: str, command: tuple[str, ...] | list[str], timeout_s: float,
              env_inputs: tuple[str, ...] | list[str], env_fingerprint: str, policy_hash: str,
              version: str = __version__, inputs_digest: str = "") -> str:
    """Everything that can change a check's result. The environment fingerprint already covers the
    executable's content, the OS and the values of declared `env_inputs`; `inputs_digest` covers the
    contents of the check's `cache_inputs` files; the rest is listed out so the key reads as what it is."""
    return sha256_hex(canonical_json({
        "schema": KEY_SCHEMA, "kind": "check", "candidate": candidate_id, "check_id": check_id,
        "command": list(command), "timeout_s": timeout_s, "env_inputs": sorted(env_inputs),
        "env_fingerprint": env_fingerprint, "policy_hash": policy_hash, "daedalus_version": version,
        "inputs_digest": inputs_digest,
    }))


def inputs_digest(root: Path, patterns: tuple[str, ...] | list[str]) -> str:
    """Hash of the files a check's `cache_inputs` name (paths or globs under `root`), by relative path
    and content. A pattern that matches nothing is part of the digest, so a file appearing or going
    away changes it. Empty for a check with no `cache_inputs`."""
    if not patterns:
        return ""
    found = []
    for pat in patterns:
        files = []
        for p in sorted(root.glob(pat)):
            try:
                if p.is_file():
                    files.append([p.relative_to(root).as_posix(), file_sha256(p)])
            except OSError:
                files.append([p.relative_to(root).as_posix(), "unreadable"])
        found.append([pat, files])
    return sha256_hex(canonical_json(found))


def contract_digest(c: TaskContract) -> str:
    """The parts of a contract a reviewer judges code against. The run id and version are left out
    on purpose: they name the run, not the requirement."""
    d = c.to_dict()
    keys = ("objective", "scope", "acceptance_criteria", "invariants", "hard_constraints",
            "design_decisions", "required_checks", "verification_plan", "base_revision", "risk_tier")
    return sha256_hex(canonical_json({k: d[k] for k in keys}))


def adapter_identity(adapter: Any) -> str | None:
    """Adapter name and the model its review sessions run on, or None when no model is configured
    (`review_model` / `model`): the adapter would use whatever its CLI defaults to, which the key
    cannot name, so such a review is neither reused nor a source."""
    model = getattr(adapter, "review_model", None) or getattr(adapter, "model", None)
    if not model:
        return None
    name = getattr(adapter, "name", None) or f"{type(adapter).__module__}.{type(adapter).__qualname__}"
    return f"{name}:{model}"


def review_inputs(perspective: str, policy_hash: str, contract: TaskContract, template_hash: str,
                  adapter: Any, version: str = __version__, first_pass: bool = True) -> dict[str, Any]:
    """Everything but the candidate that a review verdict depends on. Stored in the review event so a
    later lookup can recompute the key from the event itself. Only first-pass reviews (no prior
    findings shown to the reviewer) are keyed, and `first_pass` says so in the key."""
    return {"perspective": perspective, "policy_hash": policy_hash, "contract": contract_digest(contract),
            "prompt": template_hash, "adapter": adapter_identity(adapter), "first_pass": first_pass,
            "daedalus_version": version}


def review_key_from(candidate_id: str, inputs: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({
        "schema": KEY_SCHEMA, "kind": "review", "candidate": candidate_id, "perspective": inputs.get("perspective"),
        "policy_hash": inputs.get("policy_hash"), "contract": inputs.get("contract"), "prompt": inputs.get("prompt"),
        "adapter": inputs.get("adapter"), "first_pass": inputs.get("first_pass"),
        "daedalus_version": inputs.get("daedalus_version"),
    }))


def review_key(candidate_id: str, perspective: str, policy_hash: str, contract: TaskContract,
               template_hash: str, adapter: Any, version: str = __version__, first_pass: bool = True) -> str:
    return review_key_from(candidate_id, review_inputs(perspective, policy_hash, contract, template_hash,
                                                       adapter, version, first_pass))


# -------------------------------------------------------------------- lookup
class EvidenceCache:
    """Read-only view over the audit log. Ask `trusted()` first, then look up. The chain is verified
    once per instance; a source older than `max_age_s` (by `clock`) is never reused."""

    def __init__(self, store: EventStore, *, max_age_s: float = DEFAULT_MAX_AGE_S,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.max_age_s = max_age_s
        self.clock = clock
        self._trusted: bool | None = None

    def trusted(self) -> bool:
        """Whether the log is intact. Anything short of a verified chain disables reuse."""
        if self._trusted is None:
            try:
                self.store.verify_chain()
                self._trusted = True
            except (AuditIntegrityError, sqlite3.Error, ValueError):
                self._trusted = False
        return self._trusted

    def _fresh(self, e: Event) -> bool:
        return self.clock() - e.ts <= self.max_age_s

    def find_check(self, key: str, *, policy: Policy, candidate_id: str) -> tuple[EvidenceRecord, Source] | None:
        """Newest earlier PASS under this key that Daedalus itself ran. The key is recomputed from
        each recorded event, never read from it."""
        invalidated = {(e.run_id, e.payload.get("evidence_id"))
                       for e in self.store.events_of_type(ev.EVIDENCE_INVALIDATED)}
        for e in reversed(self.store.events_of_type(ev.EVIDENCE_RECORDED, candidate_id=candidate_id)):
            if e.payload.get("reused_from") or not self._fresh(e):
                continue  # only originals: a reuse points at its original, never chains
            try:
                rec = EvidenceRecord.from_dict(e.payload["record"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                rec.result != "PASS" or rec.exit_code != 0 or rec.error or not rec.verifier_trusted
                or rec.verifier_id not in policy.trusted_verifiers
                or (e.run_id, rec.evidence_id) in invalidated
            ):
                continue
            k = check_key(rec.candidate_id, rec.check_id, rec.command, rec.timeout_s,
                          tuple(rec.environment.get("env_inputs", {})), rec.env_fingerprint, rec.policy_hash,
                          str(rec.environment.get("daedalus_version", "")),
                          str(e.payload.get("cache_inputs_digest", "")))
            if k == key:
                return rec, Source(e.run_id, e.seq, e.hash, max(rec.ended_at - rec.started_at, 0.0))
        return None

    def find_review(self, key: str, *, candidate_id: str, blocking: frozenset[str] | set[str]
                    ) -> tuple[dict[str, Any], Source] | None:
        """Newest earlier approving, launched, independent, full review of `candidate_id` under this
        key (first-pass reviews only: the key says so). As for checks, the key is recomputed from the
        event's own candidate and stored inputs, never read from it."""
        for e in reversed(self.store.events_of_type(ev.REVIEW_RECORDED, candidate_id=candidate_id)):
            if not self._fresh(e):
                continue
            r = e.payload.get("review", {})
            inputs = r.get("cache_inputs")
            if not isinstance(inputs, dict) or r.get("reused_from") or r.get("candidate_id") != candidate_id:
                continue
            if review_key_from(str(r.get("candidate_id")), inputs) != key or r.get("cache_key") != key:
                continue
            if (
                r.get("source") != "launched" or r.get("exposed_to") or r.get("scope", "full") != "full"
                or any(str(f.get("severity")) in blocking for f in r.get("findings", ()))
            ):
                continue
            return {**r, "reviewer": e.actor}, Source(
                e.run_id, e.seq, e.hash, float(r.get("duration_s") or 0.0), float(r.get("cost") or 0.0),
                str(r.get("cost_basis") or "api_equivalent"),
            )
        return None


def reused_record(src: EvidenceRecord, *, run_id: str, contract_version: int, mandatory: bool,
                  now: float) -> EvidenceRecord:
    """The source's observation, bound to this run and its current contract version. Timing is
    when it was reused; the source's own duration travels in `reused_from`."""
    return replace(src, run_id=run_id, attempt=1, contract_version=contract_version, mandatory=mandatory,
                   started_at=now, ended_at=now)
