"""`daedalus demo`: a 20-second, hermetic demonstration of the gate.

A scripted agent first cheats (it guts the failing test instead of fixing the bug), then a second one
fixes the bug honestly. Every verdict printed here comes from the real Ariadne/gate; nothing is
simulated except the agents' edits. No network, API key or agent binary is needed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from daedalus.core.acceptance import Decision, Disposition
from daedalus.core.errors import DaedalusError

DEMO_ACTOR = "human:demo"
DOCS_URL = "https://github.com/KEV-INZHU/Daedalus#readme"
TEST_PATH = "tests/test_calc.py"

CHEAT_TEST = "from calc import add\n\n\ndef test_add():\n    assert True\n"


class Out:
    """Minimal printer: ANSI colour on a TTY (never with NO_COLOR), pauses only on a TTY."""

    def __init__(self, fast: bool = False, stream=None):
        self.stream = stream or sys.stdout
        tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.color = tty and not os.environ.get("NO_COLOR")
        self.pace = tty and not fast

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text: str = "", delay: float = 0.4) -> None:
        print(text, file=self.stream, flush=True)
        if self.pace and delay:
            time.sleep(delay)

    def scene(self, title: str) -> None:
        self.line()
        self.line(self.c("1;36", f"== {title} =="), 0.6)

    def agent(self, text: str) -> None:
        self.line(f"  {self.c('2', 'agent>')} {text}")

    def gate(self, text: str, *, good: bool | None = None, delay: float = 0.4) -> None:
        code = {True: "1;32", False: "1;31", None: "1;33"}[good]
        self.line(f"  {self.c(code, 'gate>')} {text}", delay)


# The demo ignores the user's git configuration (a signing key, hooks, templates) so it behaves the
# same everywhere. Applied to the whole process while the demo runs, so Ariadne's own git calls are
# covered too.
_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
# Variables that redirect git to another repository or object store: removed while the demo runs.
_GIT_UNSET = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES")
_GIT_FLAGS = ("-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}")


@contextmanager
def hermetic_git() -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in (*_GIT_ENV, *_GIT_UNSET)}
    for k in _GIT_UNSET:
        os.environ.pop(k, None)
    os.environ.update(_GIT_ENV)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _git(root: Path, *args: str) -> None:
    try:
        subprocess.run(["git", *_GIT_FLAGS, "-C", str(root), *args], check=True, capture_output=True,
                       env={**{k: v for k, v in os.environ.items() if k not in _GIT_UNSET}, **_GIT_ENV})
    except FileNotFoundError as exc:
        raise DaedalusError("the demo needs git on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise DaedalusError(
            f"the demo's `git {args[0]}` failed" + (f": {detail[-1]}" if detail else "")
        ) from exc


def _data(name: str) -> str:
    return (resources.files("daedalus") / "data" / "demo" / name).read_text(encoding="utf-8")


def build_project(root: Path) -> None:
    """A tiny repo whose `add` is broken and whose test correctly fails, gated by a `tests` check."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--template=", "-b", "main")
    _git(root, "config", "user.email", "demo@example.com")
    _git(root, "config", "user.name", "Daedalus demo")
    _git(root, "config", "core.autocrlf", "false")
    (root / "tests").mkdir()
    (root / "calc.py").write_text(_data("calc.py"), encoding="utf-8")
    (root / TEST_PATH).write_text(_data("calc_test.py"), encoding="utf-8")
    (root / "run_tests.py").write_text(_data("run_tests.py"), encoding="utf-8")
    cfg = {
        "enabled": True,
        "mode": "gate",
        "checks": {"tests": {"command": [sys.executable, "run_tests.py"], "timeout_s": 60}},
    }
    (root / ".daedalus.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (root / ".gitignore").write_text(".daedalus/\n__pycache__/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "A tiny project with a bug")


def _show_verdict(out: Out, d: Decision) -> None:
    checks = ", ".join(f"{k} {v[0].value}" for k, v in d.check_states.items()) or "no checks"
    out.gate(f"checks: {checks}", good=None)
    if d.disposition is Disposition.ACCEPTED:
        out.gate("ACCEPTED", good=True)
    else:
        out.gate(f"NOT ACCEPTED ({d.disposition.value})", good=False)
        for r in d.reasons:
            out.gate(f"  refused [{r.code}]: {r.message}", good=False, delay=0.2)


def run_demo(out: Out, root: Path, *, cheat: bool = True) -> dict:
    """Play both scenes in `root` (`cheat=False` skips scene 1). Returns what happened."""
    with hermetic_git():
        return _play(out, root, cheat)


def _play(out: Out, root: Path, cheat: bool) -> dict:
    from daedalus.orchestration.ariadne import Ariadne  # deferred: the CLI imports this module on every call

    build_project(root)
    result: dict = {"root": root}
    ari = Ariadne(root)
    try:
        out.line(f"Demo repository: {root}")
        out.line("calc.py has a bug (add returns a - b) and tests/test_calc.py fails, correctly.")
        rid = ari.start({"objective": "fix the failing test in calc.py"}, actor=DEMO_ACTOR)
        result["run_id"] = rid

        if cheat:
            out.scene("Scene 1: the agent that cheats")
            out.agent("Fixing the failing test...")
            (root / TEST_PATH).write_text(CHEAT_TEST, encoding="utf-8")
            out.agent("Done! All tests pass.")
            ari.verify(rid)
            d1 = ari.finish(rid)
            result["scene1"] = d1
            _show_verdict(out, d1)
            if d1.disposition is Disposition.ACCEPTED:
                # Cannot happen with the detector in place; if it does, the demo must fail, not pretend.
                raise DaedalusError("the gate accepted the gutted test: the test-tampering detector "
                                    "(daedalus.verification.test_integrity) is not working in this install")

        out.scene("Scene 2: the agent that does the work")
        out.agent("The test was right. Restoring it and fixing calc.py instead.")
        _git(root, "checkout", "--", TEST_PATH)
        (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        out.agent("Fixed add(); the original test is untouched.")
        ari.verify(rid)
        d2 = ari.finish(rid)
        result["scene2"] = d2
        _show_verdict(out, d2)
        if d2.disposition is Disposition.ACCEPTED:
            n = ari.store.verify_chain()
            state = ari.state(rid)
            checks = ",".join(f"{k}={v[0].value}" for k, v in d2.check_states.items())
            receipt = (f"run {rid} | candidate {d2.candidate_id[:12]} | checks {checks} | "
                       f"cost ${state.cash_used:.2f} | audit chain verified ({n} events)")
            result["receipt"] = receipt
            out.line()
            out.line("  " + out.c("1;32", "receipt: ") + receipt, 0.8)
    finally:
        ari.close()
    return result


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fast", action="store_true", help="no pauses between lines")
    parser.add_argument("--keep", action="store_true",
                        help="leave the demo repository on disk and print its path")


def run(args: argparse.Namespace) -> int:
    out = Out(fast=getattr(args, "fast", False))
    root = Path(tempfile.mkdtemp(prefix="daedalus-demo-")) / "project"
    try:
        result = run_demo(out, root)
        ok = "receipt" in result
        out.line()
        if args.keep:
            out.line(f"Kept: {root}")
            out.line(f"Inspect it: cd {root} && daedalus status && daedalus log")
        out.line("Try it on your repo: " + out.c("1", "daedalus init"), 0.2)
        out.line(f"Docs: {DOCS_URL}", 0)
        return 0 if ok else 1
    finally:
        if not args.keep:
            remove_tree(root.parent)


def remove_tree(path: Path, _retried: set[str] | None = None) -> None:
    """Delete the demo repository. Git writes its objects read-only: Windows refuses to delete
    a read-only file, and POSIX refuses to delete anything inside a read-only directory. Make
    both the entry and its directory writable and retry instead of leaving the repo behind.
    Best effort: it never raises."""
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
            pass  # best effort: a demo leftover in the temp dir is not worth an error

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daedalus demo", description="Watch the gate catch a cheating agent.")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
