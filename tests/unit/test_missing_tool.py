"""A check that fails because its tool is missing says so, instead of reading as a test failure."""

from __future__ import annotations

import sys

from conftest import HUMAN, make_repo
from daedalus.orchestration.ariadne import Ariadne
from daedalus.verification.check_runner import missing_tool


def test_a_missing_module_is_named(tmp_path, clock):
    cmd = [sys.executable, "-c", "import daedalus_no_such_module_xyz"]
    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": cmd, "timeout_s": 60}}})
    ari = Ariadne(root, clock=clock)
    try:
        rid = ari.start({"objective": "o"}, actor=HUMAN)
        ari.verify(rid)
        [reason] = [r for r in ari.evaluate(rid).reasons if r.code == "check:tests"]
        assert "looks like a missing tool" in reason.message
        assert "`daedalus_no_such_module_xyz` is not installed where the check ran" in reason.message
        assert "bootstrap" in reason.message
    finally:
        ari.close()


def test_an_ordinary_failure_gets_no_note():
    assert missing_tool(b"AssertionError: 1 != 2\n") is None
    assert missing_tool(b"") is None
    assert "pytest" in missing_tool(b"/usr/bin/python: No module named pytest\n")


def test_the_repositorys_own_modules_are_never_called_a_missing_tool(tmp_path):
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "helpers.py").write_text("", encoding="utf-8")
    assert missing_tool(b"No module named 'mypkg.sub'", tmp_path) is None
    assert missing_tool(b"No module named 'helpers'", tmp_path) is None
    assert missing_tool(b"No module named 'pytest'", tmp_path) is not None
