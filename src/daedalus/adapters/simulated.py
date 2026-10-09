"""Simulated adapter: scripted agent sessions for tests and Phase 0 drills.

Each role has a queue of `ScriptedStep`s (or callables). A step may edit files
in the request's working directory, which is how a simulated Builder produces
a candidate. Sessions can be left "in flight" to exercise cancellation and
crash recovery.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from daedalus.adapters.base import Adapter, AgentRequest, AgentResult, Capabilities, ScriptedStep

Step = ScriptedStep | Callable[[AgentRequest], AgentResult]


class SimulatedAdapter(Adapter):
    name = "simulated"

    def __init__(self, script: dict[str, list[Step]] | None = None, capabilities: Capabilities | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.capabilities = capabilities or Capabilities(
            launch_agent=True,
            cancellation="cooperative",
            isolation="worktree",
            identity="launched",
            structured_results=True,
            cost_reporting=True,
            status_query=True,
        )
        self.requests: list[AgentRequest] = []
        self.cancelled: set[str] = set()
        self.outcomes: dict[str, AgentResult] = {}

    def run_agent(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        queue = self.script.get(request.role) or self.script.get("*") or []
        if not queue:
            return AgentResult("FAILED", error=f"no scripted step for role {request.role!r}")
        step = queue.pop(0)
        session = uuid.uuid4().hex
        if callable(step):
            result = step(request)
            result = AgentResult(**{**result.__dict__, "session_id": result.session_id or session})
        else:
            for rel, content in step.edits.items():
                _write(request.cwd, rel, content)
            result = AgentResult(step.status, step.output, step.structured, step.cost, session)
        self.outcomes[result.session_id or session] = result
        return result

    def cancel(self, session_id: str) -> bool:
        self.cancelled.add(session_id)
        return True

    def status(self, session_id: str) -> AgentResult | None:
        return self.outcomes.get(session_id)


def _write(root: Path, rel: str, content: str | None) -> None:
    p = Path(root) / rel
    if content is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
