"""Filesystem helpers shared by the demo, snapshots and worktrees."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any


def remove_tree(path: Path, _retried: set[str] | None = None) -> None:
    """Delete a directory tree that may hold read-only files (git writes its objects read-only).
    Windows refuses to delete a read-only file, and POSIX refuses to delete anything inside a
    read-only directory, so make both the entry and its directory writable and retry. Best
    effort: it never raises, and never changes anything outside the tree."""
    top = os.path.abspath(path)
    retried = set() if _retried is None else _retried

    def unlock(target: str) -> None:
        # Never touch anything outside the tree (such as the temp directory that holds it), and
        # never follow a symlink: removing a link only needs its directory to be writable.
        if target != top and not target.startswith(top + os.sep):
            return
        if os.path.islink(target):
            return
        is_dir = os.path.isdir(target)
        try:
            os.chmod(target, stat.S_IRWXU if is_dir else stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass

    def retry(func: Any, target: str, *_: Any) -> None:
        target = os.path.abspath(target)
        unlock(os.path.dirname(target))
        unlock(target)
        try:
            if func in (os.open, os.scandir, os.listdir) and os.path.isdir(target):
                # A directory that could not be listed: its contents were never visited. Retry it
                # once; a second failure means the unlock did not help.
                if target not in retried and os.access(target, os.R_OK | os.X_OK):
                    retried.add(target)
                    remove_tree(Path(target), retried)
            else:
                func(target)
        except Exception:
            pass  # best effort: whatever is left is not worth an error

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except Exception:
        pass
