"""Subprocess adapter for CLI agent harnesses.

One generic adapter covers any harness with a non-interactive CLI mode; the
presets only differ in argv, how the prompt is passed, and how output is
parsed. `claude-code` is the reference preset. `codex` and `aider` are
experimental: their argv follows their documented non-interactive modes but
is not exercised by Daedalus's tests, and they declare that limitation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import replace
from typing import Any

from daedalus.adapters.base import Adapter, AgentRequest, AgentResult, Capabilities, extract_json
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

PRESETS: dict[str, dict[str, Any]] = {
    "claude-code": {
        "argv": ["claude", "-p", "--output-format", "json"],
        "args": ["--permission-mode", "acceptEdits"],
        "read_only_args": ["--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit"],
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
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> CommandAdapter:
        name = cfg.get("name", "claude-code")
        preset = PRESETS.get(name, {})
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

    def available(self) -> bool:
        return shutil.which(self.argv[0]) is not None

    def _command(self, req: AgentRequest) -> tuple[list[str], bytes | None]:
        cmd = self.argv + self.args + (self.read_only_args if req.read_only else [])
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
            try:
                doc = json.loads(text)
            except ValueError:
                return AgentResult(
                    "FAILED",
                    output=text,
                    session_id=session,
                    error=f"unparseable claude output (exit {code}): {err[-500:]}",
                )
            text = str(doc.get("result", ""))
            cost = float(doc.get("total_cost_usd") or 0.0)
            if doc.get("is_error") or code != 0:
                return AgentResult(
                    "FAILED",
                    output=text,
                    cost=cost,
                    session_id=session,
                    error=f"agent reported an error (exit {code})",
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
