"""Coordination for the agents `daedalus run` launches (orchestration/lifecycle.py).

On by default (`coordination:` in .daedalus.yml). Each launched builder or worker gets a generated
name, registered before it works, and its launch request carries the run's context, its unread
messages and the environment `daedalus bus` reads. Each launched reviewer gets a registered name
too, so the authorship rules follow its principal, but no peer messages in its prompt: a builder
does not get to talk to the reviewer judging its work, and a review's cache key keeps meaning the
same prompt. A reviewer's findings are posted on the bus once its review is recorded.

The bus never decides acceptance, so a coordination failure never fails a run: it is reported and
that launch goes ahead without coordination.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.adapters.base import AgentRequest
from daedalus.coordination.bus import FileBus
from daedalus.coordination.channel import Channel
from daedalus.coordination.surface import ENV_BUS_DIR, attach
from daedalus.core.config import STATE_DIR
from daedalus.core.errors import DaedalusError

if TYPE_CHECKING:
    from daedalus.orchestration.ariadne import Ariadne

ENV_REPO = "DAEDALUS_REPO"  # the repository whose run the session belongs to (workers run in worktrees)
BUS_COMMAND = "daedalus bus"
_FAILURES = (DaedalusError, OSError, ValueError)


def bus_root(ari: Ariadne) -> Path:
    """$DAEDALUS_BUS_DIR, else `coordination: bus_dir`, else .daedalus/bus; always absolute (a
    relative path is taken from the repository), since sessions run in other directories.

    A bus inside the repository but outside .daedalus/ is refused: its files would be part of every
    candidate, so each message would change the tree between checks and reviews."""
    configured = os.environ.get(ENV_BUS_DIR) or ari.config().bus_dir
    root = ari.root.resolve()
    where = (root / configured).resolve() if configured else root / STATE_DIR / "bus"
    state = root / STATE_DIR
    if (where == root or root in where.parents) and where != state and state not in where.parents:
        raise DaedalusError(
            f"bus directory {where} is inside the repository: use one under {STATE_DIR}/ or outside the repository"
        )
    return where


class Coordinator:
    """Coordination for one run's launches. Its settings are read once, when it is made (once per
    `drive`): editing .daedalus.yml mid-run changes nothing until the next run. A failure turns
    coordination off for the launch it happened in, never for later ones, and every registered name
    is retired through the same channel whatever happened to its launch."""

    def __init__(self, ari: Ariadne, run_id: str, notify: Callable[[str], None]):
        self.notify = notify
        self.channel: Channel | None = None
        try:
            if ari.config().coordination:
                self.channel = Channel(ari, run_id, FileBus(bus_root(ari)))
        except _FAILURES as exc:
            notify(f"coordination is off for this run: {exc}")

    @property
    def on(self) -> bool:
        return self.channel is not None

    def _skip(self, what: str, exc: BaseException) -> None:
        self.notify(f"coordination is off for this launch ({what}): {exc}")

    def name(self, role: str, task_id: str, principal: str) -> str | None:
        """A registered name for an agent about to work, or None without coordination."""
        if self.channel is None:
            return None
        try:
            return self.channel.spawn(role, task_id, principal)
        except _FAILURES as exc:
            self._skip(task_id, exc)
            return None

    def prepare(self, request: AgentRequest, name: str | None) -> AgentRequest:
        """The builder's request with its coordination context, environment and `daedalus bus` allowed.
        If that fails the request goes out unchanged; the name stays registered and is still retired."""
        if self.channel is None or name is None:
            return request
        try:
            out, _ = attach(request, self.channel, name)
        except _FAILURES as exc:
            self._skip(request.task_id, exc)
            return request
        return replace(
            out, env={**out.env, ENV_REPO: str(self.channel.ari.root)}, bus_commands=(BUS_COMMAND,)
        )

    def retire(self, name: str | None) -> None:
        if self.channel is None or name is None:
            return
        try:
            if self.channel.state().agents.get(name, {}).get("retired_at") is None:
                self.channel.retire(name)
            else:
                self.channel.bus.unsubscribe(name)
        except _FAILURES as exc:
            self.notify(f"could not retire {name}: {exc}")

    def post_findings(self, name: str | None, findings: Iterable[dict[str, Any]]) -> None:
        """Share a recorded review's findings. Other reviewers are not shown them until they have
        reviewed the same candidate (channel sealing)."""
        if self.channel is None or name is None:
            return
        try:
            for f in findings:
                self.channel.post(
                    name,
                    "finding",
                    f"{str(f.get('severity', '')).upper()}: {f.get('title', '')}",
                    str(f.get("required_change") or f.get("evidence") or ""),
                )
        except _FAILURES as exc:
            self.notify(f"could not post {name}'s findings: {exc}")
