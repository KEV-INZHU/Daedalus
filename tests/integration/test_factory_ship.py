"""The factory's optional ship step: push the integration branch, and open a pull request."""

from __future__ import annotations

import json
import subprocess

import pytest
from test_factory import add_orders, build, out

from conftest import HUMAN, git, make_repo
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.factory.line import Line, factory_config
from daedalus.factory.orders import FactoryError
from daedalus.orchestration.ariadne import Ariadne


def shipping_repo(tmp_path, ship):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    root = make_repo(tmp_path / "repo", {"factory": {"andon": 2, "ship": ship}})
    git(root, "remote", "add", "origin", str(remote))
    add_orders(root, {"fix": {"objective": "Make the app ok"}})
    return root, remote


def run_line(root, clock):
    ari = Ariadne(root, clock=clock)
    try:
        return Line(ari, SimulatedAdapter({"brunel": [build({"app.txt": "ok\n"})]}), actor=HUMAN).run()
    finally:
        ari.close()


def ledger_types(root):
    lines = (root / ".daedalus" / "factory" / "ledger.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines]


def test_shipped_orders_are_pushed(tmp_path, clock):
    root, remote = shipping_repo(tmp_path, {"push": "origin"})
    report = run_line(root, clock)
    assert report.shipped == ["fix"]
    assert out(remote, "rev-parse", "main") == out(root, "rev-parse", "HEAD")
    assert any(n.startswith("pushed main to origin") for n in report.notes)
    [pushed] = [e for e in ledger_types(root) if e["type"] == "line.pushed"]
    assert pushed["shipped"] == ["fix"] and pushed["pull_request"] is None


def test_without_gh_the_push_still_happens(tmp_path, clock, monkeypatch):
    from daedalus.factory import line as line_mod

    real = line_mod.shutil.which
    monkeypatch.setattr(
        line_mod.shutil, "which", lambda name, *a, **k: None if name == "gh" else real(name, *a, **k)
    )
    root, remote = shipping_repo(tmp_path, {"push": "origin", "pull_request": True, "base": "main"})
    report = run_line(root, clock)
    assert out(remote, "rev-parse", "main") == out(root, "rev-parse", "HEAD")
    assert any("the `gh` CLI is not installed" in n for n in report.notes)


def test_a_failed_push_is_reported_and_nothing_is_undone(tmp_path, clock):
    root, _ = shipping_repo(tmp_path, {"push": "nowhere"})
    report = run_line(root, clock)
    assert report.shipped == ["fix"] and (root / "app.txt").read_text() == "ok\n"
    assert any(n.startswith("push to nowhere failed") for n in report.notes)
    assert not [e for e in ledger_types(root) if e["type"] == "line.pushed"]


def test_nothing_is_pushed_by_default(tmp_path, clock):
    root, remote = shipping_repo(tmp_path, {})
    run_line(root, clock)
    assert (
        subprocess.run(["git", "-C", str(remote), "rev-parse", "main"], capture_output=True).returncode != 0
    )


@pytest.mark.parametrize(
    "ship",
    [
        {"pull_request": True},
        {"push": "origin", "pull_request": "yes"},
        {"push": "origin", "other": 1},
        "origin",
        {"push": "--upload-pack=x"},  # would be read as an option
        {"push": "origin", "base": "-x"},
        {"push": " "},
    ],
)
def test_bad_ship_settings_are_refused(tmp_path, ship):
    root = make_repo(tmp_path / "r", {"factory": {"ship": ship}})
    with pytest.raises(FactoryError):
        factory_config(root)


def test_a_hanging_push_is_bounded_and_reported(tmp_path, clock, monkeypatch):
    from daedalus.factory import line as line_mod

    real = line_mod.subprocess.run

    def hang(cmd, *a, **kw):
        if cmd[:1] == ["git"] and "push" in cmd:
            assert kw.get("stdin") is line_mod.subprocess.DEVNULL and kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
            assert kw.get("timeout") == line_mod.SHIP_TIMEOUT_S and "--" in cmd
            raise line_mod.subprocess.TimeoutExpired(cmd, kw["timeout"])
        return real(cmd, *a, **kw)

    monkeypatch.setattr(line_mod.subprocess, "run", hang)
    root, _ = shipping_repo(tmp_path, {"push": "origin"})
    report = run_line(root, clock)
    assert report.shipped == ["fix"]
    assert any("push to origin failed: timed out" in n for n in report.notes)
