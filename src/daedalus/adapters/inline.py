"""Inline adapter: the harness *is* the executor.

Used when an agent already running in a harness (Claude Code, Codex, ...)
works under the gate via a skill or hook. Daedalus cannot launch, cancel or
identify that agent, and says so; anything that needs those capabilities
(launched reviews, fan-out) requires a launching adapter instead.
"""

from __future__ import annotations

from daedalus.adapters.base import Adapter, AgentRequest, AgentResult, Capabilities
from daedalus.core.errors import CapabilityError


class InlineAdapter(Adapter):
    name = "inline"
    capabilities = Capabilities(
        launch_agent=False,
        cancellation="none",
        isolation="none",
        identity="self_reported",
        structured_results=False,
        cost_reporting=False,
        status_query=False,
        limitations=("the harness agent runs outside Daedalus's control",),
    )

    def run_agent(self, request: AgentRequest) -> AgentResult:
        raise CapabilityError(
            "the inline adapter cannot launch agents; configure `adapter:` in .daedalus.yml"
        )
