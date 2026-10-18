"""`daedalus ... | head`: a reader that closes early must not produce a traceback."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap

import pytest

from daedalus import cli

# Drives cli.main with a stand-in `status` so the test does not depend on a repository.
DRIVER = textwrap.dedent(
    """
    import sys
    from daedalus import cli

    def chatty(args):
        for i in range(100_000):
            print(f"line {i}")
        return 0

    def broken(args):
        raise ValueError("real failure")

    cli.cmd_status = {"chatty": chatty, "broken": broken}[sys.argv[1]]
    sys.exit(cli.main(["status"]))
    """
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe semantics")


def _run_into_closed_pipe(*cmd: str) -> subprocess.CompletedProcess:
    """Run with stdout attached to a pipe whose reader has already gone away."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        return subprocess.run(cmd, stdout=write_end, stderr=subprocess.PIPE, text=True, timeout=60)
    finally:
        os.close(write_end)


@posix_only
def test_command_output_into_closed_pipe_exits_quietly():
    proc = _run_into_closed_pipe(sys.executable, "-c", DRIVER, "chatty")
    assert proc.stderr == ""
    assert proc.returncode == 128 + cli.SIGPIPE


@posix_only
def test_help_into_closed_pipe_exits_quietly():
    proc = _run_into_closed_pipe(sys.executable, "-m", "daedalus.cli", "--help")
    assert proc.stderr == ""
    assert proc.returncode == 128 + cli.SIGPIPE


def test_other_exceptions_still_raise_with_traceback():
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, "broken"], capture_output=True, text=True, timeout=60
    )
    assert "Traceback" in proc.stderr
    assert "ValueError: real failure" in proc.stderr
    assert proc.returncode == 1


def test_broken_pipe_in_process_returns_sigpipe_status(monkeypatch):
    def broken_pipe(args):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "cmd_status", broken_pipe)
    # A stdout with no file descriptor keeps the devnull redirect off the test runner's fd 1.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli.main(["status"]) == 128 + cli.SIGPIPE


@pytest.mark.parametrize("exc", [ValueError("boom"), OSError("disk"), ConnectionResetError("peer")])
def test_other_exceptions_propagate_in_process(monkeypatch, exc):
    def failing(args):
        raise exc

    monkeypatch.setattr(cli, "cmd_status", failing)
    with pytest.raises(type(exc)):
        cli.main(["status"])
