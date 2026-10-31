"""The bus's writer lock as a standalone file the shared `bus.py` can copy."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from daedalus.coordination import buslock
from daedalus.coordination.bus import BusError, FileBus

SOURCE = Path(buslock.__file__)


def test_it_needs_nothing_but_the_standard_library():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names) | {"__future__"}


def test_a_copy_and_filebus_exclude_each_other(tmp_path):
    copy = tmp_path / "elsewhere" / "buslock.py"  # as `bus.py` would use it, outside the package
    copy.parent.mkdir()
    copy.write_bytes(SOURCE.read_bytes())
    bus = FileBus(tmp_path / "bus")
    script = (
        f"import sys; sys.path.insert(0, {str(copy.parent)!r}); from buslock import locked, LockTimeout\n"
        f"try:\n    with locked({str(bus.root)!r}, timeout_s=0.2): print('got it')\n"
        "except LockTimeout: print('timed out')"
    )
    with bus.locked():
        held = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert held.stdout.strip() == "timed out"
    free = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert free.stdout.strip() == "got it"


def test_filebus_reports_a_held_lock_as_a_bus_error(tmp_path):
    bus = FileBus(tmp_path / "bus")
    with buslock.locked(bus.root):
        with pytest.raises(BusError):
            with bus.locked(timeout_s=0.1):
                pass
