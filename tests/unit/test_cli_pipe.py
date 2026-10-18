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

    def refused(args):
        print("some buffered output")
        raise cli.DaedalusError("refused")

    def interrupted(args):
        print("some buffered output")
        raise KeyboardInterrupt

    def other_pipe(args):
        read_end, write_end = os.pipe()
        os.close(read_end)
        os.write(write_end, b"x")

    import os
    cli.cmd_status = {
        "chatty": chatty, "broken": broken, "refused": refused,
        "interrupted": interrupted, "other_pipe": other_pipe,
    }[sys.argv[1]]
    sys.exit(cli.main(["status"]))
    """
)

# Drives cli.main with a parser whose `--help` is far larger than any stdio buffer, so the
# write fails inside _Parser._print_message rather than at the final flush.
BIG_HELP_DRIVER = textwrap.dedent(
    """
    import sys
    from daedalus import cli

    cli.build_parser = lambda: cli._Parser(prog="big", description="word " * 200_000)
    sys.exit(cli.main(["--help"]))
    """
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe semantics")


def _run_into_closed_pipe(*cmd: str) -> subprocess.CompletedProcess:
    """Run with stdout attached to a pipe whose reader has already gone away."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    # Keep stdout block-buffered so output really is pending when the command exits.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    try:
        return subprocess.run(
            cmd, stdout=write_end, stderr=subprocess.PIPE, text=True, timeout=60, env=env
        )
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


@posix_only
def test_large_help_into_closed_pipe_exits_quietly():
    proc = _run_into_closed_pipe(sys.executable, "-c", BIG_HELP_DRIVER)
    assert proc.stderr == ""
    assert proc.returncode == 128 + cli.SIGPIPE


class _ClosedStdout(io.StringIO):
    """A stdout whose reader has gone away; no file descriptor, so the devnull
    redirect stays off the test runner's fd 1."""

    def write(self, s):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError


def test_print_message_raises_broken_pipe_on_stdout(monkeypatch):
    # Pins the private argparse hook: stock argparse swallows this error.
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())
    with pytest.raises(BrokenPipeError):
        cli._Parser(prog="p")._print_message("help text", sys.stdout)


def test_print_message_swallows_broken_pipe_on_other_streams(monkeypatch):
    monkeypatch.setattr(sys, "stderr", _ClosedStdout())
    cli._Parser(prog="p")._print_message("usage text", sys.stderr)


@posix_only
def test_usage_error_keeps_status_when_stderr_closed():
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "daedalus.cli", "--no-such-flag"],
            stdout=subprocess.PIPE, stderr=write_end, timeout=60,
        )
    finally:
        os.close(write_end)
    assert proc.returncode == 2


@posix_only
def test_usage_error_keeps_status_when_stdout_closed():
    proc = _run_into_closed_pipe(sys.executable, "-m", "daedalus.cli", "--no-such-flag")
    assert proc.returncode == 2
    assert "Exception ignored" not in proc.stderr


@posix_only
@pytest.mark.parametrize("mode, status, message", [
    ("refused", 2, "daedalus: refused"),
    ("interrupted", 130, "daedalus: interrupted"),
])
def test_error_paths_flush_into_closed_pipe(mode, status, message):
    proc = _run_into_closed_pipe(sys.executable, "-c", DRIVER, mode)
    assert proc.stderr.strip() == message
    assert proc.returncode == status


@posix_only
def test_broken_pipe_from_another_pipe_is_not_hidden():
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, "other_pipe"], capture_output=True, text=True, timeout=60
    )
    assert "BrokenPipeError" in proc.stderr
    assert proc.returncode == 1


def test_broken_pipe_in_process_returns_sigpipe_status(monkeypatch):
    def broken_pipe(args):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "cmd_status", broken_pipe)
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())
    assert cli.main(["status"]) == 128 + cli.SIGPIPE


def test_broken_pipe_in_process_with_healthy_stdout_propagates(monkeypatch):
    def broken_pipe(args):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "cmd_status", broken_pipe)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(BrokenPipeError):
        cli.main(["status"])


@pytest.mark.parametrize("exc, status", [(cli.DaedalusError("no"), 2), (KeyboardInterrupt(), 130)])
def test_error_paths_flush_closed_stdout_in_process(monkeypatch, exc, status):
    def failing(args):
        raise exc

    silenced = []
    monkeypatch.setattr(cli, "cmd_status", failing)
    monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert cli.main(["status"]) == status
    assert silenced, "the error path must flush stdout and silence it"


@pytest.mark.parametrize("exc, status", [(cli.DaedalusError("no"), 2), (KeyboardInterrupt(), 130)])
def test_error_paths_survive_closed_stderr(monkeypatch, exc, status):
    def failing(args):
        raise exc

    silenced = []
    monkeypatch.setattr(cli, "cmd_status", failing)
    monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())
    monkeypatch.setattr(sys, "stderr", _ClosedStdout())
    assert cli.main(["status"]) == status
    assert silenced


def test_stdout_broken_after_print_message_failure(monkeypatch):
    monkeypatch.setattr(cli, "_stdout_failed", False)
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())
    with pytest.raises(BrokenPipeError):
        cli._Parser(prog="p")._print_message("help text", sys.stdout)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli._stdout_broken()


@posix_only
def test_stdout_broken_detects_closed_reader(monkeypatch):
    read_end, write_end = os.pipe()
    os.close(read_end)
    stream = os.fdopen(write_end, "w")
    try:
        monkeypatch.setattr(cli, "_stdout_failed", False)
        monkeypatch.setattr(sys, "stdout", stream)
        assert cli._stdout_broken()
    finally:
        monkeypatch.undo()
        stream.close()


@pytest.mark.parametrize("exc", [ValueError("boom"), OSError("disk"), ConnectionResetError("peer")])
def test_other_exceptions_propagate_in_process(monkeypatch, exc):
    def failing(args):
        raise exc

    monkeypatch.setattr(cli, "cmd_status", failing)
    with pytest.raises(type(exc)):
        cli.main(["status"])
