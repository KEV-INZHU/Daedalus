"""Daedalus gate for Hermes Agent: a thin shim over `daedalus hook <name>`.

Hermes event -> Daedalus hook (Claude-shaped payloads in, Claude-shaped JSON out):
  pre_llm_call (every turn)   -> `prompt`; on a session's first turn also `session-start`,
                                 whose additionalContext is injected as {"context": ...}
  pre_tool_call (write_file, patch) -> `pre-edit` (never blocks the edit)
  pre_verify (agent edited code, about to finish) -> `stop`; {"decision": "block", "reason"}
                                 becomes {"action": "continue", "message": reason}

Stdlib only; imports nothing from Hermes except (guarded) its session helpers. Never fails closed:
any error means "no directive", and an error is never reported as acceptance. A no-op when
`daedalus` is not on PATH, and inside sessions Daedalus launched itself (`--source daedalus`
or DAEDALUS_AGENT).
"""

# daedalus-gate: installed by `daedalus init`
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import threading

# Messages the harness itself injects as user turns; the prompt hook must not see them as the user's.
# Keep in sync with daedalus.harness.claude_code.HARNESS_PROMPTS (duplicated: this shim is stdlib-only).
HARNESS_PROMPTS = ("<task-notification>", "<system-reminder>", "<agent-message", "Stop hook feedback:")
FEEDBACK_PREFIX = "Stop hook feedback:\n"  # marks the continue message as harness-injected for the prompt hook
EDIT_TOOLS = ("write_file", "patch")
SOURCE_TAG = "daedalus"  # `hermes chat --source daedalus`, set by Daedalus's own launches
FAST_TIMEOUT_S = 20  # pre_tool_call fails CLOSED on Hermes's 30s callback timeout: stay well under it
STOP_TIMEOUT_S = 3600
DEFAULT_WAIT_S = 25  # Hermes abandons a pre_verify callback after plugins.hook_callback_timeout (30s)

_pending: dict[str, str] = {}  # session -> gate feedback that arrived after Hermes stopped waiting
_started: set[str] = set()
_lock = threading.Lock()
_ctx = None


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:  # noqa: BLE001
        return os.environ.get(name, "")


def launched_by_daedalus() -> bool:
    return bool(os.environ.get("DAEDALUS_AGENT")) or _session_value("HERMES_SESSION_SOURCE") == SOURCE_TAG


def _cwd() -> str:
    with contextlib.suppress(Exception):
        if _ctx is not None and (cwd := _ctx.session_cwd()):
            return cwd
    return os.getcwd()


def _run(sub: str, payload: dict, timeout: float) -> dict:
    """`daedalus hook <sub>`; {} when Daedalus is absent, slow, or answers nothing usable."""
    exe = shutil.which("daedalus")
    if exe is None or launched_by_daedalus():
        return {}
    try:
        proc = subprocess.run(
            [exe, "hook", sub], input=json.dumps(payload), capture_output=True, text=True,
            timeout=timeout, cwd=payload.get("cwd") or None, check=False,
        )
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return out if isinstance(out, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _say(text: str) -> None:
    """Show the user a gate message; never raises."""
    with contextlib.suppress(Exception):
        if text and _ctx is not None:
            _ctx.post_message(text)


def pre_llm_call(session_id: str = "", user_message: str = "", is_first_turn: bool = False, **_kw):
    base = {"cwd": _cwd(), "session_id": session_id}
    parts: list[str] = []
    with _lock:
        late = _pending.pop(session_id, "")
        first = is_first_turn or session_id not in _started
        _started.add(session_id)
    if late:
        parts.append(late)
    if first:
        ctx = _run("session-start", base, FAST_TIMEOUT_S).get("hookSpecificOutput", {}).get("additionalContext")
        if isinstance(ctx, str) and ctx:
            parts.append(ctx)
    if not str(user_message or "").lstrip().startswith(HARNESS_PROMPTS):
        _run("prompt", {**base, "prompt": user_message}, FAST_TIMEOUT_S)
    return {"context": "\n\n".join(parts)} if parts else None


def pre_tool_call(tool_name: str = "", args=None, session_id: str = "", **_kw):
    if tool_name not in EDIT_TOOLS:
        return
    args = args if isinstance(args, dict) else {}
    payload = {"cwd": _cwd(), "session_id": session_id, "tool_name": tool_name,
               "tool_input": {"file_path": args.get("path") or ""}}
    _say(_run("pre-edit", payload, FAST_TIMEOUT_S).get("systemMessage", ""))
    # never blocks an edit


def _wait_budget() -> float:
    """How long Hermes will wait for this callback: plugins.hook_callback_timeout (0 = forever)."""
    try:
        from hermes_cli.config import load_config_readonly

        t = float((load_config_readonly().get("plugins") or {}).get("hook_callback_timeout", 30))
        return STOP_TIMEOUT_S if t <= 0 else max(1.0, t - 5)
    except Exception:  # noqa: BLE001
        return DEFAULT_WAIT_S


def _feedback(out: dict) -> str:
    return str(out.get("reason") if out.get("decision") == "block" else out.get("systemMessage") or "")


def pre_verify(session_id: str = "", **_kw):
    """The Stop equivalent. The gate can take minutes but Hermes abandons a pre_verify callback after
    plugins.hook_callback_timeout, so wait only that long; a verdict that arrives later is delivered
    with the next turn (and shown to the user) rather than lost."""
    payload = {"cwd": _cwd(), "session_id": session_id}
    box: dict = {}

    def work() -> None:
        out = _run("stop", payload, STOP_TIMEOUT_S)
        with _lock:
            box["out"] = out
            late = box.get("abandoned")
        if late and (text := _feedback(out)):
            with _lock:
                _pending[session_id] = text
            _say(text)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(_wait_budget())
    with _lock:
        if "out" not in box:
            box["abandoned"] = True
            return None
        out = box["out"]
    if out.get("decision") == "block" and out.get("reason"):
        return {"action": "continue", "message": FEEDBACK_PREFIX + str(out["reason"])}
    _say(_feedback(out))
    return None


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("pre_verify", pre_verify)
