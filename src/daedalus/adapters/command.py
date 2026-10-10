"""Subprocess adapter for CLI agent harnesses.

One generic adapter covers any harness with a non-interactive CLI mode; the
presets only differ in argv, how the prompt is passed, and how output is
parsed. `claude-code` is the reference preset. `codex` and `aider` are
experimental: their argv follows their documented non-interactive modes but
is not exercised by Daedalus's tests, and they declare that limitation.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
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

# Preset launch lines: `argv` + `args` for every launch, then `builder_args` for a
# builder launch or `read_only_args` for a review launch. Nothing that enables
# writing belongs in `argv` or `args`.
PRESETS: dict[str, dict[str, Any]] = {
    "claude-code": {
        # stream-json (which needs --verbose) is the only format that carries the
        # subscription's rate-limit reading; the parser also accepts plain json.
        "argv": ["claude", "-p", "--output-format", "stream-json", "--verbose"],
        # Lean sessions: no MCP servers, no skills or slash commands. The harness's
        # own setup otherwise loads ~37k input tokens into every launch.
        "args": ["--strict-mcp-config", "--disable-slash-commands"],
        "builder_args": ["--permission-mode", "acceptEdits"],
        # --restricted: no user, project or local settings files (so no settings-granted
        # permissions or hooks) and file tools confined to the working directory. Without
        # it a reviewer reads anywhere the user's settings allow and loads `.claude/CLAUDE.md`
        # (excluded from the candidate) and the project's auto-memory, both of which a
        # Builder session can write; with it neither loads (observed on claude 2.1.296).
        "read_only_args": ["--restricted", "--tools", CLAUDE_REVIEW_TOOLS],
        "locked_read_only": True,
        "prompt_via": "stdin",
        "parse": "claude-json",
        "capabilities": replace(_BASE_CAPS, cost_reporting=True),
    },
    "codex": {
        "argv": ["codex", "exec"],
        "args": [],
        "builder_args": ["--full-auto"],  # write-enabling: never part of a review launch
        "read_only_args": ["--sandbox", "read-only"],
        "prompt_via": "arg",
        "parse": "text",
        "capabilities": replace(_BASE_CAPS, limitations=_BASE_CAPS.limitations + ("experimental preset",)),
    },
    "aider": {
        "argv": ["aider", "--yes-always", "--no-auto-commits"],
        "args": [],
        "builder_args": [],
        "read_only_args": ["--dry-run"],
        "prompt_via": "message",
        "parse": "text",
        "capabilities": replace(_BASE_CAPS, limitations=_BASE_CAPS.limitations + ("experimental preset",)),
    },
}


HARNESS_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
STOP_POLL_S = 1.0  # how often a running session's `should_stop` is consulted


class _SessionTree:
    """A launched session and every process it starts.

    On Windows the session runs in a kill-on-close job object: killing it ends the
    whole tree (the agent's shells and test runs too), and if the orchestrator dies
    the OS closes the job handle and ends the tree, so no session outlives the
    process that supervises it. Processes the session starts in the instant before it
    joins the job escape it. Elsewhere only the session process itself is killed.
    """

    def __init__(self, proc: subprocess.Popen[bytes]):
        self.proc = proc
        self._job = _kill_on_close_job(proc) if os.name == "nt" else None

    def kill(self) -> None:
        if self._job is not None:
            _kernel32().TerminateJobObject(self._job, 1)
        try:
            self.proc.kill()
        except OSError:
            pass  # already gone

    def close(self) -> None:
        """Release the job; anything the session left running ends with it."""
        if self._job is not None:
            _kernel32().CloseHandle(self._job)
            self._job = None


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


def _kill_on_close_job(proc: subprocess.Popen[bytes]) -> Any:
    """A job object holding `proc` that kills its processes when the last handle closes.
    None when the job cannot be set up (the session then runs, as before, without it)."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    extended_limit_information = 9
    if not (
        k32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info))
        and k32.AssignProcessToJobObject(job, int(proc._handle))  # type: ignore[attr-defined]
    ):
        k32.CloseHandle(job)
        return None
    return job


def agent_env() -> dict[str, str]:
    """Environment for a launched agent: a fresh harness session, marked as an agent.

    Launched agents must not inherit the parent harness session's markers (the
    child is its own session), and DAEDALUS_AGENT makes Daedalus's human-only
    commands refuse inside it.
    """
    env = {k: v for k, v in os.environ.items() if k not in HARNESS_SESSION_VARS}
    env[AGENT_MARKER] = "1"
    return env


def claude_messages(text: str) -> list[dict[str, Any]]:
    """Every message a `claude -p` session printed, whatever the output format:
    one result object (`json`, older releases), an array (`json`, 2.1.x) or one
    message per line (`stream-json`). Unparseable lines are skipped."""
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
        out = []
        for line in text.split("\n"):  # not splitlines(): U+2028 and friends can sit inside JSON strings
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict):
                out.append(m)
        return out
    if isinstance(doc, list):
        return [m for m in doc if isinstance(m, dict)]
    return [doc] if isinstance(doc, dict) else []


def claude_result(text: str) -> dict[str, Any] | None:
    """The session's final `type: result` message, or None."""
    results = [m for m in claude_messages(text) if m.get("type") == "result"]
    return results[-1] if results else None


def claude_quota(messages: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    """The last subscription rate-limit reading (`rate_limit_event`, stream-json only).

    The event is undocumented, so this never raises: anything missing, malformed or
    non-finite (in any window) yields None, which budget policy must treat as
    "unknown", never as headroom.
    """
    events = [m for m in messages if m.get("type") == "rate_limit_event"]
    info = events[-1].get("rate_limit_info") if events else None
    if not isinstance(info, dict):
        return None

    def window(w: Any, util: str, reset: str) -> dict[str, float] | None:
        try:
            u, r = float(w[util]), float(w[reset])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return {"utilization": u, "resets_at": r} if math.isfinite(u) and math.isfinite(r) else None

    raw = info.get("unifiedWindows")
    windows: dict[str, dict[str, float]] = {}
    if raw is not None:
        if not isinstance(raw, dict) or not raw:
            return None
        for name, w in raw.items():
            parsed = window(w, "utilization", "resetsAt")
            if parsed is None:
                return None  # a partial reading could hide the binding window
            windows[str(name)] = parsed
    else:
        parsed = window(info, "utilization", "resetsAt")
        if parsed is None or not isinstance(info.get("rateLimitType"), str):
            return None
        windows[info["rateLimitType"]] = parsed
    return {"source": "claude-code rate_limit_event", "status": str(info.get("status", "")), "windows": windows,
            "observed_at": now}


def claude_usage(result: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Models and token counts from a result message (plus the init message's model).
    Never raises: malformed fields count as zero or are left out."""
    def num(d: Any, key: str) -> int:
        try:
            v = float(d.get(key) or 0) if isinstance(d, dict) else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0
        return int(v) if math.isfinite(v) and v >= 0 else 0

    u = result.get("usage")
    model_usage = result.get("modelUsage")
    models = {}
    for name, m in (model_usage.items() if isinstance(model_usage, dict) else ()):
        if isinstance(m, dict):
            models[str(name)] = {k: num(m, k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                        "cacheCreationInputTokens")}
    init = claude_init(messages)
    model = init.get("model")
    return {
        "model": model if isinstance(model, str) else (next(iter(models)) if models else None),
        "models": models,
        "input_tokens": num(u, "input_tokens"),
        "output_tokens": num(u, "output_tokens"),
        "cache_read_input_tokens": num(u, "cache_read_input_tokens"),
        "cache_creation_input_tokens": num(u, "cache_creation_input_tokens"),
        "num_turns": num(result, "num_turns"),
        "duration_ms": num(result, "duration_ms"),
    }


def claude_init(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next((m for m in messages if m.get("type") == "system" and m.get("subtype") == "init"), {})


# Environment variables under which a claude session is billed rather than drawn from a subscription:
# API keys and bearer tokens, a gateway base URL, or a cloud provider.
BILLING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def claude_cost_basis(messages: list[dict[str, Any]], env: dict[str, str]) -> str:
    """`api_equivalent` only when nothing suggests billing: the launch environment
    carries no billed credential, gateway or cloud provider, and the session's init
    message says `apiKeySource: "none"` (a subscription login). An init message without
    that field, or with any other value, is cash. Plain `json` output has no init
    message; then the environment alone decides. Billed money is never mistaken for an
    estimate."""
    if any(env.get(v) for v in BILLING_ENV_VARS):
        return "cash"
    init = claude_init(messages)
    if not init:
        return "api_equivalent"  # plain `json` output has no init message; the environment showed no billing
    return "api_equivalent" if init.get("apiKeySource") == "none" else "cash"


_PRESET_CONFIG_KEYS = frozenset({"name", "args"})


def _str_list(cfg: dict[str, Any], key: str, adapter: str) -> list[str]:
    """A list of strings, or [] when the key is absent. Anything else, falsy or not, is refused."""
    if key not in cfg:
        return []
    value = cfg[key]
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise CapabilityError(f"adapter {adapter!r}: `{key}` must be a list of strings")
    return value


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
        self._procs: dict[str, _SessionTree] = {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> CommandAdapter:
        name = cfg.get("name", "claude-code")
        preset = PRESETS.get(name, {})
        if preset.get("locked_read_only"):
            return cls._locked(name, preset, cfg)
        if preset:
            # Built-in presets keep their launch line; config args reach builder launches only.
            extra = sorted(set(cfg) - _PRESET_CONFIG_KEYS)
            if extra:
                raise CapabilityError(
                    f"adapter {name!r} is a built-in preset; {extra} cannot be set "
                    f"(allowed: {sorted(_PRESET_CONFIG_KEYS)}; use your own adapter name with `argv` "
                    "for a different command line)"
                )
            return cls._from_preset(name, preset, _str_list(cfg, "args", name))
        unsupported = [k for k in ("model", "review_model") if k in cfg]
        if unsupported:
            raise CapabilityError(f"adapter {name!r}: {unsupported} are supported only by the claude-code preset")
        argv = _str_list(cfg, "argv", name)
        if not argv:
            raise CapabilityError(
                f"unknown adapter {name!r}: use one of {sorted(PRESETS)} or give `argv` in .daedalus.yml"
            )
        return cls(
            name,
            argv,
            args=_str_list(cfg, "args", name),
            read_only_args=_str_list(cfg, "read_only_args", name),
            prompt_via=cfg.get("prompt_via", "stdin"),
            parse=cfg.get("parse", "text"),
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
        args = _str_list(cfg, "args", name)
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
        return cls._from_preset(
            name, preset, args, _model(cfg.get("model"), name), _model(cfg.get("review_model"), name)
        )

    @classmethod
    def _from_preset(
        cls,
        name: str,
        preset: dict[str, Any],
        config_args: list[str],
        model: str | None = None,
        review_model: str | None = None,
    ) -> CommandAdapter:
        return cls(
            name,
            preset["argv"],
            args=list(preset["args"]),
            builder_args=list(preset["builder_args"]) + config_args,
            read_only_args=preset["read_only_args"],
            prompt_via=preset["prompt_via"],
            parse=preset["parse"],
            capabilities=preset["capabilities"],
            model=model,
            review_model=review_model,
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
        tree = _SessionTree(proc)
        self._procs[session] = tree
        try:
            out, err, stopped = _wait(proc, stdin, request)
            if stopped is not None:
                tree.kill()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    pass  # a grandchild still holds the pipes; the session process itself is dead
                basis = claude_cost_basis([], agent_env()) if self.parse == "claude-json" else "api_equivalent"
                return AgentResult(stopped[0], session_id=session, error=stopped[1], cost_basis=basis)
        except BaseException:
            tree.kill()  # an orchestrator error or Ctrl-C never leaves the session running
            raise
        finally:
            self._procs.pop(session, None)
            tree.close()
        if proc.returncode < 0:
            return AgentResult("CANCELLED", session_id=session, error="agent process was terminated")
        text = out.decode("utf-8", "replace")
        return self._parse(text, err.decode("utf-8", "replace"), proc.returncode, session)

    def _parse(self, text: str, err: str, code: int, session: str) -> AgentResult:
        cost = 0.0
        usage = quota = None
        if self.parse == "claude-json":
            messages = claude_messages(text)
            results = [m for m in messages if m.get("type") == "result"]
            quota = claude_quota(messages, time.time())
            basis = claude_cost_basis(messages, agent_env())
            if not results:
                return AgentResult(
                    "FAILED",
                    output=text[-4000:],
                    session_id=session,
                    error=f"unparseable claude output (exit {code}): {err[-500:]}",
                    quota=quota,
                )
            doc = results[-1]
            text = str(doc.get("result", ""))
            usage = claude_usage(doc, messages)
            try:
                cost = float(doc.get("total_cost_usd") or 0.0)
            except (TypeError, ValueError, OverflowError):
                cost = 0.0  # an unreadable cost must not turn a session report into a crash
            if not (math.isfinite(cost) and cost >= 0):
                cost = 0.0  # Ariadne refuses such costs too; never let one disable a budget cap
            subtype = str(doc.get("subtype", "success"))
            if doc.get("is_error") or subtype != "success" or code != 0:
                return AgentResult(
                    "FAILED",
                    output=text,
                    cost=cost,
                    session_id=session,
                    error=f"agent reported an error (subtype {subtype}, exit {code})"
                    + (f": {err[-500:]}" if code != 0 and err.strip() else ""),
                    usage=usage,
                    quota=quota,
                    cost_basis=basis,
                )
        elif code != 0:
            return AgentResult("FAILED", output=text, session_id=session, error=f"exit {code}: {err[-500:]}")
        return AgentResult(
            "COMPLETED", output=text, structured=extract_json(text), cost=cost, session_id=session,
            usage=usage, quota=quota, cost_basis=basis if self.parse == "claude-json" else "api_equivalent",
        )

    def cancel(self, session_id: str) -> bool:
        tree = self._procs.get(session_id)
        if tree is None:
            return False
        tree.kill()
        return True


def _wait(
    proc: subprocess.Popen[bytes], stdin: bytes | None, request: AgentRequest
) -> tuple[bytes, bytes, tuple[str, str] | None]:
    """Wait for the session, consulting `request.should_stop` every STOP_POLL_S.

    Returns (stdout, stderr, None) when it exits by itself, or ("", "", (status, why))
    when it must be stopped: TIMED_OUT past its deadline, CANCELLED once the run no
    longer wants it. A `should_stop` that raises counts as "keep going"; the deadline
    still bounds the session.
    """
    deadline = time.monotonic() + request.timeout_s
    send = stdin
    while True:
        left = max(0.0, deadline - time.monotonic())
        try:
            out, err = proc.communicate(send, timeout=min(left, STOP_POLL_S) if request.should_stop else left)
            return out, err, None
        except subprocess.TimeoutExpired:
            send = None  # input is written once; a retry only keeps collecting output
        if time.monotonic() >= deadline:
            return b"", b"", ("TIMED_OUT", f"agent exceeded {request.timeout_s:g}s")
        try:
            stop = bool(request.should_stop and request.should_stop())
        except Exception:  # noqa: BLE001
            stop = False
        if stop:
            return b"", b"", ("CANCELLED", "the run was cancelled or finished while the session ran")
