"""`daedalus doctor`: every setup problem is named with a one-line fix, a set-up
repository passes, and the command never changes anything."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from conftest import git, make_repo
from daedalus import cli
from daedalus.adapters.command import BILLING_ENV_VARS
from daedalus.harness import claude_code, provenance


def _fake_exe(bindir: Path, name: str) -> None:
    (bindir / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / name).chmod(0o755)
    (bindir / f"{name}.bat").write_text("@exit /b 0\n", encoding="utf-8")  # Windows


@pytest.fixture
def bindir(tmp_path: Path, monkeypatch) -> Path:
    """A PATH with git, a stand-in `daedalus` and a stand-in `claude`, and nothing else."""
    d = tmp_path / "bin"
    d.mkdir()
    _fake_exe(d, "daedalus")
    _fake_exe(d, "claude")
    monkeypatch.setenv("PATH", os.pathsep.join([str(d), str(Path(shutil.which("git")).parent)]))
    for var in BILLING_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return d


@pytest.fixture
def ready(tmp_path: Path, bindir: Path) -> Path:
    root = make_repo(tmp_path / "ready", {"adapter": "claude-code"})
    claude_code.install(root)
    provenance.install_git_hook(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "install daedalus")
    return root


def doctor(root: Path, capsys) -> tuple[int, str]:
    code = cli.main(["-C", str(root), "doctor"])
    return code, capsys.readouterr().out


def failed(out: str) -> dict[str, tuple[str, str]]:
    """{check name: (detail, fix)} for every failing check."""
    lines = out.splitlines()
    res = {}
    for i, line in enumerate(lines):
        if line.startswith("FAIL"):
            name, _, detail = line[4:].strip().partition(": ")
            fix = lines[i + 1].strip()
            assert fix.startswith("fix: ") and len(fix) > len("fix: "), out
            res[name] = (detail, fix[len("fix: "):])
    return res


def edit_config(root: Path, commit: bool = True, **changes) -> None:
    p = root / ".daedalus.yml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg.update(changes)
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if commit:
        git(root, "commit", "-qam", "edit config")


def tree_state(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under the repository, .git included: content hash and mode."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[p.relative_to(root).as_posix()] = (
            hashlib.sha256(p.read_bytes()).digest() if p.is_file() else b"", st.st_mode
        )
    return out


# ------------------------------------------------------------------- C2: passes
def test_a_set_up_repository_passes(ready, capsys):
    code, out = doctor(ready, capsys)
    assert code == 0, out
    assert "FAIL" not in out
    assert "ready" in out.splitlines()[-1]


def test_cash_budget_above_zero_passes_when_reviews_are_billed(ready, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    edit_config(ready, policy={"default_budget": {"max_cash": 5}})
    code, out = doctor(ready, capsys)
    assert code == 0, out
    assert "cash budget" in out


# --------------------------------------------------- C1: each problem, with a fix
def _no_config(root, bindir, mp):
    git(root, "rm", "-q", ".daedalus.yml")
    git(root, "commit", "-qm", "drop config")


def _invalid_config(root, bindir, mp):
    edit_config(root, mode="nonsense")


def _uncommitted_config(root, bindir, mp):
    edit_config(root, commit=False, mode="review")


def _no_mandatory_check(root, bindir, mp):
    edit_config(root, checks={"tests": {"command": ["true"], "mandatory": False}})


def _no_claude_hooks(root, bindir, mp):
    git(root, "rm", "-q", ".claude/settings.json")
    git(root, "commit", "-qm", "drop hooks")


def _claude_hooks_incomplete(root, bindir, mp):
    p = root / ".claude" / "settings.json"
    s = json.loads(p.read_text(encoding="utf-8"))
    del s["hooks"]["Stop"]
    p.write_text(json.dumps(s), encoding="utf-8")
    git(root, "commit", "-qam", "lose the stop hook")


def _claude_hooks_uncommitted(root, bindir, mp):
    git(root, "rm", "-q", "--cached", ".claude/settings.json")
    git(root, "commit", "-qm", "untrack hooks")


def _no_git_hook(root, bindir, mp):
    provenance.hook_path(root, "pre-push").unlink()


def _no_daedalus_on_path(root, bindir, mp):
    for p in bindir.glob("daedalus*"):
        p.unlink()


def _inline_adapter(root, bindir, mp):
    edit_config(root, adapter="inline")


def _adapter_unavailable(root, bindir, mp):
    for p in bindir.glob("claude*"):
        p.unlink()


def _cash_without_budget(root, bindir, mp):
    mp.setenv("ANTHROPIC_API_KEY", "sk-test")


PROBLEMS = {
    "no config": (_no_config, ".daedalus.yml committed and valid", "daedalus init"),
    "invalid config": (_invalid_config, ".daedalus.yml committed and valid", ".daedalus.yml"),
    "uncommitted config": (_uncommitted_config, ".daedalus.yml committed and valid", "git add .daedalus.yml"),
    "no mandatory check": (_no_mandatory_check, "a mandatory check", "checks:"),
    "no claude hooks": (_no_claude_hooks, "Claude Code hooks installed and committed", "daedalus install claude-code"),
    "claude hook missing": (_claude_hooks_incomplete, "Claude Code hooks installed and committed",
                            "daedalus install claude-code"),
    "claude hooks uncommitted": (_claude_hooks_uncommitted, "Claude Code hooks installed and committed", "git add .claude/"),
    "no git hook": (_no_git_hook, "git hooks installed", "daedalus install claude-code"),
    "daedalus not on PATH": (_no_daedalus_on_path, "daedalus on PATH", "pip install"),
    "adapter cannot launch": (_inline_adapter, "launching adapter for reviews", "adapter: claude-code"),
    "adapter not installed": (_adapter_unavailable, "launching adapter for reviews", "claude"),
    "cash with no budget": (_cash_without_budget, "cash budget for billed reviews", "max_cash"),
}


@pytest.mark.parametrize("problem", PROBLEMS)
def test_each_problem_is_reported_with_a_fix(problem, ready, bindir, capsys, monkeypatch):
    breaker, name, hint = PROBLEMS[problem]
    breaker(ready, bindir, monkeypatch)
    code, out = doctor(ready, capsys)
    assert code != 0, out
    bad = failed(out)
    assert name in bad, out
    assert hint in bad[name][1], out


def test_an_unset_repository_lists_every_problem(tmp_path, bindir, capsys):
    root = tmp_path / "bare"
    root.mkdir()
    git(root, "init", "-q")
    for p in bindir.iterdir():
        p.unlink()
    code, out = doctor(root, capsys)
    assert code == 1
    assert set(failed(out)) >= {
        ".daedalus.yml committed and valid", "a mandatory check", "Claude Code hooks installed and committed",
        "git hooks installed", "daedalus on PATH", "launching adapter for reviews",
    }


# ------------------------------------------------------------ C3: changes nothing
@pytest.mark.parametrize("problem", [None, "uncommitted config", "no claude hooks", "no git hook"])
def test_doctor_changes_nothing(problem, ready, bindir, capsys, monkeypatch):
    if problem:
        PROBLEMS[problem][0](ready, bindir, monkeypatch)
    before = tree_state(ready)
    doctor(ready, capsys)
    assert tree_state(ready) == before


def test_doctor_creates_nothing_in_an_empty_directory(tmp_path, bindir, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q")
    before = tree_state(root)
    doctor(root, capsys)
    assert tree_state(root) == before
