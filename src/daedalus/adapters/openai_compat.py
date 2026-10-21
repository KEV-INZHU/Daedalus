"""Review-only adapters that call a model over HTTP: no agent CLI needed.

Two wire formats, both with the standard library only:

- `openai-compatible`: `POST {base_url}/chat/completions` (OpenAI, OpenRouter, Together, Groq,
  vLLM, LM Studio, Ollama).
- `anthropic`: `POST {base_url}/messages` (the Anthropic Messages API).

An HTTP model has no tools, so it cannot read the repository. The review prompt Daedalus builds
already carries the candidate diff; this adapter appends the full current content of the changed
files (source before tests), all within `max_context_chars`, and says so when it cut anything.
The reviewer therefore judges from the diff and those files only. Read-only is true by
construction: the adapter never touches the working tree.

The API key is read from the environment variable named in `api_key_env` at launch time and is
never stored, logged or put in an error message. Builder launches are refused.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from daedalus.adapters.base import Adapter, AgentRequest, AgentResult, Capabilities, extract_json
from daedalus.core.errors import CapabilityError

OPENAI_COMPATIBLE = "openai-compatible"
ANTHROPIC = "anthropic"
HTTP_ADAPTERS = (OPENAI_COMPATIBLE, ANTHROPIC)

DEFAULT_BASE_URLS = {OPENAI_COMPATIBLE: "https://api.openai.com/v1", ANTHROPIC: "https://api.anthropic.com/v1"}
DEFAULT_MAX_CONTEXT_CHARS = 200_000
DEFAULT_MAX_TOKENS = 8192  # the Messages API requires an output cap; chat completions leave it to the server
ANTHROPIC_VERSION = "2023-06-01"
MAX_RETRY_WAIT_S = 10.0  # a longer Retry-After is not worth blocking a review for
_POLL_S = 0.2

_ALLOWED_KEYS = {"name", "base_url", "review_model", "api_key_env", "cost_per_mtok", "max_context_chars",
                 "max_tokens"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL = re.compile(r"^\S+$")
NOTE_RESERVE = 400  # kept free for the diff-cut note, so a cut prompt stays about max_chars
MIN_DIFF_CHARS = 500  # a review with less diff than this is not a review: fail with a clear error instead
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)  # reasoning models (qwen3, deepseek-r1) print this first
_DIFF_BLOCK = re.compile(r"```diff\n(.*?)\n```[ \t]*\s*$", re.DOTALL)
_TEST_NAME = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.|^conftest\.)")

SYSTEM_NOTE = (
    "You are a code reviewer with no tools. You cannot read files, run code or see anything beyond this "
    "message. Judge only from the diff and the file contents given below, and do not claim to have run "
    "checks or tests. Finish with the fenced JSON block the task asks for."
)


def is_local_url(url: str) -> bool:
    """Whether the endpoint is this machine: `localhost` or a loopback/unspecified IP address. A DNS name
    such as `127.evil.example` is not local."""
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in ("test", "tests", "__tests__") for p in parts[:-1]) or bool(_TEST_NAME.search(parts[-1]))


def _read_text(root: Path, rel: str) -> str | None:
    """A changed file's current text, or None when it is gone, binary, outside the repository, or reached
    through a symlink (a builder-made `notes.txt -> .env` must not attach the target to the request)."""
    try:
        parts = Path(rel).parts
        if not parts or ".." in parts or Path(rel).is_absolute():
            return None
        cur = root
        for part in parts:
            cur = cur / part
            if cur.is_symlink():
                return None
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return None
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_review_prompt(prompt: str, root: Path, max_chars: int, paths: Sequence[str] = ()) -> str:
    """The review prompt plus the content of `paths` (the candidate's changed paths, from Daedalus,
    never parsed from the prompt: an agent can write `+++ b/<any file>` into the diff it authored),
    bounded by `max_chars` (about: the notes that say what was cut sit on top). Priority: the diff,
    then changed source, then changed tests."""
    notes: list[str] = []
    m = _DIFF_BLOCK.search(prompt)
    if m and len(prompt) > max_chars:
        diff = m.group(1)
        fixed = len(prompt) - len(diff)  # the instructions, findings and notes around the diff
        room = max_chars - NOTE_RESERVE - fixed
        if room < min(len(diff), MIN_DIFF_CHARS):
            raise _Failure(
                f"max_context_chars ({max_chars}) is too small: the review instructions alone take {fixed} "
                f"characters, leaving {max(room, 0)} for the diff. Raise `max_context_chars` under `adapter:` "
                "(at least "
                f"{fixed + NOTE_RESERVE + MIN_DIFF_CHARS}) or review a smaller change"
            )
        prompt = prompt[: m.start(1)] + diff[:room] + prompt[m.end(1):]
        notes.append(f"the diff was cut to {room} of {len(diff)} characters; judge only what is shown, "
                     "and say so if that stops you from reaching a verdict")
    left = 0 if notes else max_chars - len(prompt)  # a cut diff already used the budget: the diff has priority
    paths = list(dict.fromkeys(paths))
    ordered = [p for p in paths if not _is_test_path(p)] + [p for p in paths if _is_test_path(p)]
    blocks: list[str] = []
    omitted: list[str] = []
    for rel in ordered:
        text = _read_text(root, rel)
        if text is None:
            continue
        head = f"### {rel}\n```\n"
        cost = len(head) + len(text) + 5
        if cost <= left:
            blocks.append(f"{head}{text}\n```")
            left -= cost
        elif left > len(head) + 500:  # a useful prefix still fits
            keep = left - len(head) - 5
            blocks.append(f"{head}{text[:keep]}\n```")
            omitted.append(f"{rel} (first {keep} of {len(text)} characters shown)")
            left = 0
        else:
            omitted.append(f"{rel} (omitted)")
    if omitted:
        shown = omitted[:5] + ([f"{len(omitted) - 5} more"] if len(omitted) > 5 else [])
        notes.append("the context budget (max_context_chars) ran out; full content is incomplete for: "
                     + "; ".join(shown))
    parts = [prompt]
    if blocks:
        parts += ["", "## Full current content of the changed files (for context; the diff above is what "
                      "you judge)", ""] + blocks
    if notes:
        parts += ["", "## Truncation note", ""] + [f"- {n}" for n in notes]
    return "\n".join(parts)


@dataclass(frozen=True)
class _Reply:
    text: str
    input_tokens: int
    output_tokens: int
    truncated: bool  # the model hit its output cap


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the API key to whatever host answered."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Failure(Exception):
    def __init__(self, message: str, status: str = "FAILED"):
        super().__init__(message)
        self.status = status


def _num(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 else 0


def _rate(cfg: dict[str, Any], key: str) -> float:
    v = cfg.get(key, 0)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise CapabilityError(f"adapter cost_per_mtok.{key} must be a number of dollars per million tokens, 0 or more")
    return float(v)


class HttpReviewAdapter(Adapter):
    """Runs read-only reviews against a chat API. See the module docstring."""

    def __init__(self, name: str, cfg: dict[str, Any] | None = None):
        cfg = dict(cfg or {})
        if name not in HTTP_ADAPTERS:
            raise CapabilityError(f"unknown HTTP adapter {name!r}: use one of {list(HTTP_ADAPTERS)}")
        extra = sorted(set(cfg) - _ALLOWED_KEYS)
        if extra:
            raise CapabilityError(f"adapter {name!r}: unknown setting(s) {extra} (allowed: {sorted(_ALLOWED_KEYS - {'name'})}). "
                                  "This adapter reviews only; it has no builder settings")
        self.name = name
        self.base_url = str(cfg.get("base_url") or DEFAULT_BASE_URLS[name]).strip().rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise CapabilityError(f"adapter {name!r}: base_url must start with http:// or https:// (got {self.base_url!r})")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise CapabilityError(f"adapter {name!r}: base_url must not contain credentials (user:password@); "
                                  "put the key in the environment variable named by `api_key_env`")
        if parsed.query or parsed.fragment or parsed.params:
            raise CapabilityError(f"adapter {name!r}: base_url must not carry a query string, fragment or "
                                  "parameters (they could hold a secret and would be logged and cached); "
                                  "use `api_key_env` for the key")
        model = cfg.get("review_model")
        if not isinstance(model, str) or not _MODEL.match(model):
            raise CapabilityError(f"adapter {name!r}: set `review_model` to the model name the API expects "
                                  "(it also keys the evidence cache)")
        self.review_model: str = model
        self.model = None  # builder launches are refused
        env = cfg.get("api_key_env")
        if env is not None and not (isinstance(env, str) and _ENV_NAME.match(env)):
            raise CapabilityError(f"adapter {name!r}: `api_key_env` is the NAME of an environment variable "
                                  "(for example OPENAI_API_KEY), never the key itself")
        self.api_key_env: str | None = env
        costs = cfg.get("cost_per_mtok")
        if costs is not None and not isinstance(costs, dict):
            raise CapabilityError(f"adapter {name!r}: cost_per_mtok must be a mapping like {{input: 0, output: 0}}")
        self.costs = {"input": _rate(costs, "input"), "output": _rate(costs, "output")} if costs else None
        if costs and set(costs) - {"input", "output"}:
            raise CapabilityError(f"adapter {name!r}: cost_per_mtok takes only `input` and `output`")
        self.max_context_chars = self._whole(cfg, "max_context_chars", DEFAULT_MAX_CONTEXT_CHARS, 2000)
        self.max_tokens = self._whole(cfg, "max_tokens", DEFAULT_MAX_TOKENS, 1)
        self.retry_delay_s = 2.0  # used when a 429 names no Retry-After
        self.capabilities = Capabilities(
            launch_agent=True,  # for reviews and arbitrations; builder launches raise in run_agent
            cancellation="cooperative",  # `should_stop` is polled; the request is abandoned, not recalled
            isolation="none",  # nothing is isolated because nothing runs: the model sees only the prompt
            identity="launched",
            structured_results=True,
            cost_reporting=self.costs is not None,
            status_query=False,
            limitations=("review only: cannot build", "judges from the diff and changed files only; "
                         "no tools, cannot read other files or run anything"),
        )

    def _whole(self, cfg: dict[str, Any], key: str, default: int, floor: int) -> int:
        v = cfg.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < floor:
            raise CapabilityError(f"adapter {self.name!r}: {key} must be a whole number of at least {floor}")
        return v

    # ---------------------------------------------------------------- config facts (doctor)
    @property
    def local(self) -> bool:
        return is_local_url(self.base_url)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None if self.api_key_env else None

    @property
    def endpoint(self) -> str:
        return self.base_url + ("/messages" if self.name == ANTHROPIC else "/chat/completions")

    @property
    def cost_untracked(self) -> bool:
        """A hosted endpoint with no prices: it bills, and Daedalus cannot say how much."""
        return not self.local and not self.costs

    def expected_cost_basis(self) -> str | None:
        if self.costs and (self.costs["input"] or self.costs["output"]):
            return "cash"
        # Billed but unpriced: cash with an unknown cost, so the cash budget applies (and fails closed)
        # instead of every review being recorded as a free API-equivalent estimate.
        return "cash" if self.cost_untracked else None

    # ---------------------------------------------------------------- the session
    def run_agent(self, request: AgentRequest) -> AgentResult:
        if not request.read_only:
            raise CapabilityError(
                f"the {self.name} adapter reviews only: it has no tools, so it cannot edit files or run "
                "commands. Use an agent adapter such as `claude-code` for building, or set a different "
                "builder adapter and keep this one for reviews"
            )
        key = self.api_key()
        if self.api_key_env and not key and not self.local:
            raise CapabilityError(f"adapter {self.name!r}: ${self.api_key_env} is not set in the environment")
        session = uuid.uuid4().hex
        try:
            prompt = build_review_prompt(request.prompt, Path(request.cwd), self.max_context_chars,
                                         request.context_paths)
            reply = self._call(prompt, request)
        except _Failure as exc:
            return AgentResult(exc.status, error=str(exc), session_id=session, usage=self._usage(0, 0))
        usage = self._usage(reply.input_tokens, reply.output_tokens)
        cost = self._cost(reply.input_tokens, reply.output_tokens)
        basis = "cash" if cost or self.cost_untracked else "api_equivalent"
        unknown = self.cost_untracked
        text = _THINK.sub("", reply.text).strip()
        structured = extract_json(text)
        if structured is None:
            why = "the model's reply was cut off at its output limit" if reply.truncated else \
                "the model returned no valid JSON result block" if text else "the model returned an empty reply"
            return AgentResult("FAILED", output=text, error=why, cost=cost, session_id=session, cost_basis=basis,
                               usage=usage, cost_unknown=unknown)
        return AgentResult("COMPLETED", output=text, structured=structured, cost=cost, session_id=session,
                           cost_basis=basis, usage=usage, cost_unknown=unknown)

    def _usage(self, tokens_in: int, tokens_out: int) -> dict[str, Any]:
        return {"model": self.review_model, "input_tokens": tokens_in, "output_tokens": tokens_out}

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        if not self.costs:
            return 0.0
        return (tokens_in * self.costs["input"] + tokens_out * self.costs["output"]) / 1_000_000

    def _body(self, prompt: str) -> tuple[dict[str, str], dict[str, Any]]:
        key = self.api_key()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "daedalus-review"}
        if self.name == ANTHROPIC:
            if key:
                headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
            return headers, {"model": self.review_model, "max_tokens": self.max_tokens, "temperature": 0,
                             "system": SYSTEM_NOTE, "messages": [{"role": "user", "content": prompt}]}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers, {"model": self.review_model, "temperature": 0, "stream": False,
                         "messages": [{"role": "system", "content": SYSTEM_NOTE},
                                      {"role": "user", "content": prompt}]}

    def _call(self, prompt: str, request: AgentRequest) -> _Reply:
        """One request, with one bounded retry on 429, honouring the deadline and `should_stop`."""
        deadline = time.monotonic() + request.timeout_s
        headers, payload = self._body(prompt)
        data = json.dumps(payload).encode("utf-8")
        outcome = self._send(data, headers, deadline, request.should_stop)
        if isinstance(outcome, float):  # a 429: wait as asked (bounded), then try exactly once more
            self._sleep(min(outcome, MAX_RETRY_WAIT_S), deadline, request.should_stop)
            outcome = self._send(data, headers, deadline, request.should_stop)
            if isinstance(outcome, float):
                raise _Failure("the API is rate limiting requests (HTTP 429 twice); try again later")
        return outcome

    def _sleep(self, seconds: float, deadline: float, should_stop: Any) -> None:
        end = min(time.monotonic() + seconds, deadline)
        while (now := time.monotonic()) < end:
            self._check(deadline, should_stop)
            time.sleep(min(_POLL_S, end - now))
        self._check(deadline, should_stop)

    @staticmethod
    def _check(deadline: float, should_stop: Any) -> None:
        if should_stop is not None and should_stop():
            raise _Failure("the run no longer wants this review", "CANCELLED")
        if time.monotonic() >= deadline:
            raise _Failure("the review request timed out", "TIMED_OUT")

    def _send(self, data: bytes, headers: dict[str, str], deadline: float, should_stop: Any) -> _Reply | float:
        """_Reply on success; the seconds to wait on a 429; _Failure otherwise. The request runs on a
        worker thread so cancellation and the deadline are honoured even while the server is silent.
        A cancelled or timed-out request abandons its daemon thread; the thread is bounded, because the
        socket timeout passed to `_post` is the time left until the deadline, so it ends then at the latest."""
        box: list[Any] = []
        remaining = max(0.1, deadline - time.monotonic())

        def work() -> None:
            try:
                box.append(self._post(data, headers, remaining))
            except BaseException as exc:  # noqa: BLE001 — handed to the caller's thread
                box.append(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while thread.is_alive():
            self._check(deadline, should_stop)
            thread.join(_POLL_S)
        got = box[0]
        if isinstance(got, _Failure):
            raise got
        if isinstance(got, BaseException):
            raise _Failure(f"{type(got).__name__}: {got}") from got
        return got

    def _post(self, data: bytes, headers: dict[str, str], timeout: float) -> _Reply | float:
        req = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)  # honours HTTPS_PROXY and the system CA bundle
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            with exc:  # always release the response, whichever branch answers
                if exc.code == 429:
                    try:
                        return float(exc.headers.get("Retry-After", ""))
                    except ValueError:
                        return self.retry_delay_s
                snippet = exc.read(400).decode("utf-8", "replace").strip() if exc.fp else ""
                hint = " (check the API key)" if exc.code in (401, 403) else ""
                raise _Failure(f"HTTP {exc.code} from {self.endpoint}{hint}: {snippet or exc.reason}") from exc
        except TimeoutError as exc:
            raise _Failure("the review request timed out", "TIMED_OUT") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise _Failure("the review request timed out", "TIMED_OUT") from exc
            raise _Failure(f"cannot reach {self.endpoint}: {exc.reason}") from exc
        return self._parse(raw)

    def _parse(self, raw: bytes) -> _Reply:
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise _Failure("the API did not answer with JSON") from exc
        if not isinstance(body, dict):
            raise _Failure("the API did not answer with a JSON object")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        if self.name == ANTHROPIC:
            blocks = body.get("content") if isinstance(body.get("content"), list) else []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            return _Reply(text if isinstance(text, str) else "", _num(usage.get("input_tokens")),
                          _num(usage.get("output_tokens")), body.get("stop_reason") == "max_tokens")
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        text = message.get("content")
        return _Reply(text if isinstance(text, str) else "", _num(usage.get("prompt_tokens")),
                      _num(usage.get("completion_tokens")), choice.get("finish_reason") == "length")

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> HttpReviewAdapter:
        return cls(str(cfg.get("name")), cfg)
