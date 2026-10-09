"""Execution adapters: translate Daedalus requests into a harness's operations."""

from __future__ import annotations

from typing import Any

from daedalus.adapters.base import Adapter


def make_adapter(cfg: dict[str, Any] | None) -> Adapter:
    """Build the adapter named in `.daedalus.yml` (`adapter:`); inline when unset."""
    from daedalus.adapters.command import CommandAdapter
    from daedalus.adapters.inline import InlineAdapter

    if not cfg or cfg.get("name") in (None, "inline"):
        return InlineAdapter()
    return CommandAdapter.from_config(cfg)
