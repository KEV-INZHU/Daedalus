"""Plain-words names for the roles, for everything a person reads.

Persisted ids, reason codes, event payloads and command arguments keep the short ids
(`aristotle`, `plato`, ...): the audit log and the CLI refer to them. Messages say what the role
does, with the id after it, so either one finds the other.
"""

from __future__ import annotations

DISPLAY_NAMES = {
    "aristotle": "quality review (Aristotle)",
    "socrates": "validation review (Socrates)",
    "james": "functionality review (James)",
    "mozi": "simplicity review (Mozi)",
    "plato": "arbiter (Plato)",
    "brunel": "builder (Brunel)",
    "vitruvius": "planner (Vitruvius)",
}


def display_name(role: str) -> str:
    """How a person reads a role id; an id with no entry (a custom review) is shown as it is."""
    return DISPLAY_NAMES.get(role, role)


def review_name(perspective: str) -> str:
    """A review perspective as a noun phrase: `quality review (Aristotle)`, or `mine review`."""
    name = display_name(perspective)
    return name if " review" in name else f"{name} review"


def with_article(phrase: str) -> str:
    return ("an " if phrase[:1].lower() in "aeiou" else "a ") + phrase
