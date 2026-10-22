"""First-run experience: check detection, `init`, grouped help, and success that reads as success."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import git, make_repo
from daedalus import cli
from daedalus.core.config import detect_checks, load_config, render_config

# How `init` quotes the commit message for the user's shell: cmd.exe has no single quotes.
COMMIT_ARG = 'commit -m "Configure Daedalus"' if os.name == "nt" else "commit -m 'Configure Daedalus'"


def git_out(root: Path, *args: str) -> str:
    """stdout of a git command (the shared `git` fixture helper discards it)."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _cmd(checks: dict, cid: str = "tests") -> list[str]:
    return checks[cid]["command"]


# ------------------------------------------------------------- detect_checks
def test_pytest_is_found_from_a_tests_directory_alone(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_a(): pass\n", encoding="utf-8")
    assert _cmd(detect_checks(tmp_path)) == ["python", "-m", "pytest", "-q"]


def test_pytest_is_found_from_a_loose_test_file(tmp_path):
    (tmp_path / "test_app.py").write_text("def test_a(): pass\n", encoding="utf-8")
    assert _cmd(detect_checks(tmp_path)) == ["python", "-m", "pytest", "-q"]


def test_a_javascript_tests_directory_is_not_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "app.test.js").write_text("test('a', () => {})\n", encoding="utf-8")
    assert detect_checks(tmp_path) == {}


def test_uv_projects_run_pytest_through_uv(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert _cmd(detect_checks(tmp_path)) == ["uv", "run", "pytest", "-q"]


def test_tox_and_makefile_targets(tmp_path):
    (tmp_path / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    assert _cmd(detect_checks(tmp_path)) == ["tox"]
    other = tmp_path / "other"
    other.mkdir()
    (other / "Makefile").write_text("build:\n\tcc x.c\n\ntest: build\n\t./a.out\n", encoding="utf-8")
    assert _cmd(detect_checks(other)) == ["make", "test"]
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    (nothing / "Makefile").write_text("build:\n\tcc x.c\n", encoding="utf-8")
    assert detect_checks(nothing) == {}


@pytest.mark.parametrize(
    "lock, expected",
    [
        (None, ["npm", "test", "--silent"]),
        ("pnpm-lock.yaml", ["pnpm", "test"]),
        ("yarn.lock", ["yarn", "test"]),
        ("bun.lockb", ["bun", "run", "test"]),
    ],
)
def test_package_json_test_script_uses_the_lockfile_manager(tmp_path, lock, expected):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    if lock:
        (tmp_path / lock).write_text("", encoding="utf-8")
    assert _cmd(detect_checks(tmp_path)) == expected


def test_the_npm_init_placeholder_test_script_is_ignored(tmp_path):
    placeholder = 'echo "Error: no test specified" && exit 1'
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": placeholder}}), encoding="utf-8")
    assert detect_checks(tmp_path) == {}


def test_cargo_and_go_are_still_detected(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert _cmd(detect_checks(tmp_path)) == ["cargo", "test", "--quiet"]
    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    assert _cmd(detect_checks(go)) == ["go", "test", "./..."]


def test_lint_and_typecheck_are_optional_and_only_when_configured(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n",
                                             encoding="utf-8")
    checks = detect_checks(tmp_path)
    assert checks["tests"].get("mandatory", True) is True
    assert checks["lint"]["mandatory"] is False and checks["typecheck"]["mandatory"] is False
    assert checks["lint"]["command"][:3] == ["ruff", "check", "."]

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert set(detect_checks(plain)) == {"tests"}


def test_tsc_needs_typescript_and_a_tsconfig(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"typescript": "5"}}), encoding="utf-8")
    assert "typecheck" not in detect_checks(tmp_path)
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert detect_checks(tmp_path)["typecheck"]["command"] == ["npx", "--no-install", "tsc", "--noEmit"]


def test_rendered_config_has_no_literal_braces_when_nothing_is_detected():
    text = render_config({})
    assert "{}" not in text
    assert yaml.safe_load(text)["checks"] is None


def test_rendered_optional_checks_load_as_optional(tmp_path):
    checks = {
        "tests": {"command": ["python", "-m", "pytest", "-q"]},
        "lint": {"command": ["ruff", "check", "."], "mandatory": False},
    }
    (tmp_path / ".daedalus.yml").write_text(render_config(checks), encoding="utf-8")
    policy = load_config(tmp_path).policy
    assert policy.mandatory_checks() == ("tests",)
    assert list(policy.checks["lint"].command) == ["ruff", "check", "."]


# ------------------------------------------------------------------ init
def _fresh_repo(tmp_path: Path) -> Path:
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def test_init_without_commit_prints_the_exact_command(tmp_path, capsys):
    root = _fresh_repo(tmp_path)
    assert cli.main(["-C", str(root), "init", "--no-hooks"]) == 0
    out = capsys.readouterr().out
    assert "git -C" in out and "add .daedalus.yml" in out and COMMIT_ARG in out
    assert git_out(root, "log", "--oneline").count("\n") == 1  # nothing was committed


def test_init_commit_commits_only_what_it_wrote_with_no_trailers(tmp_path, capsys):
    root = _fresh_repo(tmp_path)
    (root / "unrelated.txt").write_text("keep out\n", encoding="utf-8")
    git(root, "add", "unrelated.txt")  # staged by the user, not by init
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--commit"]) == 0
    assert "committed" in capsys.readouterr().out
    show = git_out(root, "show", "--stat", "--format=%an <%ae>%n%B", "HEAD")
    assert show.startswith("Dev <dev@example.com>")  # the configured identity
    assert "Configure Daedalus" in show and "Co-Authored-By" not in show and "Claude" not in show
    assert ".daedalus.yml" in show and "unrelated.txt" not in show
    assert git_out(root, "diff", "--cached", "--name-only").split() == ["unrelated.txt"]


def test_init_commit_outside_a_repository_is_a_clear_error(tmp_path, capsys):
    root = tmp_path / "plain"
    root.mkdir()
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--commit"]) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_init_force_keeps_a_backup_of_the_old_config(tmp_path, capsys):
    root = _fresh_repo(tmp_path)
    (root / ".daedalus.yml").write_text("enabled: true\n# hand-edited\n", encoding="utf-8")
    assert cli.main(["-C", str(root), "init", "--no-hooks"]) == 0  # exists: kept
    assert (root / ".daedalus.yml").read_text(encoding="utf-8").endswith("# hand-edited\n")
    assert not (root / ".daedalus.yml.bak").exists()
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--force"]) == 0
    assert (root / ".daedalus.yml.bak").read_text(encoding="utf-8") == "enabled: true\n# hand-edited\n"
    check = load_config(root).policy.checks["tests"]  # the same command as before, now with its detected timeout
    assert list(check.command) == ["python", "-m", "pytest", "-q"] and check.timeout_s == 1800


# ------------------------------------------------------------------ help
def test_help_groups_commands_and_lists_internals_last(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    titles = ["Start here", "Daily", "Human decisions", "Factory", "Integrations", "Advanced / internal"]
    positions = [out.index(t) for t in titles]
    assert positions == sorted(positions)
    start = out[positions[0]:positions[1]]
    assert all(c in start for c in ("init", "demo", "status", "doctor"))
    internal = out[positions[-1]:]
    assert all(c in internal for c in ("hook", "statusline", "reconcile", "act"))


def test_every_registered_command_is_listed_in_help(capsys):
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in sub.choices:
        assert f"    {name} " in out, name


def test_demo_without_its_module_says_so(monkeypatch, capsys):
    import sys

    import daedalus

    monkeypatch.setitem(sys.modules, "daedalus.demo", None)  # makes `from daedalus import demo` fail
    monkeypatch.delattr(daedalus, "demo", raising=False)  # an earlier import leaves the attribute behind
    assert cli.main(["demo"]) == 2
    assert "demo not available in this build" in capsys.readouterr().err


# ------------------------------------------------------- success reads as success
def test_status_says_ready_not_blocked_when_only_finishing_is_left(repo, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr("daedalus.harness.claude_code.launched_by_daedalus", lambda: False)
    (repo / "app.txt").write_text("ok\n", encoding="utf-8")
    assert cli.main(["-C", str(repo), "start", "make app ok"]) == 0
    assert cli.main(["-C", str(repo), "verify", "--no-finish"]) == 0
    out = capsys.readouterr().out
    assert "Disposition: READY" in out and "BLOCKED" not in out

    assert cli.main(["-C", str(repo), "status"]) in (0, 1)
    status = capsys.readouterr().out
    assert "Disposition: READY" in status and "BLOCKED" not in status


def test_verify_finishes_the_run_when_every_condition_holds(repo, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr("daedalus.harness.claude_code.launched_by_daedalus", lambda: False)
    (repo / "app.txt").write_text("ok\n", encoding="utf-8")
    assert cli.main(["-C", str(repo), "start", "make app ok"]) == 0
    assert cli.main(["-C", str(repo), "verify"]) == 0
    out = capsys.readouterr().out
    assert "Disposition: ACCEPTED" in out
    assert cli.main(["-C", str(repo), "status"]) == 0
    assert "ACCEPTED" in capsys.readouterr().out


def test_auto_finish_can_be_turned_off_in_config(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr("daedalus.harness.claude_code.launched_by_daedalus", lambda: False)
    root = make_repo(tmp_path / "noauto", {"harness": {"auto_finish": False}})
    (root / "app.txt").write_text("ok\n", encoding="utf-8")
    assert cli.main(["-C", str(root), "start", "make app ok"]) == 0
    assert cli.main(["-C", str(root), "verify"]) == 0
    assert "Disposition: READY" in capsys.readouterr().out


def test_a_failing_check_is_still_blocked(repo, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr("daedalus.harness.claude_code.launched_by_daedalus", lambda: False)
    assert cli.main(["-C", str(repo), "start", "make app ok"]) == 0
    assert cli.main(["-C", str(repo), "verify"]) == 1  # app.txt still says "bad"
    out = capsys.readouterr().out
    assert "Disposition: BLOCKED" in out and "READY" not in out
