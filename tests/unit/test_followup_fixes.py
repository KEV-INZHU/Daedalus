"""Follow-up round: always-true assertions, move detection, runner files, init and demo details."""

from __future__ import annotations

import hashlib
import io
import os
import shlex
import shutil
import subprocess
import types
from pathlib import Path

import pytest
import yaml

from conftest import fake_executable, git
from daedalus import cli, demo
from daedalus.cli import lifecycle
from daedalus.adapters.command import BILLING_ENV_VARS
from daedalus.core.config import load_config, render_config
from daedalus.core.paths import MAKEFILE_NAMES
from daedalus.core.policy import CheckDefinition
from daedalus.verification import test_integrity as ti


def commit(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())  # exact bytes: Windows text mode would add CR
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def change(root: Path, files: dict[str, str | None]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for rel, text in files.items():
        p = root / rel
        if text is None:
            p.unlink()
            out[rel] = None
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode())  # exact bytes: Windows text mode would add CR
            out[rel] = hashlib.sha256(text.encode()).hexdigest()
    return out


def scan(root: Path, base: str, files: dict[str, str | None], **kw) -> ti.IntegrityReport:
    return ti.analyze(root, base, change(root, files), **kw)


def kinds(report: ti.IntegrityReport) -> set[str]:
    return {s.kind for s in report.signals}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


REAL = "def test_a():\n    assert f() == 3\n"


def weakened(repo: Path, body: str, base: str = REAL) -> ti.IntegrityReport:
    sha = commit(repo, {"tests/test_a.py": base})
    return scan(repo, sha, {"tests/test_a.py": body})


# ------------------------------------------------------------------ F1: always-true assertions
@pytest.mark.parametrize("assertion", [
    "assert f() == 3 or True",
    "assert True or f() == 3",
    "assert True and 1",
    "assert (f() == 3, 'message')",
    "assert [f()]",
    "assert {'a': f()}",
    "assert {f()}",
])
def test_always_true_assert_forms_are_trivial(repo, assertion):
    r = weakened(repo, f"def test_a():\n    {assertion}\n")
    assert kinds(r) & {ti.TRIVIAL_ASSERT, ti.LOST_ASSERTIONS}, r.signals


def test_assert_true_helper_with_an_always_true_argument_is_trivial(repo):
    base = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(f())\n"
    cand = base.replace("self.assertTrue(f())", "self.assertTrue(f() or True)")
    r = weakened(repo, cand, base)
    assert r.flagged and "assertTrue(f() or True)" in r.summary(), r.signals


def test_a_real_and_or_assert_is_not_trivial(repo):
    r = weakened(repo, "def test_a():\n    assert f() == 3 or g() == 3\n")
    assert not r.flagged, r.signals


@pytest.mark.parametrize("guard", ["if True:\n        return", "if 1:\n        raise SystemExit"])
def test_an_if_true_early_exit_neutralizes_the_assertions_after_it(repo, guard):
    r = weakened(repo, f"def test_a():\n    {guard}\n    assert f() == 3\n")
    assert ti.NEUTRALIZED in kinds(r), r.signals


def test_a_conditional_early_exit_is_not_neutralizing(repo):
    r = weakened(repo, "def test_a():\n    if g():\n        return\n    assert f() == 3\n")
    assert not r.flagged, r.signals


# --------------------------------------------------------------- F12: broader swallowing forms
@pytest.mark.parametrize("handler", [
    "except Exception:\n        pass",
    "except builtins.Exception:\n        pass",
    "except (ValueError, Exception):\n        pass",
    "except BaseException:\n        pass",
])
def test_exception_handlers_that_swallow_assertions_are_neutralizing(repo, handler):
    r = weakened(repo, f"def test_a():\n    try:\n        assert f() == 3\n    {handler}\n")
    assert ti.NEUTRALIZED in kinds(r), r.signals


@pytest.mark.parametrize("ctx", [
    "contextlib.suppress(AssertionError)", "suppress(Exception)", "pytest.raises(AssertionError)",
])
def test_with_blocks_that_absorb_assertion_failures_are_neutralizing(repo, ctx):
    r = weakened(repo, f"def test_a():\n    with {ctx}:\n        assert f() == 3\n")
    assert ti.NEUTRALIZED in kinds(r), r.signals


@pytest.mark.parametrize("handler", ["except Exception:\n        raise", "except KeyError:\n        pass"])
def test_an_except_that_reraises_or_catches_something_narrow_is_fine(repo, handler):
    r = weakened(repo, f"def test_a():\n    try:\n        assert f() == 3\n    {handler}\n")
    assert ti.NEUTRALIZED not in kinds(r)


# --------------------------------------------------------------------- F2/F10: move detection
def test_a_deleted_test_is_not_hidden_by_a_same_named_test_elsewhere(repo):
    base = commit(repo, {"tests/test_a.py": REAL, "tests/test_b.py": REAL})  # both already have test_a
    r = scan(repo, base, {"tests/test_a.py": None, "tests/test_b.py": REAL + "\n\ndef test_other():\n    assert 1 + 1 == 2\n"})
    assert ti.REMOVED_FILE in kinds(r), r.signals


def test_a_real_move_into_a_file_where_the_name_is_new_is_still_not_flagged(repo):
    base = commit(repo, {"tests/test_a.py": REAL, "tests/test_b.py": "def test_b():\n    assert 1 == 1\n"})
    r = scan(repo, base, {"tests/test_a.py": None, "tests/test_b.py": "def test_b():\n    assert 1 == 1\n\n\n" + REAL})
    assert not r.flagged, r.signals


def test_assertion_counts_are_kept_per_file(repo):
    two = "def test_a():\n    assert f() == 3\n    assert g() == 4\n"
    base = commit(repo, {"tests/test_a.py": two, "tests/test_z.py": "def test_z():\n    assert 1 == 1\n"})
    # test_a moves to a new file, and loses an assertion there; a later file must not overwrite the count
    r = scan(repo, base, {"tests/test_a.py": None, "tests/test_m.py": REAL,
                          "tests/test_z.py": "def test_z():\n    assert 1 == 1\n\n\ndef test_a2():\n    assert 2 == 2\n"})
    assert not any(s.kind == ti.REMOVED_FILE for s in r.signals), r.signals


# --------------------------------------------------------------- F3: files a mandatory check runs
def _check(command, inputs=()):
    return CheckDefinition("tests", tuple(command), cache_inputs=tuple(inputs))


def test_check_runner_specs_names_files_in_argv_and_cache_inputs():
    specs = ti.check_runner_specs([_check(["bash", "scripts/run.sh", "--cfg=conf/x.ini", "-q", "/abs", "../up"],
                                          ["uv.lock"])])
    assert {"scripts/run.sh", "conf/x.ini", "uv.lock", "bash"} <= set(specs)
    assert "-q" not in specs and "/abs" not in specs and "../up" not in specs


def test_editing_a_script_a_mandatory_check_runs_is_a_signal(repo):
    base = commit(repo, {"scripts/run.sh": "pytest -q\n", "other.sh": "echo 1\n"})
    specs = ti.check_runner_specs([_check(["bash", "scripts/run.sh"])])
    r = scan(repo, base, {"scripts/run.sh": "true\n"}, runner_files=specs)
    assert kinds(r) == {ti.RUNNER_CONFIG} and r.paths == ("scripts/run.sh",)
    assert not scan(repo, base, {"other.sh": "echo 2\n"}, runner_files=specs).flagged
    assert not scan(repo, base, {"scripts/run.sh": "pytest -q\n"}, runner_files=specs).flagged


def test_deleting_such_a_script_is_a_signal(repo):
    base = commit(repo, {"run_tests.py": "import pytest\n"})
    specs = ti.check_runner_specs([_check(["python", "run_tests.py"])])
    assert kinds(scan(repo, base, {"run_tests.py": None}, runner_files=specs)) == {ti.RUNNER_CONFIG}


def test_whole_file_changes_in_cache_inputs_are_signals(repo):
    base = commit(repo, {"locks/dev.lock": "a==1\n"})
    specs = ti.check_runner_specs([_check(["pytest"], ["locks/*.lock"])])
    assert kinds(scan(repo, base, {"locks/dev.lock": "a==2\n"}, runner_files=specs)) == {ti.RUNNER_CONFIG}


def test_the_ariadne_scan_uses_the_mandatory_checks_files(tmp_path):
    from conftest import HUMAN, make_repo
    from daedalus.orchestration.ariadne import Ariadne

    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": ["sh", "run_checks.sh"], "timeout_s": 60}}})
    (root / "run_checks.sh").write_text("exit 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "script")
    a = Ariadne(root)
    try:
        rid = a.start({"objective": "x"}, actor=HUMAN)
        (root / "run_checks.sh").write_text("exit 0\n", encoding="utf-8")
        report = a.test_integrity(a.state(rid), a.capture(a.state(rid)))
        assert report is not None and ti.RUNNER_CONFIG in kinds(report)
    finally:
        a.close()


# ------------------------------------------------------------ F14/F31: more runner config forms
@pytest.mark.parametrize("name", [*MAKEFILE_NAMES, "noxfile.py"])
def test_every_makefile_spelling_and_noxfile_are_runner_config(repo, name):
    assert any(ti.match(name, g) for g in ti.RUNNER_CONFIG_GLOBS)
    text = "test:\n\tpytest -q\n" if name != "noxfile.py" else "def tests(s):\n    s.run('pytest')\n"
    base = commit(repo, {name: text})
    new = "test:\n\ttrue\n" if name != "noxfile.py" else "def tests(s):\n    pass\n"
    assert kinds(scan(repo, base, {name: new})) == {ti.RUNNER_CONFIG}


def test_a_bare_tool_table_with_pytest_keys_is_runner_config(repo):
    base = commit(repo, {"pyproject.toml": "[project]\nname = 'x'\n\n[tool]\npytest.ini_options.addopts = '-q'\n"})
    r = scan(repo, base, {"pyproject.toml": "[project]\nname = 'x'\n\n[tool]\npytest.ini_options.addopts = '--co'\n"})
    assert kinds(r) == {ti.RUNNER_CONFIG}
    other = commit(repo, {"pyproject.toml": "[tool]\nblack.line-length = 100\n"})
    assert not scan(repo, other, {"pyproject.toml": "[tool]\nblack.line-length = 90\n"}).flagged


# ---------------------------------------------------------------------------- F13: skip moved
def test_a_skip_marker_moved_to_another_test_is_flagged(repo):
    base = ("import pytest\n\n\n@pytest.mark.skip\ndef test_a():\n    assert f() == 3\n\n\n"
            "def test_b():\n    assert g() == 4\n")
    cand = ("import pytest\n\n\ndef test_a():\n    assert f() == 3\n\n\n@pytest.mark.skip\n"
            "def test_b():\n    assert g() == 4\n")
    r = weakened(repo, cand, base)
    assert ti.SKIP_ADDED in kinds(r) and "test_b" in r.summary(), r.signals


# -------------------------------------------------------------------------- F15: approx
def test_approx_is_not_counted_as_an_assertion(repo):
    base = "import pytest\n\n\ndef test_a():\n    assert f() == pytest.approx(3)\n"
    cand = "def test_a():\n    assert f() == 3\n"
    assert not weakened(repo, cand, base).flagged
    assert ti.parse_python(base).asserts == 1
    assert ti.parse_python("import pytest\n\n\ndef test_a():\n    with pytest.raises(ValueError):\n        f()\n").asserts == 1


# ------------------------------------------------------------------ F16: JS regex literals
def test_a_regex_literal_with_slashes_does_not_hide_a_skip(repo):
    js = "test('a', () => {\n  const re = /\\//;\n  expect(f()).toBe(3);\n});\nit.skip('b', () => {});\n"
    parsed = ti.parse_text("js", js)
    assert parsed.skips == 1 and parsed.asserts == 1
    cls = "test('a', () => { const r = /[/]/; });\nit.skip('b', () => {});\n"
    assert ti.parse_text("js", cls).skips == 1


def test_comments_and_division_still_behave(repo):
    js = "// it.skip('x')\nconst a = 6 / 2; // it.only('y')\n/* it.skip('z') */\ntest('a', () => { expect(a / 2).toBe(1); });\n"
    p = ti.parse_text("js", js)
    assert p.skips == 0 and p.focus == 0 and p.asserts == 1


# -------------------------------------------------------------------------- F7: timeouts
def test_render_config_keeps_non_default_timeouts_and_round_trips():
    text = render_config({
        "tests": {"command": ["python", "-m", "pytest", "-q"], "timeout_s": 1800},
        "fast": {"command": ["true"], "timeout_s": 600},
        "lint": {"command": ["ruff", "check", "."], "timeout_s": 900, "mandatory": False},
    })
    assert "tests: {command: " in text and "timeout_s: 1800" in text
    assert "  fast: true" in text
    raw = yaml.safe_load(text)["checks"]
    assert raw["tests"]["timeout_s"] == 1800 and raw["lint"] == {"command": ["ruff", "check", "."], "timeout_s": 900, "mandatory": False}


def test_init_writes_the_detected_timeout(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    assert cli.main(["-C", str(tmp_path), "init", "--no-hooks"]) == 0
    assert load_config(tmp_path).policy.checks["tests"].timeout_s == 1800


# ------------------------------------------------------------------------ F8: hermetic git
def test_the_demo_unsets_git_redirection_variables_and_restores_them(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / "index"))
    with demo.hermetic_git():
        for var in demo._GIT_UNSET:
            assert var not in os.environ
    assert os.environ["GIT_DIR"] == str(other) and os.environ["GIT_INDEX_FILE"] == str(other / "index")
    res = demo.run_demo(demo.Out(fast=True, stream=io.StringIO()), tmp_path / "p", cheat=False)
    assert res["scene2"].disposition.value == "ACCEPTED"
    assert not (other / "HEAD").exists() and os.environ["GIT_WORK_TREE"] == str(other)


# ------------------------------------------------------------------ F4 / F25 / F28: init
def _fresh(tmp_path):
    root = tmp_path / "r"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "d@example.com")
    git(root, "config", "user.name", "Dev")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def test_init_commit_leaves_out_a_gitignore_with_the_users_own_edits(tmp_path, capsys):
    root = _fresh(tmp_path)
    (root / ".gitignore").write_text("*.pyc\nsecret-notes.txt\n", encoding="utf-8")  # the user's own edit
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--commit"]) == 0
    out = capsys.readouterr().out
    assert ".gitignore has uncommitted changes besides init's entries" in out
    committed = subprocess.run(["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.split()
    assert ".daedalus.yml" in committed and ".gitignore" not in committed


def test_init_commit_still_commits_a_gitignore_that_holds_only_init_entries(tmp_path, capsys):
    root = _fresh(tmp_path)
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--commit"]) == 0
    committed = subprocess.run(["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.split()
    assert ".gitignore" in committed


def test_init_commit_command_from_a_subdirectory_uses_git_dash_c(tmp_path, capsys, monkeypatch):
    root = _fresh(tmp_path)
    sub = root / "tests"
    monkeypatch.chdir(sub)
    assert cli.main(["init", "--no-hooks"]) == 0
    out = capsys.readouterr().out
    assert any(f"git -C {lifecycle._quote(str(r))} add" in out for r in (root, root.resolve())), out
    monkeypatch.chdir(root)
    capsys.readouterr()
    assert cli.main(["init", "--no-hooks", "--force"]) == 0
    assert "  git add" in capsys.readouterr().out


def _fake_claude(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    fake_executable(d, "claude", "")
    monkeypatch.setenv("PATH", os.pathsep.join([str(d), str(Path(shutil.which("git")).parent)]))
    for var in BILLING_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_commit_command_quoting_survives_spaces_backslashes_and_ampersands(monkeypatch, name):
    monkeypatch.setattr(lifecycle, "os", types.SimpleNamespace(name=name))  # pathlib keeps the real os
    root = r"C:\Users\A B\R&D"
    q = lifecycle._quote(root)
    if name == "nt":
        assert q == '"C:\\Users\\A B\\R&D"'  # one double-quoted argument, backslashes kept
        assert lifecycle._quote('say "hi"') == '"say \\"hi\\""'
    else:
        assert shlex.split(q) == [root]
    cmd = lifecycle._commit_command([".daedalus.yml"], Path(root))
    assert cmd.startswith(f"git -C {q} add ") and cmd.count(q) == 2


def test_init_writes_the_claude_code_adapter_when_the_binary_is_on_path(tmp_path, monkeypatch, capsys):
    root = _fresh(tmp_path / "x")
    _fake_claude(tmp_path, monkeypatch)
    assert cli.main(["-C", str(root), "init"]) == 0
    assert yaml.safe_load((root / ".daedalus.yml").read_text())["adapter"] == "claude-code"


def test_init_does_not_pick_an_adapter_whose_reviews_would_be_billed_in_cash(tmp_path, monkeypatch):
    root = _fresh(tmp_path / "x")
    _fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv(BILLING_ENV_VARS[0], "sk-test")
    assert cli.main(["-C", str(root), "init"]) == 0
    assert "adapter" not in yaml.safe_load((root / ".daedalus.yml").read_text())


def test_init_keeps_inline_without_the_binary_and_doctor_warns(tmp_path, monkeypatch, capsys):
    root = _fresh(tmp_path / "x")
    monkeypatch.setenv("PATH", str(Path(shutil.which("git")).parent))
    assert cli.main(["-C", str(root), "init", "--commit"]) == 0
    assert "adapter" not in yaml.safe_load((root / ".daedalus.yml").read_text())
    from daedalus.doctor import diagnose, render

    lines = render(diagnose(root))
    warn = [ln for ln in lines if ln.startswith("WARN")]
    assert warn and "risk-exception" in warn[0]
    assert not any(ln.startswith("FAIL") for ln in lines if "launching adapter" in ln)


# ------------------------------------------------------------------------- F30: CI paths
def test_ci_treats_non_ascii_test_paths_literally(tmp_path):
    from conftest import make_repo
    from daedalus.ci import run_ci

    root = make_repo(tmp_path / "r")
    name = "tests/test_café.py"
    (root / "tests").mkdir()
    (root / name).write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add")
    git(root, "checkout", "-q", "-b", "feature")
    (root / name).write_text("def test_a():\n    assert 1 == 1\n    assert 2 == 2\n", encoding="utf-8")
    git(root, "commit", "-qam", "edit")
    rep = run_ci(root, "main")
    assert name in rep.changed
    assert not any("deleted" in s for s in rep.test_integrity), rep.test_integrity
