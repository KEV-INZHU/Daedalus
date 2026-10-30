"""Coordination between a run's agents over a shared, local filesystem message bus.

The bus carries coordination messages; it decides nothing. Ariadne's audit log stays the
authority on lifecycle and acceptance: agent identities are registered there, and a message on
the bus never changes run state. See docs/coordination.md.
"""

from daedalus.coordination.bus import DEFAULT_TOPICS, TOPIC, FileBus
from daedalus.coordination.channel import Bootstrap, Channel
from daedalus.coordination.identity import new_agent_name, parse_agent_name

__all__ = [
    "DEFAULT_TOPICS",
    "TOPIC",
    "Bootstrap",
    "Channel",
    "FileBus",
    "new_agent_name",
    "parse_agent_name",
]
