"""State vocabularies and legal transition tables (spec §5, §12).

Execution, verification, authorization and final disposition are tracked
independently. Illegal transitions raise; the control plane never repairs
state silently.
"""

from __future__ import annotations

from enum import Enum

from daedalus.core.errors import IllegalTransition


class ExecutionState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    FINISHED = "FINISHED"


class CheckState(str, Enum):
    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    FAIL = "FAIL"
    STALE = "STALE"
    ERROR = "ERROR"


class AuthorizationState(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class Disposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class WorkerState(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED_STALE = "REJECTED_STALE"


class Severity(str, Enum):
    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    ADVISORY = "ADVISORY"


EXECUTION_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.QUEUED: frozenset(
        {ExecutionState.RUNNING, ExecutionState.CANCELLING, ExecutionState.FINISHED}
    ),
    ExecutionState.RUNNING: frozenset({ExecutionState.CANCELLING, ExecutionState.FINISHED}),
    ExecutionState.CANCELLING: frozenset({ExecutionState.FINISHED}),
    ExecutionState.FINISHED: frozenset(),  # terminal: resuming requires a new run
}

# COMPLETED is "produced a proposal"; it is not yet terminal for integration
# purposes and may still be superseded or rejected as stale.
WORKER_TRANSITIONS: dict[WorkerState, frozenset[WorkerState]] = {
    WorkerState.PENDING: frozenset(
        {WorkerState.DISPATCHED, WorkerState.CANCELLED, WorkerState.SUPERSEDED}
    ),
    WorkerState.DISPATCHED: frozenset(
        {
            WorkerState.RUNNING,
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.TIMED_OUT,
            WorkerState.CANCELLED,
            WorkerState.REJECTED_STALE,
        }
    ),
    WorkerState.RUNNING: frozenset(
        {
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.TIMED_OUT,
            WorkerState.CANCELLED,
            WorkerState.REJECTED_STALE,
        }
    ),
    WorkerState.COMPLETED: frozenset({WorkerState.SUPERSEDED, WorkerState.REJECTED_STALE}),
    WorkerState.FAILED: frozenset(),
    WorkerState.TIMED_OUT: frozenset(),
    WorkerState.CANCELLED: frozenset(),
    WorkerState.SUPERSEDED: frozenset(),
    WorkerState.REJECTED_STALE: frozenset(),
}

WORKER_ACTIVE = frozenset({WorkerState.DISPATCHED, WorkerState.RUNNING})
WORKER_TERMINAL = frozenset(
    {
        WorkerState.FAILED,
        WorkerState.TIMED_OUT,
        WorkerState.CANCELLED,
        WorkerState.SUPERSEDED,
        WorkerState.REJECTED_STALE,
    }
)


def check_execution_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in EXECUTION_TRANSITIONS[current]:
        raise IllegalTransition(f"execution {current.value} -> {target.value} is not legal")


def check_worker_transition(current: WorkerState, target: WorkerState) -> None:
    if target not in WORKER_TRANSITIONS[current]:
        raise IllegalTransition(f"worker {current.value} -> {target.value} is not legal")
