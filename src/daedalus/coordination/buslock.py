"""The bus's writer lock, as one standalone file.

Every writer of a bus directory (Daedalus's FileBus, and the shared bus's own `bus.py`) should hold
this lock while it appends to a topic log, fans out to inboxes or rewrites `subscribers.json`.
Then two writers never interleave a line or lose a subscription update.

The file imports only the standard library, so it can be copied next to `bus.py` as is:

    from buslock import locked

    with locked(BUS_ROOT):
        append_to_topic(...)
        fan_out(...)

It is an OS lock (`fcntl.flock`, or `msvcrt.locking` on Windows) on `<bus>/.lock`, a file that stays
in place. A writer that crashes releases the lock with its process, so there is never a stale lock to
break. Not re-entrant: take it once per operation.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

LOCK_FILE = ".lock"

if os.name == "nt":
    import msvcrt

    def _try_lock(fh: Any) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: Any) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fh: Any) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: Any) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class LockTimeout(TimeoutError):
    pass


@contextlib.contextmanager
def locked(root: str | Path, timeout_s: float = 10.0) -> Iterator[None]:
    """Hold the bus's writer lock for the duration of the block."""
    path = Path(root) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            _try_lock(fh)
            break
        except OSError:
            if time.monotonic() > deadline:
                fh.close()
                raise LockTimeout(f"bus at {root} stayed locked for {timeout_s:g}s") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            _unlock(fh)
        finally:
            fh.close()
