"""Shared fixtures: a synthetic git repository and a controllable clock."""

from __future__ import annotations

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
