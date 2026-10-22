"""`daedalus demo` plays against the real gate: the honest scene must be accepted and the
cheating scene refused."""

from __future__ import annotations

import io

from daedalus import demo
from daedalus.core.acceptance import Disposition


def test_honest_scene_is_accepted_with_a_receipt(tmp_path):
    buf = io.StringIO()
    res = demo.run_demo(demo.Out(fast=True, stream=buf), tmp_path / "p", cheat=False)
    assert res["scene2"].disposition is Disposition.ACCEPTED
    assert "cost $0.00" in res["receipt"] and "audit chain verified" in res["receipt"]
    assert res["run_id"] in buf.getvalue()


def test_cheating_scene_is_refused(tmp_path):
    buf = io.StringIO()
    res = demo.run_demo(demo.Out(fast=True, stream=buf), tmp_path / "p")
    d1 = res["scene1"]
    assert d1.check_states["tests"][0].value == "PASS"  # the cheat really does pass the tests
    assert d1.disposition is not Disposition.ACCEPTED
    assert any(r.code == "test_integrity" for r in d1.reasons)
    assert res["scene2"].disposition is Disposition.ACCEPTED
    assert "receipt" in res


def test_demo_ignores_the_users_git_config(tmp_path, monkeypatch):
    """A global commit.gpgsign / hooksPath must not break or hijack the demo."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /nonexistent\n[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("HOME", str(tmp_path))
    res = demo.run_demo(demo.Out(fast=True, stream=io.StringIO()), tmp_path / "p", cheat=False)
    assert res["scene2"].disposition is Disposition.ACCEPTED
    assert not marker.exists()
    import os
    assert os.environ["GIT_CONFIG_GLOBAL"] == str(gitconfig)  # restored afterwards


def test_a_git_failure_is_a_clear_error_not_a_traceback(tmp_path, monkeypatch):
    import pytest

    from daedalus.core.errors import DaedalusError

    monkeypatch.setenv("PATH", str(tmp_path))  # no git
    with pytest.raises(DaedalusError, match="needs git"):
        demo.build_project(tmp_path / "p")


def test_main_cleans_up_and_keep_prints_path(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    demo.main(["--fast", "--keep"])
    out = capsys.readouterr().out
    assert "\033[" not in out and "Kept: " in out and "daedalus init" in out
    from pathlib import Path
    kept = Path(out.split("Kept: ")[1].splitlines()[0])
    assert (kept / ".daedalus").is_dir()
    demo.remove_tree(kept.parent)
