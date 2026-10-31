"""Run one trusted check as a subprocess (spec §8, §13).

Checks run with a bounded environment: variables that look like credentials
are removed unless the check declares them in `env_inputs`. A timeout, a
crash, or a missing executable is ERROR, never PASS.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from daedalus.core.policy import CheckDefinition

_SECRET = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY|_KEY$|^AWS_|^GH_|^GITHUB_TOKEN)", re.IGNORECASE
)


@dataclass(frozen=True)
class CheckOutcome:
    result: str  # PASS | FAIL | ERROR
    exit_code: int | None
    error: str | None
    timed_out: bool
    stdout_sha256: str | None
    stderr_sha256: str | None
    log_path: str | None
    started_at: float
    ended_at: float


def scrubbed_env(allowed: tuple[str, ...]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in allowed or not _SECRET.search(k)}


def run_check(
    check: CheckDefinition, cwd: Path, log_dir: Path, *, clock=time.time, log_name: str = "check",
    run_env: dict[str, str] | None = None,
) -> CheckOutcome:
    """`run_env` is what Daedalus itself tells every check about the run (DAEDALUS_BASE_REVISION);
    it is added after scrubbing and is not part of the check's definition or fingerprint."""
    log_dir.mkdir(parents=True, exist_ok=True)
    started = clock()
    try:
        proc = subprocess.run(
            list(check.command),
            cwd=str(cwd),
            env={**scrubbed_env(check.env_inputs), **(run_env or {})},
            capture_output=True,
            timeout=check.timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        out, err = exc.stdout or b"", exc.stderr or b""
        log = _write_log(log_dir, log_name, check, out, err, None)
        return CheckOutcome(
            "ERROR",
            None,
            f"timed out after {check.timeout_s:g}s",
            True,
            _sha(out),
            _sha(err),
            log,
            started,
            clock(),
        )
    except OSError as exc:
        return CheckOutcome(
            "ERROR",
            None,
            f"could not start {check.command[0]!r}: {exc}",
            False,
            None,
            None,
            None,
            started,
            clock(),
        )
    log = _write_log(log_dir, log_name, check, proc.stdout, proc.stderr, proc.returncode)
    result = "PASS" if proc.returncode == 0 else "FAIL"
    note = missing_tool(proc.stderr) if result == "FAIL" else None
    return CheckOutcome(
        result, proc.returncode, note, False, _sha(proc.stdout), _sha(proc.stderr), log, started, clock()
    )


_NO_MODULE = re.compile(rb"No module named '?([A-Za-z0-9_.]+)'?")


def missing_tool(stderr: bytes) -> str | None:
    """Why a check failed when the reason is its tool, not the code: a Python module that is not
    installed here (a fresh container usually). Said so the failure is not read as a test failure."""
    m = _NO_MODULE.search(stderr or b"")
    if m is None:
        return None
    mod = m.group(1).decode("ascii", "replace")
    return (f"`{mod}` is not installed where the check ran, so this is not a test result. Install it (in a "
            "fresh container, `harness: {bootstrap: ...}` in .daedalus.yml can install it, e.g. "
            "`pip install -e \".[dev]\"`)")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_log(
    log_dir: Path, name: str, check: CheckDefinition, out: bytes, err: bytes, code: int | None
) -> str | None:
    """Logs are retained for humans; the evidence binds to the output hashes, so a
    failed log write loses convenience, not provenance."""
    path = log_dir / f"{name}.log"
    header = f"$ {' '.join(check.command)}\n# exit: {code}\n--- stdout ---\n".encode()
    try:
        path.write_bytes(header + out + b"\n--- stderr ---\n" + err)
    except OSError:
        return None
    return str(path)
