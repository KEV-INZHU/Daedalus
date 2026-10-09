"""The adapter contract (spec §2.2; docs/adapter-contract.md).

The harness supplies agent execution; Daedalus owns the lifecycle. An adapter
translates one into the other and *declares* what it can guarantee. The core
never assumes a capability: a run that needs one the adapter lacks fails
closed, unless policy lists that shortfall under `allowed_degradations`.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daedalus.core.errors import CapabilityError

CANCELLATION_LEVELS = ("none", "cooperative", "hard")
ISOLATION_LEVELS = ("none", "worktree", "sandbox")


@dataclass(frozen=True)
class Capabilities:
    launch_agent: bool  # can Daedalus start an agent session itself?
    cancellation: str  # none | cooperative | hard
    isolation: str  # none | worktree | sandbox
    identity: str  # launched (Daedalus started it, knows who it is) | self_reported
    structured_results: bool  # can return machine-readable results
    cost_reporting: bool
    status_query: bool  # can report on a session after an orchestrator restart
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "launch_agent": self.launch_agent,
            "cancellation": self.cancellation,
            "isolation": self.isolation,
            "identity": self.identity,
            "structured_results": self.structured_results,
            "cost_reporting": self.cost_reporting,
            "status_query": self.status_query,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class AgentRequest:
    run_id: str
    task_id: str
    role: str  # brunel | socrates | mozi | aristotle | james | plato
    prompt: str
    cwd: Path
    timeout_s: float = 3600.0
    read_only: bool = False


@dataclass(frozen=True)
class AgentResult:
    status: str  # COMPLETED | FAILED | TIMED_OUT | CANCELLED
    output: str = ""
    structured: dict[str, Any] | None = None
    cost: float = 0.0
    session_id: str | None = None
    error: str | None = None


class Adapter(ABC):
    name: str = "adapter"
    capabilities: Capabilities

    @abstractmethod
    def run_agent(self, request: AgentRequest) -> AgentResult:
        """Run one agent session to completion (or timeout) and return its result."""

    def cancel(self, session_id: str) -> bool:
        """Request cancellation. Returns True only if the adapter could act on it."""
        return False

    def status(self, session_id: str) -> AgentResult | None:
        """Report a session's outcome after a restart; None when unknowable."""
        return None


def require(capabilities: Capabilities, needs: dict[str, Any], allowed_degradations: frozenset[str]) -> None:
    """Fail closed when the adapter cannot guarantee what the run needs."""
    missing: list[str] = []
    for name, wanted in needs.items():
        have = getattr(capabilities, name)
        if name == "cancellation":
            ok = CANCELLATION_LEVELS.index(have) >= CANCELLATION_LEVELS.index(wanted)
        elif name == "isolation":
            ok = ISOLATION_LEVELS.index(have) >= ISOLATION_LEVELS.index(wanted)
        elif isinstance(wanted, bool):
            ok = have or not wanted
        else:
            ok = have == wanted
        if not ok and name not in allowed_degradations:
            missing.append(f"{name} (needs {wanted!r}, adapter declares {have!r})")
    if missing:
        raise CapabilityError(
            "adapter cannot guarantee: "
            + "; ".join(missing)
            + ". Use a capable adapter or list the shortfall under `allowed_degradations` in policy."
        )


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """The last JSON object in a fenced block, or the whole text if it is JSON."""
    candidates = list(reversed(_FENCE.findall(text or ""))) + [text or ""]
    for c in candidates:
        try:
            obj = json.loads(c.strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class ScriptedStep:
    """Used by the simulated adapter: what one agent session does."""

    status: str = "COMPLETED"
    output: str = ""
    structured: dict[str, Any] | None = None
    cost: float = 0.0
    edits: dict[str, str | None] = field(default_factory=dict)
