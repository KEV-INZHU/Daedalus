"""Append-only, hash-chained event store (spec §2.1, §13).

Run state is never stored directly: it is replayed from events, so every core
decision is reproducible from the persisted log and the pinned policy. Each
event's hash covers its content and the previous event's hash; any edit,
deletion or reordering breaks the chain and makes the store refuse to load.

The chain detects corruption and naive tampering. It is not a defence against
an actor who can rewrite the whole database file (see docs/security-model.md).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.core.errors import AuditIntegrityError
from daedalus.core.policy import canonical_json, sha256_hex

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY,
    run_id    TEXT NOT NULL,
    ts        REAL NOT NULL,
    type      TEXT NOT NULL,
    actor     TEXT NOT NULL,
    payload   TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
"""


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    ts: float
    type: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "ts": self.ts,
            "type": self.type,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


def _event_hash(
    prev_hash: str, seq: int, run_id: str, ts: float, type_: str, actor: str, payload: str
) -> str:
    body = canonical_json(
        {"seq": seq, "run_id": run_id, "ts": ts, "type": type_, "actor": actor, "payload": payload}
    )
    return sha256_hex(prev_hash + body)


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ write
    def append(self, run_id: str, type_: str, actor: str, payload: dict[str, Any], ts: float) -> Event:
        body = canonical_json(payload)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
            h = _event_hash(prev, seq, run_id, ts, type_, actor, body)
            self._db.execute(
                "INSERT INTO events(seq, run_id, ts, type, actor, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                (seq, run_id, ts, type_, actor, body, prev, h),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return Event(seq, run_id, ts, type_, actor, payload, prev, h)

    # ------------------------------------------------------------------- read
    _SELECT = "SELECT seq, run_id, ts, type, actor, payload, prev_hash, hash FROM events"

    @staticmethod
    def _event(r: tuple[Any, ...]) -> Event:
        try:
            payload = json.loads(r[5])
        except ValueError as exc:
            raise AuditIntegrityError(f"audit event {r[0]} has a malformed payload") from exc
        return Event(r[0], r[1], r[2], r[3], r[4], payload, r[6], r[7])

    def events(self, run_id: str | None = None) -> list[Event]:
        if run_id is None:
            rows = self._db.execute(self._SELECT + " ORDER BY seq").fetchall()
        else:
            rows = self._db.execute(self._SELECT + " WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [self._event(r) for r in rows]

    def verify_chain(self) -> int:
        """Recompute the whole chain. Returns the number of events verified."""
        prev = GENESIS
        count = 0
        for seq, run_id, ts, type_, actor, payload, prev_hash, h in self._db.execute(
            "SELECT seq, run_id, ts, type, actor, payload, prev_hash, hash FROM events ORDER BY seq"
        ):
            count += 1
            if seq != count:
                raise AuditIntegrityError(f"audit log gap or reordering at seq {seq}")
            if prev_hash != prev or _event_hash(prev, seq, run_id, ts, type_, actor, payload) != h:
                raise AuditIntegrityError(f"audit log hash chain broken at seq {seq}")
            prev = h
        return count

    def run_ids(self) -> list[str]:
        rows = self._db.execute("SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(seq)").fetchall()
        return [r[0] for r in rows]

    def open_run_ids(self) -> list[str]:
        """Runs with no recorded final disposition, newest first."""
        rows = self._db.execute(
            "SELECT run_id FROM events GROUP BY run_id "
            "HAVING SUM(type = 'disposition.recorded') = 0 ORDER BY MIN(seq) DESC"
        ).fetchall()
        return [r[0] for r in rows]

    def has_event(self, run_id: str, types: tuple[str, ...]) -> bool:
        """Whether the run has an event of any of these types. Safe from any thread or
        process: it reads through its own short-lived, read-only connection."""
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            marks = ",".join("?" * len(types))
            row = db.execute(
                f"SELECT 1 FROM events WHERE run_id = ? AND type IN ({marks}) LIMIT 1", (run_id, *types)
            ).fetchone()
            return row is not None
        finally:
            db.close()

    def resolve_run_id(self, prefix: str) -> str:
        matches = [r for r in self.run_ids() if r.startswith(prefix)]
        if len(matches) != 1:
            from daedalus.core.errors import DaedalusError

            raise DaedalusError(
                f"no run matches {prefix!r}" if not matches else f"run prefix {prefix!r} is ambiguous"
            )
        return matches[0]
