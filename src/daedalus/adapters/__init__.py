"""Execution adapters: translate Daedalus requests into a harness's operations."""

from __future__ import annotations

from typing import Any

from daedalus.adapters.base import Adapter


def make_adapter(cfg: dict[str, Any] | None) -> Adapter:
    """Build the adapter named in `.daedalus.yml` (`adapter:`); inline when unset."""
    from daedalus.adapters.command import PRESETS, CommandAdapter
    from daedalus.adapters.inline import InlineAdapter
    from daedalus.adapters.openai_compat import HTTP_ADAPTERS, HttpReviewAdapter

    if cfg and cfg.get("name") in HTTP_ADAPTERS:
        return HttpReviewAdapter.from_config(cfg)
    if not cfg or cfg.get("name") in (None, "inline"):
        return InlineAdapter()
    name = cfg["name"]
    if name not in PRESETS:
        from daedalus import plugins

        obj = plugins.adapter_entry_point(name)
        if isinstance(obj, dict):
            return CommandAdapter.from_config(cfg, preset=obj)
        if obj is not None:
            return obj(cfg)  # a factory builds the adapter itself
    return CommandAdapter.from_config(cfg)
