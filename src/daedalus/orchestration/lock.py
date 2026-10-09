"""Single-orchestrator lock with stale-holder detection.

Only one process may run checks, dispatch workers or execute restricted
actions for a repository at a time. A lock whose holder process is gone is
stale: the next holder takes it over and must run recovery before relying on
anything the dead holder intended (spec §13).
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from daedalus.core.errors import DaedalusError

if TYPE_CHECKING:  # pragma: no cover
    from typing_extensions import Self


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OrchestratorLock:
    def __init__(self, path: Path):
        self.path = path
        self.took_over_stale = False
        self._held = False
        self._depth = 0

    def holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def holder_alive(self) -> bool:
        h = self.holder()
        if h is None:
            return False
        if h.get("host") != socket.gethostname():
            return True  # cannot see another host's processes: assume alive
        return pid_alive(int(h.get("pid", -1)))

    def acquire(self) -> None:
        if self._held:
            self._depth += 1
            return
        body = json.dumps({"pid": os.getpid(), "host": socket.gethostname()})
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self.holder_alive():
                    h = self.holder() or {}
                    raise DaedalusError(
                        f"another Daedalus process (pid {h.get('pid')}) holds the orchestrator lock"
                    ) from None
                self.path.unlink(missing_ok=True)
                self.took_over_stale = True
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            self._held = True
            self._depth = 1
            return
        raise DaedalusError("could not acquire the orchestrator lock")

    def release(self) -> None:
        if not self._held:
            return
        self._depth -= 1
        if self._depth == 0:
            self._held = False
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()
