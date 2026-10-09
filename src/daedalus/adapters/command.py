"""Subprocess adapter for CLI agent harnesses.

One generic adapter covers any harness with a non-interactive CLI mode; the
presets only differ in argv, how the prompt is passed, and how output is
parsed. `claude-code` is the reference preset. `codex` and `aider` are
experimental: their argv follows their documented non-interactive modes but
is not exercised by Daedalus's tests, and they declare that limitation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import replace
from typing import Any

from daedalus.adapters.base import (
    AGENT_MARKER,
    Adapter,
    AgentRequest,
    AgentResult,
    Capabilities,
    extract_json,
)
from daedalus.core.errors import CapabilityError

_BASE_CAPS = Capabilities(
    launch_agent=True,
    cancellation="hard",  # we own the child process while it runs
    isolation="none",
    identity="launched",
    structured_results=True,  # via a fenced JSON block the role prompt asks for
    cost_reporting=False,
    status_query=False,  # a session cannot be re-attached after a restart
    limitations=("sessions are not resumable after an orchestrator restart",),
)

# Reviewers get exactly these tools: an allowlist, so a new editing or shell tool
# in a future harness release is excluded by default rather than by a denylist.
CLAUDE_REVIEW_TOOLS = "Read,Grep,Glob"
# A locked preset's launch line comes from the preset alone. Config may set only
# these keys, and `args` may contain only these flags (an allowlist: anything that
# could add tools, MCP servers, plugins, settings or permissions is refused).
_LOCKED_CONFIG_KEYS = frozenset({"name", "model", "review_model", "args"})
# flag -> validator for its single value (None: the flag takes no value). Only
# fixed-arity flags belong here: a list-valued flag could swallow later arguments.
_LOCKED_ARG_FLAGS: dict[str, Any] = {
    "--verbose": None,
    "--max-turns": re.compile(r"^[1-9][0-9]{0,3}$"),
    "--fallback-model": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]*$"),
}
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]*$")

PRESETS: dict[str, dict[str, Any]] = {
    "claude-code": {
        "argv": ["claude", "-p", "--output-format", "json"],
        # Lean sessions: no MCP servers, no skills or slash commands. The harness's
        # own setup otherwise loads ~37k input tokens into every launch.
        "args": ["--permission-mode", "acceptEdits", "--strict-mcp-config", "--disable-slash-commands"],
        "read_only_args": ["--tools", CLAUDE_REVIEW_TOOLS],
        "locked_read_only": True,
        "prompt_via": "stdin",
        "parse": "claude-json",
        "capabilities": replace(_BASE_CAPS, cost_reporting=True),
    },
    "codex": {
        "argv": ["codex", "exec"],
        "args": ["--full-auto"],
        "read_only_args": ["--sandbox", "read-only"],
        "prompt_via": "arg",
        "parse": "text",
        "capabilities": replace(_BASE_CAPS, limitations=_BASE_CAPS.limitations + ("experimental preset",)),
    },
    "aider": {
        "argv": ["aider", "--yes-always", "--no-auto-commits"],
        "args": [],
        "read_only_args": ["--dry-run"],
        "prompt_via": "message",
        "parse": "text",
        "capabilities": replace(_BASE_CAPS, limitations=_BASE_CAPS.limitations + ("experimental preset",)),
    },
}


HARNESS_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def agent_env() -> dict[str, str]:
    """Environment for a launched agent: a fresh harness session, marked as an agent.

    Launched agents must not inherit the parent harness session's markers (the
    child is its own session), and DAEDALUS_AGENT makes Daedalus's human-only
    commands refuse inside it.
    """
    env = {k: v for k, v in os.environ.items() if k not in HARNESS_SESSION_VARS}
    env[AGENT_MARKER] = "1"
    return env


def claude_result(text: str) -> dict[str, Any] | None:
    """The final result message from `claude -p --output-format json`.

    Older releases print one result object; newer ones print the array of
    session messages, whose last `type: result` entry is the outcome.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if isinstance(doc, list):
        results = [m for m in doc if isinstance(m, dict) and m.get("type") == "result"]
        return results[-1] if results else None
    return doc if isinstance(doc, dict) and doc.get("type") == "result" else None


def _model(value: Any, adapter: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _MODEL_NAME.match(value):
        raise CapabilityError(f"adapter {adapter!r}: invalid model name {value!r}")
    return value


class CommandAdapter(Adapter):
    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        args: list[str] | None = None,
        read_only_args: list[str] | None = None,
        prompt_via: str = "stdin",
        parse: str = "text",
        capabilities: Capabilities = _BASE_CAPS,
        model: str | None = None,
        review_model: str | None = None,
        builder_args: list[str] | None = None,
    ):
        if prompt_via not in ("stdin", "arg", "message"):
            raise CapabilityError(f"prompt_via must be stdin, arg or message (got {prompt_via!r})")
        self.name = name
        self.argv = list(argv)
        self.args = list(args or [])
        self.read_only_args = list(read_only_args or [])
        self.prompt_via = prompt_via
        self.parse = parse
        self.capabilities = capabilities
        self.model = model
        self.review_model = review_model or model
        self.builder_args = list(builder_args or [])
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> CommandAdapter:
        name = cfg.get("name", "claude-code")
        preset = PRESETS.get(name, {})
        if preset.get("locked_read_only"):
            return cls._locked(name, preset, cfg)
        if preset and "read_only_args" in cfg:
            raise CapabilityError(f"adapter {name!r}: the preset's read-only profile cannot be overridden")
        unsupported = [k for k in ("model", "review_model") if k in cfg]
        if unsupported:
            raise CapabilityError(f"adapter {name!r}: {unsupported} are supported only by the claude-code preset")
        argv = cfg.get("argv") or preset.get("argv")
        if not argv:
            raise CapabilityError(
                f"unknown adapter {name!r}: use one of {sorted(PRESETS)} or give `argv` in .daedalus.yml"
            )
        return cls(
            name,
            argv,
            args=cfg.get("args", preset.get("args")),
            read_only_args=cfg.get("read_only_args", preset.get("read_only_args")),
            prompt_via=cfg.get("prompt_via", preset.get("prompt_via", "stdin")),
            parse=cfg.get("parse", preset.get("parse", "text")),
            capabilities=preset.get("capabilities", _BASE_CAPS),
        )

    @classmethod
    def _locked(cls, name: str, preset: dict[str, Any], cfg: dict[str, Any]) -> CommandAdapter:
        """Presets whose launch line config cannot change (fail closed)."""
        extra = sorted(set(cfg) - _LOCKED_CONFIG_KEYS)
        if extra:
            raise CapabilityError(
                f"adapter {name!r} has a fixed launch profile; {extra} cannot be set "
                f"(allowed: {sorted(_LOCKED_CONFIG_KEYS)})"
            )
        args = cfg.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise CapabilityError(f"adapter {name!r}: `args` must be a list of strings")
        i = 0
        while i < len(args):
            flag, eq, _ = args[i].partition("=")
            if flag not in _LOCKED_ARG_FLAGS:
                raise CapabilityError(
                    f"adapter {name!r}: `{args[i]}` is not an allowed extra flag "
                    f"(allowed: {sorted(_LOCKED_ARG_FLAGS)})"
                )
            check = _LOCKED_ARG_FLAGS[flag]
            if check is not None:
                if eq:
                    value = args[i].partition("=")[2]
                else:
                    i += 1
                    value = args[i] if i < len(args) else ""
                if not check.match(value):
                    raise CapabilityError(f"adapter {name!r}: invalid value {value!r} for `{flag}`")
            elif eq:
                raise CapabilityError(f"adapter {name!r}: `{flag}` takes no value")
            i += 1
        return cls(
            name,
            preset["argv"],
            args=list(preset.get("args") or ()),
            builder_args=args,
            read_only_args=preset.get("read_only_args"),
            prompt_via=preset.get("prompt_via", "stdin"),
            parse=preset.get("parse", "text"),
            capabilities=preset.get("capabilities", _BASE_CAPS),
            model=_model(cfg.get("model"), name),
            review_model=_model(cfg.get("review_model"), name),
        )

    def available(self) -> bool:
        return shutil.which(self.argv[0]) is not None

    def _command(self, req: AgentRequest) -> tuple[list[str], bytes | None]:
        # Read-only arguments come last so nothing configurable can follow (and override) them.
        model = self.review_model if req.read_only else self.model
        cmd = self.argv + self.args + ([f"--model={model}"] if model else [])
        cmd += self.read_only_args if req.read_only else self.builder_args
        if self.prompt_via == "stdin":
            return cmd, req.prompt.encode("utf-8")
        if self.prompt_via == "message":
            return cmd + ["--message", req.prompt], None
        return cmd + [req.prompt], None

    def run_agent(self, request: AgentRequest) -> AgentResult:
        exe = shutil.which(self.argv[0])
        if exe is None:
            raise CapabilityError(f"adapter {self.name!r}: `{self.argv[0]}` is not on PATH")
        cmd, stdin = self._command(request)
        cmd[0] = exe
        session = uuid.uuid4().hex
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(request.cwd),
                env=agent_env(),
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return AgentResult("FAILED", session_id=session, error=f"could not start agent: {exc}")
        self._procs[session] = proc
        try:
            out, err = proc.communicate(stdin, timeout=request.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return AgentResult(
                "TIMED_OUT", session_id=session, error=f"agent exceeded {request.timeout_s:g}s"
            )
        finally:
            self._procs.pop(session, None)
        if proc.returncode < 0:
            return AgentResult("CANCELLED", session_id=session, error="agent process was terminated")
        text = out.decode("utf-8", "replace")
        return self._parse(text, err.decode("utf-8", "replace"), proc.returncode, session)

    def _parse(self, text: str, err: str, code: int, session: str) -> AgentResult:
        cost = 0.0
        if self.parse == "claude-json":
            doc = claude_result(text)
            if doc is None:
                return AgentResult(
                    "FAILED",
                    output=text,
                    session_id=session,
                    error=f"unparseable claude output (exit {code}): {err[-500:]}",
                )
            text = str(doc.get("result", ""))
            try:
                cost = float(doc.get("total_cost_usd") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0  # an unreadable cost must not turn a session report into a crash
            subtype = str(doc.get("subtype", "success"))
            if doc.get("is_error") or subtype != "success" or code != 0:
                return AgentResult(
                    "FAILED",
                    output=text,
                    cost=cost,
                    session_id=session,
                    error=f"agent reported an error (subtype {subtype}, exit {code})"
                    + (f": {err[-500:]}" if code != 0 and err.strip() else ""),
                )
        elif code != 0:
            return AgentResult("FAILED", output=text, session_id=session, error=f"exit {code}: {err[-500:]}")
        return AgentResult(
            "COMPLETED", output=text, structured=extract_json(text), cost=cost, session_id=session
        )

    def cancel(self, session_id: str) -> bool:
        proc = self._procs.get(session_id)
        if proc is None:
            return False
        proc.kill()
        return True
