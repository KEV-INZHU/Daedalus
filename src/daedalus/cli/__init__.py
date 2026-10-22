"""`daedalus` command line: the reference harness and lowest common denominator.

Day-to-day surface is three things: a toggle (`on`/`off`, or `init`), a
contract (`start "goal"` / `run "goal"`), and `status`. Everything else is for
humans deciding things (`approve`, `attest`, ...) or for digging into the audit
trail (`log`, `audit`).

Human-authority commands refuse to run inside an agent session or without an
interactive terminal. That is defence in depth, not a security boundary: see
docs/security-model.md.

Layout: one module per command group next to this file. A module defines
`register(subparsers)` and adds its commands with `add_command`; any public module in this
package that does so is picked up by `build_parser`. `GROUPS` decides where a command is
listed in `--help`. Argument parsing, the closed-pipe handling and `main` live here.
"""

from __future__ import annotations

import argparse
import importlib
import os
import pkgutil
import sys
from typing import Any

from daedalus import __version__
from daedalus.cli._common import (  # noqa: F401  (re-exported: other modules and tests import these from daedalus.cli)
    EXIT,
    SEV_ABBR,
    _ari,
    _print,
    _run_id,
    _spec_from_args,
    human_actor,
    human_name,
    in_agent_session,
    render_decision,
    starter_actor,
)
from daedalus.core.errors import DaedalusError


def _interactive() -> bool:
    """Whether a person is at a terminal. Defined here (not in _common) so tests can patch it."""
    return sys.stdin.isatty()


# Where each command is listed in `--help`. A command that is not named here still works and is
# listed under "Advanced / internal", so a new module cannot vanish from the help.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Start here", ("init", "demo", "status", "doctor")),
    ("Daily", ("start", "run", "verify", "finish", "review", "cancel", "runs", "log", "audit", "on", "off")),
    ("Human decisions", ("approve", "deny", "attest", "amend", "risk-exception", "resolve", "abandon")),
    ("Factory", ("factory",)),
    ("Integrations", ("install", "plugins", "ci", "metrics", "report", "policy")),
    ("Advanced / internal", ("dispute", "arbitrate", "blocker", "restore", "hook", "statusline", "reconcile", "act")),
)
FALLBACK_GROUP = "Advanced / internal"


def _command_modules() -> list[Any]:
    """Every public module of this package that registers commands."""
    mods = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if callable(getattr(mod, "register", None)):
            mods.append(mod)
    return mods


def grouped_help(sub: Any) -> str:
    """The command list for `--help`, in GROUPS order (hidden internals last, one line each)."""
    helps = {name: (parser.description or "") for name, parser in sub.choices.items()}
    listed: dict[str, list[str]] = {title: [] for title, _ in GROUPS}
    placed: set[str] = set()
    for title, names in GROUPS:
        for name in names:
            if name in helps:
                listed[title].append(name)
                placed.add(name)
    listed[FALLBACK_GROUP] += [n for n in helps if n not in placed]
    width = max((len(n) for n in helps), default=0) + 2
    out = ["commands:"]
    for title, _ in GROUPS:
        if not listed[title]:
            continue
        out.append(f"\n  {title}")
        for name in listed[title]:
            out.append(f"    {name.ljust(width)}{helps[name]}")
    out.append("\nRun `daedalus <command> --help` for a command's options.")
    return "\n".join(out)


class _Parser(argparse.ArgumentParser):
    """argparse swallows OSError while printing `--help`, so a closed pipe would exit 0
    with the output silently lost; let a BrokenPipeError on stdout reach main() instead.
    Errors on other streams (a usage message to a closed stderr) stay swallowed so the
    usage-error status is kept. This overrides a private hook; tests/unit/test_cli_pipe.py pins it."""

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            file = file or sys.stderr
            try:
                file.write(message)
            except BrokenPipeError:
                if file is sys.stdout:
                    global _stdout_failed
                    _stdout_failed = True
                    raise
                _silence(file)  # see below
            except (AttributeError, OSError):
                # A closed stderr: what failed to write stays buffered, and flushing it at
                # interpreter shutdown would fail too and turn the usage status into 120.
                _silence(file)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="daedalus",
        description="Run agent work under a deterministic acceptance gate.",
        usage="daedalus [-h] [--version] [-C DIR] command ...",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"daedalus {__version__}")
    p.add_argument("-C", "--dir", default=".", help="repository directory (default: current)")
    # The flat list argparse would print is replaced by the grouped one in the epilog.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command", help=argparse.SUPPRESS)
    for mod in _command_modules():
        mod.register(sub)
    p.epilog = grouped_help(sub)
    return p


def utf8_output() -> None:
    """Status lines use non-ASCII separators; terminals such as Git Bash expect UTF-8
    while Python defaults to the ANSI code page for pipes on Windows."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    global _stdout_failed
    _stdout_failed = False
    utf8_output()
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:
            # `--help` and `--version` exit with their output still buffered; a usage
            # error keeps its own status even when stdout has gone away.
            if _flush_stdout() and not exc.code:
                return 128 + SIGPIPE
            raise
        code = int(args.fn(args) or 0)
    except DaedalusError as exc:
        _warn(f"daedalus: {exc}")
        code = 2
    except KeyboardInterrupt:
        _warn("daedalus: interrupted")
        code = 130
    except BrokenPipeError:
        if not _stdout_broken():
            raise  # some other pipe broke; that is a real failure
        _silence_stdout()
        return 128 + SIGPIPE
    # Flush on every exit path so a reader that closed early surfaces here,
    # not as an "Exception ignored" message at interpreter shutdown.
    if _flush_stdout() and code == 0:
        return 128 + SIGPIPE
    return code


_stdout_failed = False  # set by _Parser._print_message when a write to stdout breaks
SIGPIPE = 13  # signal.SIGPIPE on POSIX; Windows lacks the constant but the convention holds.


def _flush_stdout() -> bool:
    """Flush stdout; if its reader has gone away, silence it and return True."""
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()
        return True
    return False


def _stdout_broken() -> bool:
    """Whether stdout's reader has gone away, as opposed to some other pipe breaking."""
    if _stdout_failed:
        return True  # _Parser._print_message saw the write to stdout fail
    try:
        sys.stdout.flush()  # output that failed to reach the reader stays buffered
    except BrokenPipeError:
        return True
    except (OSError, ValueError, AttributeError):
        return False
    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        return False  # no descriptor, so the pipe that broke was not stdout
    import select

    if not hasattr(select, "poll"):
        # Windows has no poll() for pipes. Keep the old behaviour and treat the error as a
        # closed reader: the flush above already catches the usual case, and a traceback
        # for `daedalus ... | more` is the worse mistake there.
        return True
    try:
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        events = poller.poll(0)
    except (OSError, ValueError):
        return False
    # A pipe whose readers have all closed reports POLLERR on Linux and POLLHUP on macOS/BSD.
    return any(mask & (select.POLLERR | select.POLLHUP) for _, mask in events)


def _warn(message: str) -> None:
    """Report on stderr; a closed stderr must not cost the exit status or the stdout flush."""
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError, AttributeError):
        _silence(sys.stderr)


def _silence_stdout() -> None:
    """The reader went away (`daedalus status | head`). Point stdout at devnull so the
    final flush at exit cannot raise again; see the Python `signal` docs on SIGPIPE."""
    _silence(sys.stdout)


def _silence(stream: Any) -> None:
    """Point a stream whose reader is gone at devnull, so the output still buffered in it
    cannot fail again at the final flush (which would make the exit status 120)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        # not backed by a file descriptor (e.g. replaced in tests)
        pass
