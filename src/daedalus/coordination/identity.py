"""Generated agent names: `agent:<role>:<uuid4>`.

A name is for attribution, not proof of identity: the bus does not authenticate senders. What a
name stands for is whatever Ariadne registered it against (run, task, role, principal), and the
review rules key off that principal, so minting a new name changes nothing about who you are.
"""

from __future__ import annotations

import re
import uuid

from daedalus.core.contracts import PERSPECTIVES

# Roles that write first-pass reviews. A registered agent with any other role cannot submit one,
# and the channel withholds other agents' findings from these roles until they have reviewed.
REVIEWER_ROLES = frozenset(PERSPECTIVES) | {"reviewer"}

_NAME = re.compile(
    r"^agent:(?P<role>[a-z][a-z0-9_-]*):(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)


def new_agent_name(role: str) -> str:
    """A fresh name for a subagent of `role`, e.g. agent:brunel:4c2655b3-36fc-4741-b0d5-356a4bfeeb67."""
    role = role.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", role):
        raise ValueError(f"role {role!r} must be lowercase letters, digits, _ or -")
    return f"agent:{role}:{uuid.uuid4()}"


def parse_agent_name(name: str) -> tuple[str, str] | None:
    """(role, uuid) of a generated name, or None for anything else."""
    m = _NAME.match(name)
    return (m["role"], m["id"]) if m else None
