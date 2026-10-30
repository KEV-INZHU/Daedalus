"""Put coordination in front of an agent session, through whichever adapter launches it.

A watcher noticing an inbox change cannot make a running model read it. What Daedalus controls is
the session it launches, so `attach` joins the agent to the channel and puts the run's context and
every unread message into the prompt itself (marking them seen, since the session now has them),
and sets the environment its `daedalus bus` commands read, so it can post, read and acknowledge
later messages from inside the session.
"""

from __future__ import annotations

from dataclasses import replace

from daedalus.adapters.base import AgentRequest
from daedalus.coordination.channel import Bootstrap, Channel

ENV_BUS_DIR = "DAEDALUS_BUS_DIR"
ENV_RUN = "DAEDALUS_RUN_ID"
ENV_AGENT = "DAEDALUS_AGENT_NAME"


def attach(request: AgentRequest, channel: Channel, name: str) -> tuple[AgentRequest, Bootstrap]:
    """The request with `name`'s coordination context in its prompt and environment."""
    if channel.run_id != request.run_id:
        raise ValueError(f"channel is for run {channel.run_id}, request is for {request.run_id}")
    boot = channel.join(name)
    env = {**request.env, ENV_BUS_DIR: str(channel.bus.root), ENV_RUN: channel.run_id, ENV_AGENT: name}
    return replace(request, prompt=f"{request.prompt}\n\n{boot.to_text()}", env=env), boot
