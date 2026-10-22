"""Shared fixtures: a synthetic git repository and a controllable clock."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from daedalus.orchestration.ariadne import Ariadne

HUMAN = "human:tester"
BUILDER = "agent:builder"

# The check passes iff app.txt contains "ok". Running it via the current
# interpreter keeps the suite independent of PATH.
CHECK = [sys.executable, "-c", "import sys; sys.exit(0 if 'ok' in open('app.txt').read() else 1)"]


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fake_executable(directory: Path, name: str, script: str) -> Path:
    """Put a stand-in `name` command in `directory` that runs the Python `script` (sys.argv as the
    real CLI would get it). Shell-script fakes cannot run on Windows, where PATH lookup only finds
    names with a PATHEXT extension, so there it is `name.cmd` calling this interpreter."""
    directory.mkdir(parents=True, exist_ok=True)
    body = directory / f"{name}_fake.py"
    body.write_text(script, encoding="utf-8")
    if os.name == "nt":
        exe = directory / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{body}" %*\r\n', encoding="utf-8")
    else:
        exe = directory / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


def bash() -> str:
    """A real bash. On Windows `bash` on PATH is usually the WSL launcher, so use Git's."""
    if os.name == "nt":
        git_exe = shutil.which("git")
        if git_exe:
            for cand in (Path(git_exe).parent.parent / "bin" / "bash.exe", Path(git_exe).parent / "bash.exe"):
                if cand.exists():
                    return str(cand)
    return "bash"


def bash_path(*dirs: str | Path) -> str:
    """PATH for a bash child: `dirs`, then the system tools (Git's on Windows)."""
    if os.name == "nt":
        tools = Path(bash()).parent.parent
        return os.pathsep.join([*map(str, dirs), str(tools / "bin"), str(tools / "usr" / "bin")])
    return os.pathsep.join([*map(str, dirs), "/usr/bin", "/bin"])


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def make_repo(root: Path, config: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    (root / "app.txt").write_text("bad\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "lib.py").write_text("x = 1\n", encoding="utf-8")
    cfg = {"enabled": True, "mode": "gate", "checks": {"tests": {"command": CHECK, "timeout_s": 60}}}
    if config:
        cfg.update(config)
    (root / ".daedalus.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (root / ".gitignore").write_text(".daedalus/\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def ari(repo: Path, clock: Clock):
    a = Ariadne(repo, clock=clock)
    yield a
    a.close()


def fix(root: Path) -> None:
    (root / "app.txt").write_text("ok\n", encoding="utf-8")
