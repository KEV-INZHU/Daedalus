"""Escaped defects: a later `Fixes-Run:` commit marks an accepted run as having let a defect through."""

from __future__ import annotations

import json

from conftest import git
from daedalus.repository.escapes import escaped_runs

RUN = "a1b2c3d4e5f6"
OTHER = "a1b2c3d4ffff"


def commit(root, msg, path="f.txt", body="x"):
    (root / path).write_text(body + msg, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git_out(root, "rev-parse", "HEAD")


def git_out(root, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_a_later_fix_marks_the_run_escaped(repo):
    commit(repo, f"accepted work\n\nDaedalus-Run: {RUN}")
    fix = commit(repo, f"fix the regression\n\nFixes-Run: {RUN[:8]}")
    assert escaped_runs(repo, [RUN]) == {RUN: [fix]}


def test_a_fix_made_before_the_run_shipped_does_not_count(repo):
    commit(repo, f"premature\n\nFixes-Run: {RUN}")
    commit(repo, f"accepted work\n\nDaedalus-Run: {RUN}")
    assert escaped_runs(repo, [RUN]) == {}


def test_short_unknown_and_ambiguous_ids_are_never_guessed(repo):
    commit(repo, f"accepted\n\nDaedalus-Run: {RUN}")
    commit(repo, "a\n\nFixes-Run: a1b2c3")  # under 8 characters
    commit(repo, "b\n\nFixes-Run: ffffffffffff")  # not a run here
    commit(repo, "c\n\nFixes-Run: a1b2c3d4")  # prefix of both runs
    assert escaped_runs(repo, [RUN, OTHER]) == {}
    assert escaped_runs(repo, []) == {}


def test_a_run_without_provenance_still_counts(repo):
    fix = commit(repo, f"fix\n\nFixes-Run: {RUN}")
    assert escaped_runs(repo, [RUN]) == {RUN: [fix]}


def test_metrics_report_the_escape_rate(repo, ari, monkeypatch, capsys):
    from conftest import HUMAN, fix
    from daedalus.cli import main

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    assert ari.finish(rid).disposition.value == "ACCEPTED"
    commit(repo, f"later fix\n\nFixes-Run: {rid}")
    assert main(["-C", str(repo), "metrics", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["aggregate"]["escaped"] == 1 and out["aggregate"]["escape_rate"] == 1.0
    assert out["runs"][0]["escaped_by"]
    assert main(["-C", str(repo), "metrics"]) == 0
    assert "escaped defects 1 of 1 accepted · escape rate 100%" in capsys.readouterr().out
