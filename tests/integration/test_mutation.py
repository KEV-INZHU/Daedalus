"""Test strength: mutate the changed lines and report the breaks the tests do not catch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import HUMAN, git, make_repo
from daedalus.verification.mutation import changed_lines, mutants_of, run

LIB = "def is_even(n):\n    return n % 2 == 0\n"
CHANGE = LIB + "\n\ndef clamp(x, hi):\n    if x > hi and hi >= 0:\n        return hi\n    return x + 0\n"
WEAK = "from lib import clamp\nassert clamp(5, 3) == 3\n"
STRONG = (
    WEAK
    + "assert clamp(2, 3) == 2\nassert clamp(5, -1) == 5\nassert clamp(3, 3) == 3\nassert clamp(-1, 3) == -1\n"
)


def project(tmp_path: Path, tests: str) -> Path:
    root = make_repo(tmp_path / "p")
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "lib")
    (root / "lib.py").write_text(CHANGE, encoding="utf-8")
    (root / "test_lib.py").write_text(tests, encoding="utf-8")
    return root


TEST_CMD = [sys.executable, "test_lib.py"]


def test_only_changed_non_test_lines_are_candidates(tmp_path):
    root = project(tmp_path, WEAK)
    lines = changed_lines(root, "HEAD")
    assert set(lines) == {"lib.py"}  # test_lib.py (untracked) is a test file: never mutated
    assert min(lines["lib.py"]) >= 3  # is_even did not change


def test_mutants_are_single_and_described():
    found = {m.what for m, _ in mutants_of(CHANGE, "lib.py", set(range(4, 9)))}
    assert {"Gt -> LtE", "GtE -> Lt", "and -> or", "Add -> Sub", "return x -> return None"} <= found
    assert all(src != CHANGE for _, src in mutants_of(CHANGE, "lib.py", set(range(4, 9))))


def test_weak_tests_let_mutants_survive_and_the_tree_is_untouched(tmp_path):
    root = project(tmp_path, WEAK)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    result = run(root, TEST_CMD)
    assert result.mutants and result.survived and result.score < 1
    assert all(m.path == "lib.py" for m in result.survived)
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert after == before


def test_strong_tests_catch_more(tmp_path):
    weak = run(project(tmp_path / "w", WEAK), TEST_CMD)
    strong = run(project(tmp_path / "s", STRONG), TEST_CMD)
    assert strong.score > weak.score


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    from daedalus.cli import main

    root = project(tmp_path, WEAK)
    monkeypatch.chdir(root)
    assert main(["mutate", "--", *TEST_CMD]) == 1
    out = capsys.readouterr().out
    assert "mutation score" in out and "survived: lib.py:" in out
    assert main(["mutate", "--min-score", "0", "--", *TEST_CMD]) == 0
    assert main(["mutate"]) == 2  # no test command
    (root / "test_lib.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["mutate", "--", *TEST_CMD]) == 2 and "fail without any mutation" in capsys.readouterr().out
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "all in")
    assert main(["mutate", "--", *TEST_CMD]) == 0  # nothing changed since HEAD


@pytest.mark.parametrize("cap", [1, 3])
def test_the_number_of_mutants_is_capped(tmp_path, cap):
    assert len(run(project(tmp_path, WEAK), TEST_CMD, max_mutants=cap).mutants) == cap


def test_checks_are_told_the_run_base(tmp_path, clock):
    from daedalus.orchestration.ariadne import Ariadne

    out = tmp_path / "seen-base"
    probe = [
        sys.executable,
        "-c",
        f"import os; open(r'{out}', 'w').write(os.environ.get('DAEDALUS_BASE_REVISION', ''))",
    ]
    root = make_repo(tmp_path / "r", {"checks": {"probe": {"command": probe, "timeout_s": 60}}})
    ari = Ariadne(root, clock=clock)
    try:
        rid = ari.start({"objective": "o"}, actor=HUMAN)
        ari.verify(rid)
        assert out.read_text() == ari.state(rid).contract.base_revision
    finally:
        ari.close()
