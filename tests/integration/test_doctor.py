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


def _cash_not_a_number(root, bindir, mp):
    mp.setenv("ANTHROPIC_API_KEY", "sk-test")
    edit_config(root, policy={"default_budget": {"max_cash": "lots"}})


def _cash_infinite(root, bindir, mp):
    mp.setenv("ANTHROPIC_API_KEY", "sk-test")
    edit_config(root, policy={"default_budget": {"max_cash": float("inf")}})


def _write_settings(root, text):
    (root / ".claude" / "settings.json").write_text(text, encoding="utf-8")
    git(root, "commit", "-qam", "edit settings")


def _hook_event_not_a_list(root, bindir, mp):
    _write_settings(root, json.dumps({"hooks": {ev: 5 for ev in ("Stop", "SessionStart")}}))


def _hook_group_hooks_not_a_list(root, bindir, mp):
    _write_settings(root, json.dumps({"hooks": {"Stop": [{"hooks": 3}], "SessionStart": [{"hooks": True}]}}))


def _hook_command_not_a_string(root, bindir, mp):
    group = [{"hooks": [{"type": "command", "command": 5}]}]
    _write_settings(root, json.dumps({"hooks": {"Stop": group, "SessionStart": group}}))


def _cash_negative(root, bindir, mp):
    mp.setenv("ANTHROPIC_API_KEY", "sk-test")
    edit_config(root, policy={"default_budget": {"max_cash": -3}})


def _settings_not_json(root, bindir, mp):
    _write_settings(root, "{not json")


def _no_git(root, bindir, mp):
    mp.setenv("PATH", str(bindir))


def _git_hook_not_executable(root, bindir, mp):
    provenance.hook_path(root, "pre-push").chmod(0o644)


def _unknown_adapter(root, bindir, mp):
    edit_config(root, adapter="nope")


def _yaml_does_not_parse(root, bindir, mp):
    (root / ".daedalus.yml").write_text("checks: [unclosed\n  mode: : :\n", encoding="utf-8")
    git(root, "commit", "-qam", "break yaml")


CONFIG = ".daedalus.yml committed and valid"
CLAUDE = "Claude Code hooks installed and committed"
GIT_HOOKS = "git hooks installed"
ADAPTER = "launching adapter for reviews"
CASH = "cash budget for billed reviews"

# problem: (breaker, failing check, text in its detail, text in its fix)
PROBLEMS = {
    "no config": (_no_config, CONFIG, "no .daedalus.yml", "run `daedalus init`, then commit"),
    "invalid config": (_invalid_config, CONFIG, "invalid:", "see `daedalus policy --default`"),
    "uncommitted config": (_uncommitted_config, CONFIG, "valid but not committed", "git add .daedalus.yml && git commit"),
    "no mandatory check": (_no_mandatory_check, "a mandatory check", "no mandatory check",
                           "add one under `checks:`"),
    "no claude hooks": (_no_claude_hooks, CLAUDE, "no .claude/settings.json", "run `daedalus install claude-code`"),
    "claude hook missing": (_claude_hooks_incomplete, CLAUDE, "missing hooks: Stop",
                            "run `daedalus install claude-code`"),
    "claude hooks uncommitted": (_claude_hooks_uncommitted, CLAUDE, "is not committed", "git add .claude/ && git commit"),
    "no git hook": (_no_git_hook, GIT_HOOKS, "missing: pre-push", "run `daedalus install claude-code`"),
    "daedalus not on PATH": (_no_daedalus_on_path, "daedalus on PATH", "find nothing", claude_code.INSTALL_HINT),
    "adapter cannot launch": (_inline_adapter, ADAPTER, "cannot launch reviewers", "set `adapter: claude-code`"),
    "adapter not installed": (_adapter_unavailable, ADAPTER, "`claude` is not on PATH", "install `claude`"),
    "cash with no budget": (_cash_without_budget, CASH, "max_cash is 0", "set `max_cash` to a positive amount"),
    "cash not a number": (_cash_not_a_number, CASH, "max_cash is not a number", "set `max_cash` to a positive amount"),
    "cash infinite": (_cash_infinite, CASH, "not a finite amount", "set `max_cash` to a positive amount"),
    "cash negative": (_cash_negative, CASH, "max_cash is negative (-3)", "set `max_cash` to a positive amount"),
    "hook command not a string": (_hook_command_not_a_string, CLAUDE, "missing hooks: Stop, SessionStart",
                                  "run `daedalus install claude-code`"),
    "hook event not a list": (_hook_event_not_a_list, CLAUDE, "missing hooks: Stop, SessionStart",
                              "run `daedalus install claude-code`"),
    "hook group not a list": (_hook_group_hooks_not_a_list, CLAUDE, "missing hooks: Stop, SessionStart",
                              "run `daedalus install claude-code`"),
    "settings not json": (_settings_not_json, CLAUDE, "is unreadable", "repair .claude/settings.json"),
    "no git": (_no_git, GIT_HOOKS, "git is not on PATH", "install git"),
    "git hook not executable": (_git_hook_not_executable, GIT_HOOKS, "not executable", "chmod +x"),
    "unknown adapter": (_unknown_adapter, ADAPTER, "invalid adapter", "correct `adapter:` in .daedalus.yml"),
    "yaml does not parse": (_yaml_does_not_parse, CONFIG, "is not valid YAML", "see `daedalus policy --default`"),
}
POSIX_ONLY = {"git hook not executable"}


@pytest.mark.parametrize("problem", PROBLEMS)
def test_each_problem_is_reported_with_a_fix(problem, ready, bindir, capsys, monkeypatch):
    if problem in POSIX_ONLY and os.name != "posix":
        pytest.skip("executable bits are POSIX only")
    breaker, name, detail, hint = PROBLEMS[problem]
    breaker(ready, bindir, monkeypatch)
    code, out = doctor(ready, capsys)
    assert code != 0, out
    bad = failed(out)
    assert name in bad, out
    assert detail in bad[name][0], out
    assert hint in bad[name][1], out


def test_outside_a_git_repository_the_fix_is_git_init(tmp_path, bindir, capsys, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "plain"
    root.mkdir()
    (root / ".daedalus.yml").write_text(yaml.safe_dump(
        {"enabled": True, "mode": "gate", "checks": {"tests": {"command": ["true"]}}}), encoding="utf-8")
    claude_code.install(root)
    code, out = doctor(root, capsys)
    assert code == 1, out
    bad = failed(out)
    for name in (CONFIG, CLAUDE, GIT_HOOKS):
        assert bad[name] == ("not a git repository", "run `git init`, then `daedalus install claude-code`"), out
    assert "git add" not in out


def test_a_yaml_parse_error_stays_on_one_line(ready, bindir, capsys, monkeypatch):
    _yaml_does_not_parse(ready, bindir, monkeypatch)
    _, out = doctor(ready, capsys)
    line = next(ln for ln in out.splitlines() if ln.startswith("FAIL") and CONFIG in ln)
    assert "is not valid YAML" in line and "line" in line, out  # the scanner's position survives collapsing
    assert out.splitlines()[out.splitlines().index(line) + 1].strip().startswith("fix: "), out


def test_without_git_every_git_check_says_install_git(ready, bindir, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(bindir))
    code, out = doctor(ready, capsys)
    assert code == 1, out
    bad = failed(out)
    for name in (CONFIG, CLAUDE, GIT_HOOKS):
        assert bad[name] == ("git is not on PATH", "install git"), out
    assert "git add" not in out


def test_outside_a_git_repository_commit_fixes_start_with_git_init(tmp_path, bindir, capsys, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "nothing"
    root.mkdir()
    code, out = doctor(root, capsys)
    assert code == 1, out
    bad = failed(out)
    assert bad[CONFIG] == ("no .daedalus.yml", "run `git init`, then run `daedalus init`, then commit .daedalus.yml")
    assert bad[CLAUDE][1].startswith("run `git init`, then run `daedalus install claude-code`"), out
    assert bad[GIT_HOOKS] == ("not a git repository", "run `git init`, then `daedalus install claude-code`"), out


@pytest.mark.skipif(os.name != "posix", reason="the stand-in git is a shell script")
def test_another_git_error_is_shown_instead_of_not_a_repository(ready, bindir, capsys, monkeypatch):
    (bindir / "git").write_text("#!/bin/sh\necho 'fatal: index file corrupt' >&2\necho more >&2\nexit 128\n",
                                encoding="utf-8")
    (bindir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    _, out = doctor(ready, capsys)
    bad = failed(out)
    for name in (CONFIG, CLAUDE):
        assert bad[name][0] == "git failed: fatal: index file corrupt", out
        assert "git init" not in bad[name][1], out


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


def test_doctor_does_not_refresh_a_stat_stale_index(ready, bindir, capsys):
    """Touching a tracked file leaves its content alone but makes the index stale on stat:
    a plain `git status` would rewrite .git/index, and doctor must not."""
    index = ready / ".git" / "index"
    old = index.stat().st_mtime - 100
    os.utime(index, (old, old))
    for tracked in (ready / ".daedalus.yml", ready / ".claude" / "settings.json", ready / "app.txt"):
        os.utime(tracked, None)
    before = tree_state(ready)
    before_mtime = index.stat().st_mtime_ns
    doctor(ready, capsys)
    assert tree_state(ready) == before
    assert index.stat().st_mtime_ns == before_mtime


def test_doctor_creates_nothing_in_an_empty_directory(tmp_path, bindir, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q")
    before = tree_state(root)
    doctor(root, capsys)
    assert tree_state(root) == before
