"""A filesystem message bus with the shared bus's layout.

    <root>/topics/<topic>.jsonl    canonical, append-only message history
    <root>/inbox/<agent>.jsonl     delivery copies, one file per subscriber
    <root>/subscribers.json        {agent: {"topics": [...], "filters": {...}, "expires_at": ts|null}}

Posting appends to the topic log first, then fans a copy out to every matching subscriber. A
crash between the two leaves the message in history but missing from some inboxes; `reconcile`
repairs that, and is safe to run any number of times (deliveries are deduplicated by message id).

Topics are a fixed list, as on the shared bus: posting to a topic that is not on it fails rather
than silently creating one. The `daedalus` topic must also be added to the shared bus's own list
before its CLI and watchers accept it (docs/coordination.md).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from daedalus.coordination import buslock
from daedalus.core.errors import DaedalusError

TOPIC = "daedalus"
DEFAULT_TOPICS = (TOPIC,)
PRIORITIES = ("low", "normal", "high", "urgent")



class BusError(DaedalusError):
    pass


def safe_name(name: str) -> str:
    """A filename for an agent name on every OS (`:` is not allowed on Windows). The hash keeps
    two names that differ only in replaced characters apart."""
    tidy = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return f"{tidy}.{hashlib.sha256(name.encode()).hexdigest()[:8]}"


def matches(message: dict[str, Any], sub: dict[str, Any]) -> bool:
    """Whether a subscription wants a message: its topic is subscribed and every filter equals the
    message's value for that key."""
    if message.get("topic") not in sub.get("topics", ()):
        return False
    return all(message.get(k) == v for k, v in (sub.get("filters") or {}).items())


def _num(v: Any, default: float) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def valid_message(obj: Any) -> bool:
    """Whether a log line has the fields, with the types, that everything reading it relies on."""
    if not isinstance(obj, dict):
        return False
    if not all(isinstance(obj.get(k), str) for k in ("id", "sender", "topic", "type", "subject")):
        return False
    ts = obj.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    refs = obj.get("refs", [])
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return False
    return obj.get("reply_to") is None or isinstance(obj["reply_to"], str)


def _read_jsonl(
    path: Path, offset: int = 0, valid: Callable[[Any], bool] = valid_message
) -> tuple[list[dict[str, Any]], int]:
    """Complete lines from byte `offset` that pass `valid`, and the offset after the last complete
    line. A final line with no newline is a write in progress: it is left for the next read."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    out = []
    for line in data[: end + 1].splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a torn or foreign line never stops the rest of the log being read
        if valid(obj):  # a foreign or malformed line is skipped, never fatal
            out.append(obj)
    return out, offset + end + 1


def _append_line(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("ab") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, sort_keys=True, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()  # only still there if the replace did not happen


class FileBus:
    def __init__(
        self,
        root: str | Path,
        *,
        topics: tuple[str, ...] = DEFAULT_TOPICS,
        clock: Callable[[], float] = time.time,
        fanout_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.root = Path(root)
        self.topics = tuple(topics)
        self.clock = clock
        # Called before each inbox write; tests use it to simulate a delivery that fails.
        self._fanout_hook = fanout_hook
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._lock_fh: Any = None
        for d in ("topics", "inbox"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def topic_path(self, topic: str) -> Path:
        self._check_topic(topic)
        return self.root / "topics" / f"{topic}.jsonl"

    def inbox_path(self, agent: str) -> Path:
        return self.root / "inbox" / f"{safe_name(agent)}.jsonl"

    def _check_topic(self, topic: str) -> None:
        if topic not in self.topics:
            raise BusError(f"unknown topic {topic!r}; this bus carries {list(self.topics)}")

    # ------------------------------------------------------------------- lock
    @contextlib.contextmanager
    def locked(self, timeout_s: float = 10.0) -> Iterator[None]:
        """One writer at a time, across processes and threads: an OS lock (flock, or msvcrt on
        Windows) on a lock file that stays in place. A writer that crashes releases it with its
        process, so there is no stale lock to break. Re-entrant within one thread."""
        with self._thread_lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                with buslock.locked(self.root, timeout_s):  # the same lock `bus.py` can take
                    yield
            except buslock.LockTimeout as exc:
                raise BusError(str(exc)) from None
            finally:
                self._depth = 0

    # ----------------------------------------------------------- subscribers
    def subscribers(self) -> dict[str, dict[str, Any]]:
        """The subscriber table. A missing file is an empty table; a malformed one is an error,
        never read as empty (the next write would then erase every subscription)."""
        path = self.root / "subscribers.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise BusError(f"{path} is not valid JSON; fix or remove it ({exc})") from exc
        if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
            raise BusError(f"{path} must map agent names to subscriptions")
        return raw

    def subscribe(
        self,
        agent: str,
        topics: tuple[str, ...],
        filters: dict[str, Any] | None = None,
        *,
        ttl_s: float | None = None,
    ) -> dict[str, Any]:
        for t in topics:
            self._check_topic(t)
        if ttl_s is not None and ttl_s <= 0:
            raise BusError("ttl_s must be positive (or None for no expiry)")
        now = self.clock()
        with self.locked():
            subs = self.subscribers()
            # Resubscribing (a restart) keeps the original start, so reconcile still covers the gap.
            since = (subs.get(agent) or {}).get("subscribed_at", now)
            sub = {
                "topics": list(topics),
                "filters": dict(filters or {}),
                "subscribed_at": since,
                "expires_at": now + ttl_s if ttl_s is not None else None,
            }
            subs[agent] = sub
            write_json_atomic(self.root / "subscribers.json", subs)
        return sub

    def unsubscribe(self, agent: str) -> bool:
        with self.locked():
            subs = self.subscribers()
            gone = subs.pop(agent, None) is not None
            if gone:
                write_json_atomic(self.root / "subscribers.json", subs)
        return gone

    def expire(self, should_drop: Callable[[str, dict[str, Any]], bool] | None = None) -> list[str]:
        """Drop subscriptions past their `expires_at`, or that `should_drop(agent, sub)` rejects."""
        now = self.clock()
        with self.locked():
            subs = self.subscribers()
            drop = [
                a
                for a, s in subs.items()
                if (s.get("expires_at") is not None and _num(s["expires_at"], 0) <= now)
                or (should_drop and should_drop(a, s))
            ]
            for a in drop:
                del subs[a]
            if drop:
                write_json_atomic(self.root / "subscribers.json", subs)
        return drop

    # --------------------------------------------------------------- posting
    def post(
        self,
        *,
        sender: str,
        topic: str,
        type_: str,
        subject: str,
        body: str = "",
        priority: str = "normal",
        status: str = "open",
        refs: tuple[str, ...] = (),
        reply_to: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append to the topic log, then deliver a copy to each matching subscriber. Returns the
        message, with `undelivered` naming any subscriber whose copy failed (reconcile repairs it)."""
        self._check_topic(topic)
        if priority not in PRIORITIES:
            raise BusError(f"priority must be one of {PRIORITIES}")
        msg: dict[str, Any] = {
            **(extra or {}),
            "id": f"msg-{uuid.uuid4()}",
            "ts": self.clock(),
            "sender": sender,
            "topic": topic,
            "type": type_,
            "priority": priority,
            "status": status,
            "subject": subject,
            "body": body,
            "refs": list(refs),
            "reply_to": reply_to,
        }
        with self.locked():
            _append_line(self.topic_path(topic), msg)
            failed = []
            for agent, sub in self.subscribers().items():
                if not self._live(sub) or not matches(msg, sub):
                    continue
                try:
                    self._deliver(agent, msg)
                except OSError:
                    failed.append(agent)
        return {**msg, "undelivered": failed}

    def _live(self, sub: dict[str, Any]) -> bool:
        exp = sub.get("expires_at")
        return exp is None or _num(exp, 0) > self.clock()

    def _deliver(self, agent: str, msg: dict[str, Any]) -> None:
        if self._fanout_hook:
            self._fanout_hook(agent, msg)
        _append_line(self.inbox_path(agent), msg)

    # --------------------------------------------------------------- reading
    def history(self, topic: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Messages in a topic's canonical log from byte `offset`."""
        return _read_jsonl(self.topic_path(topic), offset)

    def inbox(self, agent: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        return _read_jsonl(self.inbox_path(agent), offset)

    def reconcile(self, agents: list[str] | None = None) -> dict[str, int]:
        """Deliver every message a live subscriber should hold (posted since it subscribed) but its
        inbox lacks, in history order. Returns how many copies were added per agent."""
        added: dict[str, int] = {}
        with self.locked():
            subs = self.subscribers()
            for agent in agents if agents is not None else list(subs):
                sub = subs.get(agent)
                if not sub or not self._live(sub):
                    continue
                have = {m["id"] for m in self.inbox(agent)[0]}
                for topic in sub.get("topics", ()):
                    if topic not in self.topics:
                        continue
                    for m in self.history(topic)[0]:
                        if (
                            m["id"] not in have
                            and matches(m, sub)
                            and m["ts"] >= _num(sub.get("subscribed_at"), 0)
                        ):
                            _append_line(self.inbox_path(agent), m)
                            have.add(m["id"])
                            added[agent] = added.get(agent, 0) + 1
        return added
