"""Plugin registry: harness integrations and adapters, built in or from entry points.

A *harness integration* installs the ambient gate into a harness (hooks, plugins, rules
files). Third parties register one under the entry-point group `daedalus.harnesses`.
An *adapter* launches agents (builders and reviewers); the group is `daedalus.adapters`.

Built-in harnesses are named by "module:attribute" and imported on first use, so startup
stays fast and a built-in module that fails to import is listed as unavailable, never a crash.
See docs/extending.md.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HARNESS_GROUP = "daedalus.harnesses"
ADAPTER_GROUP = "daedalus.adapters"

# name -> "module:attribute". Each attribute is a HarnessIntegration.
BUILTIN_HARNESSES: dict[str, str] = {
    "claude-code": "daedalus.harness.claude_code:INTEGRATION",
    "codex": "daedalus.harness.codex:INTEGRATION",
    "opencode": "daedalus.harness.opencode:INTEGRATION",
    "pi": "daedalus.harness.pi:INTEGRATION",
    "hermes": "daedalus.harness.hermes:INTEGRATION",
}


@dataclass(frozen=True)
class HarnessIntegration:
    """How one harness gets the ambient gate."""

    name: str
    description: str
    install: Callable[[Path], list[str]]  # human-readable lines describing what was done
    detect: Callable[[Path], bool]  # does this harness look in use in the repo or on PATH?
    adapter_preset: str | None = None  # the `adapter:` name that launches this harness, if any
    uninstall: Callable[[Path], list[str]] | None = None


@dataclass
class PluginInfo:
    """One registry entry as `daedalus plugins` shows it."""

    name: str
    kind: str  # "harness" | "adapter"
    source: str  # "built-in" | "third-party"
    available: bool
    detail: str = ""  # what it is, or why it is unavailable
    integration: HarnessIntegration | None = field(default=None, repr=False)
    read_only: str = ""  # adapters: how review launches are made read-only


class PluginError(Exception):
    """A plugin was asked for that does not exist or cannot be loaded."""


_ep_cache: dict[str, dict[str, importlib.metadata.EntryPoint]] = {}


def reset_cache() -> None:
    """Forget discovered entry points (for tests and long-lived processes)."""
    _ep_cache.clear()


def _entry_points(group: str) -> dict[str, importlib.metadata.EntryPoint]:
    if group not in _ep_cache:
        found: dict[str, importlib.metadata.EntryPoint] = {}
        for ep in importlib.metadata.entry_points(group=group):
            found.setdefault(ep.name, ep)  # first one wins
        _ep_cache[group] = found
    return _ep_cache[group]


def _as_integration(obj: Any, name: str) -> HarnessIntegration:
    if callable(obj) and not isinstance(obj, HarnessIntegration):
        obj = obj()  # a zero-argument factory is fine too
    if not isinstance(obj, HarnessIntegration):
        raise PluginError(f"harness {name!r}: expected a HarnessIntegration, got {type(obj).__name__}")
    return obj


def _load_harness(name: str) -> tuple[str, HarnessIntegration | None, str]:
    """(source, integration, error) for one harness name."""
    builtin = name in BUILTIN_HARNESSES
    source = "built-in" if builtin else "third-party"
    module = ""
    try:
        if builtin:
            module, _, attr = BUILTIN_HARNESSES[name].partition(":")
            return source, _as_integration(getattr(importlib.import_module(module), attr), name), ""
        return source, _as_integration(_entry_points(HARNESS_GROUP)[name].load(), name), ""
    except Exception as exc:  # noqa: BLE001 — a broken plugin must not take the CLI down
        if isinstance(exc, ModuleNotFoundError) and builtin and _is_own_module(exc, module):
            return source, None, "module not installed"
        return source, None, f"{type(exc).__name__}: {exc}"


def _is_own_module(exc: ModuleNotFoundError, module: str) -> bool:
    """Is the missing module the harness module itself (or a package above it), not one it imports?"""
    return bool(exc.name) and (module == exc.name or module.startswith(exc.name + "."))


def harness_names() -> list[str]:
    """Every registered harness name: built-ins first, then third-party (built-ins cannot be shadowed)."""
    extra = sorted(n for n in _entry_points(HARNESS_GROUP) if n not in BUILTIN_HARNESSES)
    return list(BUILTIN_HARNESSES) + extra


def get_harness(name: str) -> HarnessIntegration:
    if name not in harness_names():
        raise PluginError(f"unknown harness {name!r}: use one of {harness_names()}")
    _, integration, error = _load_harness(name)
    if integration is None:
        raise PluginError(f"harness {name!r} is unavailable: {error}")
    return integration


def list_harnesses() -> list[PluginInfo]:
    out = []
    for name in harness_names():
        source, integration, error = _load_harness(name)
        out.append(PluginInfo(
            name, "harness", source, integration is not None,
            integration.description if integration else error, integration,
        ))
    return out


def _detects(integration: HarnessIntegration, root: Path) -> bool:
    try:
        return bool(integration.detect(root))
    except Exception:  # noqa: BLE001 — a plugin's broken `detect` means "not detected"
        return False


def detected_harnesses(root: Path) -> list[HarnessIntegration]:
    """Available harnesses that look in use in `root` (or on PATH). A failing `detect` counts as no."""
    found = []
    for info in list_harnesses():
        if info.integration is not None and _detects(info.integration, root):
            found.append(info.integration)
    return found


# -------------------------------------------------------------------- adapters
def adapter_entry_point(name: str) -> Any | None:
    """What a third-party adapter entry point resolves to: a factory or a preset dict."""
    ep = _entry_points(ADAPTER_GROUP).get(name)
    if ep is None:
        return None
    try:
        obj = ep.load()
    except Exception as exc:
        raise PluginError(f"adapter {name!r} could not be loaded: {type(exc).__name__}: {exc}") from exc
    if not (callable(obj) or isinstance(obj, dict)):
        raise PluginError(f"adapter {name!r}: expected a factory (cfg) -> Adapter or a preset dict")
    return obj


def read_only_mode(preset: dict[str, Any]) -> str:
    """How a preset's review launches are kept read-only."""
    if preset.get("locked_read_only"):
        return "locked"
    if preset.get("read_only_args"):
        return "read_only_args"
    return "none"


def preset_available(preset: dict[str, Any]) -> tuple[bool, str]:
    """(is its executable on PATH, the executable that was probed). `available_probe` names the real
    CLI when `argv[0]` is only a launcher (the interpreter running a wrapper module)."""
    exe = str(preset.get("available_probe") or preset["argv"][0])
    return shutil.which(exe) is not None, exe


def list_adapters() -> list[PluginInfo]:
    from daedalus.adapters.command import PRESETS
    from daedalus.adapters.openai_compat import HTTP_ADAPTERS

    out = [PluginInfo("inline", "adapter", "built-in", True, "the harness agent is the executor", read_only="n/a")]
    for name in HTTP_ADAPTERS:  # always available: stdlib HTTP, nothing to install
        out.append(PluginInfo(name, "adapter", "built-in", True,
                              "reviews over HTTP, no agent CLI; judges from the diff only", read_only="no tools"))
    for name, preset in PRESETS.items():
        ok, exe = preset_available(preset)
        out.append(PluginInfo(name, "adapter", "built-in", ok, "" if ok else f"`{exe}` not on PATH",
                              read_only=read_only_mode(preset)))
    for name in sorted(_entry_points(ADAPTER_GROUP)):
        if name in PRESETS or name in HTTP_ADAPTERS or name == "inline":
            continue  # built-ins cannot be shadowed
        try:
            obj = adapter_entry_point(name)
        except PluginError as exc:
            out.append(PluginInfo(name, "adapter", "third-party", False, str(exc)))
            continue
        if isinstance(obj, dict):
            out.append(PluginInfo(name, "adapter", "third-party", True, "preset", read_only=read_only_mode(obj)))
        else:
            out.append(PluginInfo(name, "adapter", "third-party", True, "factory", read_only="unknown (factory)"))
    return out
