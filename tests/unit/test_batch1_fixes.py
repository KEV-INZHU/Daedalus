"""Regression tests for the first independent review: release artefacts, the single version,
command set, strict config values, runner-config narrowing, init housekeeping."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import git, make_repo
from daedalus import cli
from daedalus.core.config import detect_checks, load_config
from daedalus.core.errors import PolicyError

ROOT = Path(__file__).resolve().parents[2]


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


# ------------------------------------------------------------- release artefacts (C3, C4)
def test_the_documents_a_release_promises_exist():
    for name in ("SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", "CODE_OF_CONDUCT.md", "README.md", "LICENSE"):
        assert (ROOT / name).is_file(), name


def test_release_workflow_tests_checks_the_tag_and_confines_the_publish_token():
    wf = _yaml(".github/workflows/release.yml")
    assert wf[True] == {"push": {"tags": ["v*"]}}  # YAML 1.1 reads `on:` as True
    jobs = wf["jobs"]
    assert set(jobs) == {"test", "build", "publish"}
    assert jobs["build"]["needs"] == "test" and jobs["publish"]["needs"] == "build"
    run_text = "\n".join(s.get("run", "") for s in jobs["test"]["steps"])
    assert "pytest" in run_text and "ruff check" in run_text
    build = "\n".join(s.get("run", "") for s in jobs["build"]["steps"])
    assert "github.ref_name" in yaml.dump(jobs["build"]) and "does not match the built version" in build
    for name, job in jobs.items():  # id-token write only where the publish happens
        has = "id-token" in (job.get("permissions") or {})
        assert has == (name == "publish"), name


def test_ci_workflow_runs_the_full_suite_on_every_os_and_never_on_pull_request_target():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert "pull_request_target" not in wf[True]
    assert set(wf["jobs"]["test"]["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest", "windows-latest"}
    runs = [s for s in wf["jobs"]["test"]["steps"] if "pytest" in s.get("run", "")]
    assert len(runs) == 1 and "if" not in runs[0] and " -m " not in runs[0]["run"].replace("python -m pytest", "")
    assert "slow" not in text


def test_the_slow_marker_is_not_promised_anywhere():
    for rel in ("CONTRIBUTING.md", "CHANGELOG.md", "README.md", ".github/workflows/ci.yml"):
        assert "SLOW_PATHS" not in (ROOT / rel).read_text(encoding="utf-8"), rel
        assert 'not slow' not in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_composite_action_passes_inputs_through_env_and_guards_its_base():
    action = _yaml("action.yml")
    for step in action["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")  # never interpolated into the script
    gate = next(s for s in action["runs"]["steps"] if s.get("name") == "Gate")
    assert "BASE_INPUT" in gate["env"] and "EXTRA_ARGS" in gate["env"]
    script = gate["run"]
    assert "set -f" in script and "exit 2" in script  # no globbing of extra-args; a clear stop with no base
    assert action["inputs"]["daedalus-source"]["default"] == "git+https://github.com/KEV-INZHU/Daedalus"


def test_install_docs_use_the_form_that_works_before_a_pypi_release():
    for rel in ("README.md", "docs/gate-workflow.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "pipx install git+https://github.com/KEV-INZHU/Daedalus" in text, rel
        for line in text.splitlines():
            if "pipx install daedalus" in line:
                assert "after the first PyPI release" in line or "PyPI release" in line, (rel, line)
    for rel in ("README.md", "docs/ci.md", "action.yml"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"Daedalus@v0\.2\.0\s*$", text, re.MULTILINE), rel
        assert "daedalus==0.2.0" not in text, rel


def test_there_is_one_version_and_pyproject_reads_it():
    import daedalus

    cfg = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in cfg and 'attr = "daedalus.__version__"' in cfg
    project = cfg.split("[project]")[1].split("\n[")[0]
    assert not re.search(r'^version\s*=', project, re.MULTILINE)
    assert re.fullmatch(r"\d+\.\d+\.\d+", daedalus.__version__)
    assert f"## {daedalus.__version__}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "daedalus", "--version"], capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, check=False)
    assert daedalus.__version__ in out.stdout + out.stderr


# ---------------------------------------------------------------------- the command set (C3)
LEGACY_COMMANDS = {
    "abandon", "act", "amend", "approve", "arbitrate", "attest", "audit", "blocker", "cancel", "ci", "deny",
    "dispute", "doctor", "factory", "finish", "hook", "init", "install", "log", "metrics", "off", "on",
    "plugins", "policy", "reconcile", "resolve", "restore", "review", "risk-exception", "run", "runs",
    "start", "status", "statusline", "verify",
}


def test_the_split_cli_keeps_every_pre_split_command_and_adds_demo():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")
    assert set(sub.choices) == LEGACY_COMMANDS | {"demo", "report"}


def test_importing_the_cli_does_not_import_the_demo_module():
    out = subprocess.run(
        [sys.executable, "-c", "import sys; import daedalus.cli as c; c.build_parser(); "
                               "print('daedalus.demo' in sys.modules)"],
        capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, check=False)
    assert out.stdout.strip() == "False", out.stderr


def test_demo_flags_are_defined_without_importing_the_demo():
    parser = cli.build_parser()
    ns = parser.parse_args(["demo", "--fast", "--keep"])
    assert ns.fast and ns.keep


def test_a_broken_import_inside_the_demo_is_not_reported_as_a_missing_build(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "daedalus" and a and a[2] and "demo" in a[2]:
            raise ModuleNotFoundError("No module named 'somedep'", name="somedep")
        return real(name, *a, **kw)

    sys.modules.pop("daedalus.demo", None)
    import daedalus

    monkeypatch.delattr(daedalus, "demo", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["demo", "--fast"])


# ------------------------------------------------------------------- auto-finish guards
def _ready_run(repo, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    (repo / "app.txt").write_text("ok\n", encoding="utf-8")
    assert cli.main(["-C", str(repo), "start", "make app ok"]) == 0


def test_verify_does_not_finish_inside_a_session_daedalus_launched(repo, capsys, monkeypatch):
    _ready_run(repo, monkeypatch)
    monkeypatch.setattr("daedalus.harness.claude_code.launched_by_daedalus", lambda: True)
    assert cli.main(["-C", str(repo), "verify"]) == 0
    out = capsys.readouterr().out
    assert "Disposition: READY" in out and "ACCEPTED" not in out


def test_verify_only_never_finishes(repo, capsys, monkeypatch):
    _ready_run(repo, monkeypatch)
    monkeypatch.setattr("daedalus.harness.claude_code.launched_by_daedalus", lambda: False)
    assert cli.main(["-C", str(repo), "verify", "--only", "tests"]) == 0
    out = capsys.readouterr().out
    assert "ACCEPTED" not in out


@pytest.mark.parametrize("value", ["false", "no", 0, "yes", None])
def test_auto_finish_must_be_a_real_boolean(tmp_path, value):
    root = make_repo(tmp_path / "bad", {"harness": {"auto_finish": value}})
    with pytest.raises(PolicyError, match="auto_finish"):
        load_config(root)


def test_auto_finish_booleans_are_accepted(tmp_path):
    for value in (True, False):
        root = make_repo(tmp_path / f"ok{value}", {"harness": {"auto_finish": value}})
        assert load_config(root).harness["auto_finish"] is value


# ---------------------------------------------------------------- detection edge cases
def test_a_make_variable_named_test_is_not_a_test_target(tmp_path):
    (tmp_path / "Makefile").write_text("test := fast\ntest ::= slow\nall:\n\techo hi\n")
    assert detect_checks(tmp_path) == {}
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    assert detect_checks(tmp_path)["tests"]["command"] == ["make", "test"]


@pytest.mark.parametrize("deps", [["left-pad"], "left-pad", 3])
def test_a_malformed_package_json_does_not_crash_detection(tmp_path, deps):
    import json

    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest run"}, "dependencies": deps, "devDependencies": deps}))
    (tmp_path / "tsconfig.json").write_text("{}")
    checks = detect_checks(tmp_path)
    assert "tests" in checks and "typecheck" not in checks


def test_an_unconfigured_run_does_not_pick_up_lint_and_typecheck(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n[tool.mypy]\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert 1\n")
    git(tmp_path, "init", "-q", "-b", "main")
    assert set(detect_checks(tmp_path)) == {"tests", "lint", "typecheck"}  # what `daedalus init` writes
    assert set(load_config(tmp_path).policy_raw["checks"]) == {"tests"}  # what a zero-config run uses


# ------------------------------------------------------------------- init housekeeping
def _fresh(tmp_path: Path) -> Path:
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    (root / "README.md").write_text("x\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def _tracked(root: Path, rev: str = "HEAD") -> set[str]:
    return set(subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", rev],
                              capture_output=True, text=True, check=True).stdout.split())


def test_init_force_backups_are_ignored_and_never_overwritten(tmp_path, capsys):
    root = _fresh(tmp_path)
    (root / ".daedalus.yml").write_text("enabled: true\n# first\n")
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--force"]) == 0
    (root / ".daedalus.yml").write_text("enabled: true\n# second\n")
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--force"]) == 0
    assert (root / ".daedalus.yml.bak").read_text().endswith("# first\n")
    assert (root / ".daedalus.yml.bak.1").read_text().endswith("# second\n")
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall"],
                            capture_output=True, text=True, check=True).stdout
    assert ".bak" not in status  # ignored, so it is not a changed path in later candidates


def test_init_commit_commits_only_the_harness_files_it_wrote(tmp_path, capsys, monkeypatch):
    root = _fresh(tmp_path)
    (root / ".claude" / "commands").mkdir(parents=True)
    (root / ".claude" / "commands" / "mine.md").write_text("my private draft\n")
    (root / ".gitignore").write_text("*.log\n")  # the user's own uncommitted edit
    assert cli.main(["-C", str(root), "init", "--harness", "claude-code", "--commit"]) == 0
    tracked = _tracked(root)
    assert ".daedalus.yml" in tracked and ".claude/settings.json" in tracked
    assert ".claude/commands/mine.md" not in tracked


def test_init_commit_leaves_the_users_own_gitignore_edits_alone(tmp_path, capsys):
    root = _fresh(tmp_path)
    (root / ".gitignore").write_text(".daedalus/\n*.log\n")  # already has init's entry plus the user's own
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--commit"]) == 0
    out = capsys.readouterr().out
    assert ".gitignore" not in _tracked(root) and ".daedalus.yml" in _tracked(root)
    assert "not committing it" in out
