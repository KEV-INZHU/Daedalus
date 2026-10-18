"""The gate as an ambient mode: runs open themselves, reviews launch themselves,
accepted commits say so, and the committed hook config works on any machine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import HUMAN, fix, git, make_repo
from daedalus.adapters.base import AGENT_MARKER, ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.harness import claude_code, provenance
from daedalus.orchestration.ariadne import Ariadne


@pytest.fixture(autouse=True)
def not_a_launched_session(monkeypatch):
    monkeypatch.delenv(AGENT_MARKER, raising=False)


def payload(root: Path, **extra) -> dict:
    return {"cwd": str(root), "session_id": "s1", **extra}


def out(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


# ----------------------------------------------------------- runs open themselves
def test_first_edit_opens_a_run_named_after_the_prompt(ari, repo):
    claude_code.prompt_hook(payload(repo, prompt="make the app say ok"), ari=ari)
    assert ari.active_run_id() is None  # a prompt alone (a question) opens nothing
    res = claude_code.pre_edit_hook(payload(repo, tool_input={"file_path": str(repo / "app.txt")}), ari=ari)
    rid = ari.active_run_id()
    assert rid and rid in res["systemMessage"]
    assert ari.state(rid).contract.objective == "make the app say ok"
    assert "decision" not in res  # never blocks the edit
    assert "including commits" not in res["systemMessage"]
    # a second edit does not open another run
    assert claude_code.pre_edit_hook(payload(repo, tool_input={"file_path": "app.txt"}), ari=ari) == {}


def test_edits_outside_the_repository_or_to_daedalus_state_open_nothing(ari, repo, tmp_path):
    claude_code.prompt_hook(payload(repo, prompt="p"), ari=ari)
    claude_code.pre_edit_hook(payload(repo, tool_input={"file_path": str(tmp_path / "elsewhere.txt")}), ari=ari)
    claude_code.pre_edit_hook(payload(repo, tool_input={"file_path": str(repo / ".daedalus" / "x")}), ari=ari)
    assert ari.active_run_id() is None


def test_work_committed_mid_session_is_still_in_the_candidate(ari, repo):
    claude_code.prompt_hook(payload(repo, prompt="fix the app"), ari=ari)
    base = out(repo, "rev-parse", "HEAD")
    fix(repo)
    git(repo, "commit", "-qam", "agent committed before stopping")  # via a shell, no edit tool
    res = claude_code.stop_hook(payload(repo), ari=ari)
    rid = ari.store.run_ids()[-1]
    state = ari.state(rid)
    assert state.contract.base_revision == base  # based on where the prompt was given
    assert state.disposition is not None and state.disposition.value == "ACCEPTED"
    assert "ACCEPTED" in res["systemMessage"]


def test_shell_only_changes_are_gated_at_stop(ari, repo):
    claude_code.prompt_hook(payload(repo, prompt="break it"), ari=ari)
    (repo / "app.txt").write_text("still bad\n")
    res = claude_code.stop_hook(payload(repo), ari=ari)
    assert res["decision"] == "block" and "tests" in res["reason"]


def test_no_changes_means_no_run(ari, repo):
    claude_code.prompt_hook(payload(repo, prompt="what does lib.py do?"), ari=ari)
    assert claude_code.stop_hook(payload(repo), ari=ari) == {}
    assert ari.store.run_ids() == []


def test_auto_start_can_be_turned_off(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"harness": {"auto_start": False}})
    a = Ariadne(root, clock=clock)
    claude_code.prompt_hook(payload(root, prompt="p"), ari=a)
    claude_code.pre_edit_hook(payload(root, tool_input={"file_path": "app.txt"}), ari=a)
    assert a.active_run_id() is None
    a.close()


# -------------------------------------------------------- reviews launch themselves
def test_stop_launches_the_required_review_itself(tmp_path, clock, monkeypatch):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    reviewer = SimulatedAdapter({"aristotle": [ScriptedStep(structured={"summary": "fine", "findings": []})]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: reviewer)
    claude_code.prompt_hook(payload(root, prompt="fix the app"), ari=a)
    claude_code.pre_edit_hook(payload(root, tool_input={"file_path": "app.txt"}), ari=a)
    fix(root)
    res = claude_code.stop_hook(payload(root), ari=a)
    assert "ACCEPTED" in res["systemMessage"]
    assert [r.role for r in reviewer.requests] == ["aristotle"] and reviewer.requests[0].read_only
    a.close()


def test_review_findings_go_back_to_the_agent(tmp_path, clock, monkeypatch):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    finding = {"title": "no test", "evidence": "app.txt", "severity": "BLOCKER", "required_change": "add a test"}
    reviewer = SimulatedAdapter({"aristotle": [ScriptedStep(structured={"summary": "s", "findings": [finding]})]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: reviewer)
    claude_code.prompt_hook(payload(root, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(payload(root, tool_input={"file_path": "app.txt"}), ari=a)
    fix(root)
    res = claude_code.stop_hook(payload(root), ari=a)
    assert res["decision"] == "block" and "add a test" in res["reason"]
    a.close()


def test_without_a_launching_adapter_the_review_is_put_to_the_user_not_the_agent(tmp_path, clock, monkeypatch):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: None)  # inline: nothing can launch
    claude_code.prompt_hook(payload(root, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(payload(root, tool_input={"file_path": "app.txt"}), ari=a)
    fix(root)
    res = claude_code.stop_hook(payload(root), ari=a)
    assert "decision" not in res and "adapter: claude-code" in res["systemMessage"]
    assert "Daedalus launches it" not in res["systemMessage"]  # one consistent line per pending review
    a.close()


def test_verify_never_launches_reviews_inside_a_launched_session(tmp_path, clock, monkeypatch, capsys):
    from daedalus import cli

    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root)  # the CLI below runs on the real clock
    a.start({"objective": "o"}, actor=HUMAN)
    a.close()
    fix(root)
    launched = []
    monkeypatch.setattr(claude_code, "settle", lambda *x, **k: launched.append(1))
    monkeypatch.setenv(AGENT_MARKER, "1")
    assert cli.main(["-C", str(root), "verify"]) == 0  # checks pass; only the review launch is skipped
    assert launched == []


def test_verify_launches_the_required_review_itself(tmp_path, clock, monkeypatch):
    from daedalus import cli

    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root)  # the CLI below runs on the real clock
    a.start({"objective": "o"}, actor=HUMAN)
    a.close()
    fix(root)
    reviewer = SimulatedAdapter({"aristotle": [ScriptedStep(structured={"summary": "fine", "findings": []})]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: reviewer)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["-C", str(root), "verify"]) == 0
    assert [r.role for r in reviewer.requests] == ["aristotle"]
    assert cli.main(["-C", str(root), "finish"]) == 0  # ACCEPTED: no one typed `daedalus review`


def test_no_review_is_launched_while_checks_fail(tmp_path, clock, monkeypatch):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    reviewer = SimulatedAdapter()
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: reviewer)
    claude_code.prompt_hook(payload(root, prompt="fix"), ari=a)
    (root / "app.txt").write_text("nope\n")
    res = claude_code.stop_hook(payload(root), ari=a)
    assert res["decision"] == "block" and reviewer.requests == []
    a.close()


# ------------------------------------------------------------ portable hooks
def test_installed_hooks_are_portable_and_idempotent(repo):
    claude_code.install(repo)
    claude_code.install(repo)
    doc = json.loads((repo / ".claude" / "settings.json").read_text())
    for event in ("Stop", "SessionStart", "UserPromptSubmit", "PreToolUse"):
        cmds = [h["command"] for g in doc["hooks"][event] for h in g["hooks"]]
        assert len(cmds) == 1 and "daedalus hook" in cmds[0] and "python" not in cmds[0]
    assert doc["hooks"]["PreToolUse"][0]["matcher"] == claude_code.EDIT_TOOLS
    assert "daedalus statusline" in doc["statusLine"]["command"]


def test_hooks_are_silent_without_daedalus_and_session_start_says_how_to_install(repo, tmp_path):
    import shutil

    sh = shutil.which("sh") or "/bin/sh"
    env = {"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)}  # no daedalus anywhere
    cmd = claude_code._hook_command("stop")
    r = subprocess.run([sh, "-c", cmd], capture_output=True, text=True, env=env, cwd=repo)
    assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([sh, "-c", claude_code._hook_command("session-start")], capture_output=True, text=True,
                       env=env, cwd=repo)
    assert r.returncode == 0 and "not installed" in json.loads(r.stdout)["systemMessage"]


def test_upgrading_replaces_old_interpreter_pinned_hooks(repo):
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '"/usr/bin/python3" -m daedalus hook stop'}]}]}}
    settings.write_text(json.dumps(old))
    claude_code.install(repo)
    cmds = [h["command"] for g in json.loads(settings.read_text())["hooks"]["Stop"] for h in g["hooks"]]
    assert len(cmds) == 1 and "-m daedalus" not in cmds[0]


# ------------------------------------------------------------- commit trailers
def accepted_run(ari, repo) -> str:
    rid = ari.start({"objective": "fix"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    assert ari.finish(rid).disposition.value == "ACCEPTED"
    return rid


def commit_with_hook(repo: Path, msg: str = "fix the app") -> str:
    msg_file = repo / ".git" / "COMMIT_EDITMSG_TEST"
    msg_file.write_text(msg + "\n")
    import os

    cwd = os.getcwd()
    os.chdir(repo)
    try:
        provenance.commit_msg_hook([str(msg_file)])
    finally:
        os.chdir(cwd)
    return msg_file.read_text()


def test_committing_exactly_the_accepted_candidate_adds_trailers(ari, repo):
    rid = accepted_run(ari, repo)
    git(repo, "add", "-A")
    text = commit_with_hook(repo)
    assert f"Daedalus-Run: {rid}" in text and "Daedalus-Candidate: " in text


def test_partial_or_extended_commits_get_no_trailers(ari, repo):
    accepted_run(ari, repo)
    assert "Daedalus-Run" not in commit_with_hook(repo)  # nothing staged: the commit is not the candidate
    git(repo, "add", "-A")
    (repo / "src" / "lib.py").write_text("x = 2\n")  # an unreviewed edit after acceptance
    git(repo, "add", "-A")
    assert "Daedalus-Run" not in commit_with_hook(repo)


def test_existing_trailers_are_left_alone(ari, repo):
    accepted_run(ari, repo)
    git(repo, "add", "-A")
    text = commit_with_hook(repo, "msg\n\nDaedalus-Run: factory123")
    assert text.count("Daedalus-Run:") == 1


def test_git_hook_install_respects_someone_elses_hook(repo):
    assert "prepare-commit-msg" in provenance.install_git_hook(repo)
    assert "already installed" in provenance.install_git_hook(repo, only_if_absent=True)
    hook = provenance.hook_path(repo)
    hook.write_text("#!/bin/sh\necho mine\n")
    from daedalus.core.errors import DaedalusError

    with pytest.raises(DaedalusError, match="not Daedalus's"):
        provenance.install_git_hook(repo)
    assert "mine" in hook.read_text()


@pytest.fixture
def daedalus_on_path(tmp_path, monkeypatch):
    """A `daedalus` command for the git hook to find, whatever is installed."""
    import os
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "daedalus"
    src = Path(__file__).resolve().parents[2] / "src"
    shim.write_text(f'#!/bin/sh\nPYTHONPATH="{src}" exec "{sys.executable}" -m daedalus "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_real_git_commit_runs_the_hook_end_to_end(ari, repo, daedalus_on_path):
    provenance.install_git_hook(repo)
    rid = accepted_run(ari, repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix the app")
    assert f"Daedalus-Run: {rid}" in out(repo, "log", "-1", "--format=%B")


def test_a_pathspec_commit_of_part_of_the_candidate_gets_no_trailers(ari, repo, daedalus_on_path):
    provenance.install_git_hook(repo)
    rid = ari.start({"objective": "two files"}, actor=HUMAN)
    fix(repo)
    (repo / "src" / "lib.py").write_text("x = 2\n")
    ari.verify(rid)
    assert ari.finish(rid).disposition.value == "ACCEPTED"
    git(repo, "commit", "-qm", "only part of it", "app.txt")  # git uses a temporary index here
    assert "Daedalus-Run" not in out(repo, "log", "-1", "--format=%B")


def test_automatic_hook_install_works_in_a_linked_worktree(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    msg = provenance.install_git_hook(wt, only_if_absent=True)
    assert "prepare-commit-msg ->" in msg and (repo / ".git" / "hooks" / "prepare-commit-msg").exists()


def test_automatic_hook_install_skips_a_symlinked_shared_hooks_dir(repo, tmp_path):
    import shutil

    shared = tmp_path / "template-hooks"
    shared.mkdir()
    hooks = repo / ".git" / "hooks"
    shutil.rmtree(hooks)
    hooks.symlink_to(shared)
    msg = provenance.install_git_hook(repo, only_if_absent=True)
    assert "outside this repository" in msg and not (shared / "prepare-commit-msg").exists()


@pytest.mark.parametrize("where", ["in-tree", "shared"])
def test_automatic_hook_install_only_writes_into_this_repositorys_git_dir(repo, tmp_path, where):
    hooks = repo / ".husky" if where == "in-tree" else tmp_path / "shared-githooks"
    git(repo, "config", "core.hooksPath", str(hooks))
    msg = provenance.install_git_hook(repo, only_if_absent=True)
    assert "core.hooksPath" in msg and not hooks.exists()


# ------------------------------------------------------------------ status line
def test_statusline_reports_the_run_without_running_anything(ari, repo):
    assert claude_code.statusline({"workspace": {"current_dir": str(repo)}}) == "Daedalus on · no open run"
    ari.start({"objective": "o"}, actor=HUMAN)
    line = claude_code.statusline({"workspace": {"current_dir": str(repo)}})
    assert line.startswith("Daedalus · run ") and "tests not run" in line
    fix(repo)
    ari.verify(ari.active_run_id())
    assert "tests PASS" in claude_code.statusline({"workspace": {"current_dir": str(repo)}})
    (repo / "app.txt").write_text("ok, edited\n")
    ari.record_candidate(ari.active_run_id(), actor=HUMAN, authored=False)
    assert "tests stale" in claude_code.statusline({"workspace": {"current_dir": str(repo)}})
    assert claude_code.statusline({"cwd": "/nonexistent/place"}) == ""


# ------------------------------------------------------ billed reviews, refused up front
class BilledReviewer(SimulatedAdapter):
    def expected_cost_basis(self):
        return "cash"


def test_a_billed_review_the_cash_budget_cannot_cover_is_never_launched(tmp_path, clock, monkeypatch):
    from daedalus.core.errors import BudgetExhausted
    from daedalus.orchestration.lifecycle import launch_review

    root = make_repo(tmp_path / "r", {"mode": "review"})  # default max_cash: 0
    a = Ariadne(root, clock=clock)
    reviewer = BilledReviewer({"aristotle": [ScriptedStep(structured={"summary": "s", "findings": []}, cost=0.6)]})
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(root)
    a.verify(rid)
    with pytest.raises(BudgetExhausted, match="max_cash"):
        launch_review(a, rid, "aristotle", reviewer)
    assert reviewer.requests == [] and a.state(rid).cash_used == 0
    a.close()


def test_stop_tells_the_user_rather_than_the_agent_when_a_review_cannot_be_afforded(tmp_path, clock, monkeypatch):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: BilledReviewer())
    claude_code.prompt_hook(payload(root, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(payload(root, tool_input={"file_path": "app.txt"}), ari=a)
    fix(root)
    res = claude_code.stop_hook(payload(root), ari=a)
    assert "decision" not in res  # the agent cannot fix a budget; it is not bounced
    assert "Needs you" in res["systemMessage"] and "max_cash" in res["systemMessage"]
    a.close()


def test_the_dearest_billed_session_so_far_is_the_estimate_for_the_next(tmp_path, clock):
    from daedalus.core.errors import BudgetExhausted
    from daedalus.orchestration.lifecycle import refuse_unaffordable

    root = make_repo(tmp_path / "r", {"policy": {"default_budget": {
        "max_attempts": 20, "max_wall_seconds": 86400, "max_cost": 50.0, "max_workers": 1, "max_cash": 1.0}}})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    state = a.state(rid)
    refuse_unaffordable(state, BilledReviewer(), "a review")  # nothing billed yet: allowed
    state.sessions.append({"basis": "cash", "cost": 0.6})
    state.cash_used = 0.6
    with pytest.raises(BudgetExhausted, match="cannot cover"):
        refuse_unaffordable(state, BilledReviewer(), "a review")  # 0.6 + 0.6 > 1.0
    a.close()


def test_a_billed_review_within_budget_is_launched(tmp_path, clock, monkeypatch):
    root = make_repo(tmp_path / "r", {"mode": "review", "policy": {"default_budget": {
        "max_attempts": 20, "max_wall_seconds": 86400, "max_cost": 50.0, "max_workers": 1, "max_cash": 5.0}}})
    a = Ariadne(root, clock=clock)
    reviewer = BilledReviewer({"aristotle": [ScriptedStep(structured={"summary": "s", "findings": []}, cost=0.6)]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: reviewer)
    claude_code.prompt_hook(payload(root, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(payload(root, tool_input={"file_path": "app.txt"}), ari=a)
    fix(root)
    assert "ACCEPTED" in claude_code.stop_hook(payload(root), ari=a)["systemMessage"]
    a.close()


# ------------------------------------------------- committed hooks are reviewed config
def test_changing_committed_gate_hooks_is_critical(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}")
    d = ari.evaluate(rid)
    assert d.risk.effective_tier == "critical" and "gate_hooks" in d.risk.explain()
    (repo / ".claude" / "settings.json").unlink()
    (repo / ".claude" / "settings.local.json").write_text("{}")  # local state stays out of the candidate
    assert ari.evaluate(rid).risk.effective_tier == "low"


def test_bootstrap_installs_daedalus_where_it_is_missing(tmp_path):
    import shutil

    root = make_repo(tmp_path / "r", {"harness": {"bootstrap": "touch bootstrapped"}})
    claude_code.install(root)
    cmd = json.loads((root / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    tools = tmp_path / "tools"  # only what the command needs: no daedalus, wherever it is installed
    tools.mkdir()
    for name in ("sh", "touch"):
        (tools / name).symlink_to(shutil.which(name))
    r = subprocess.run([str(tools / "sh"), "-c", cmd], capture_output=True, text=True, cwd=root,
                       env={"PATH": str(tools), "HOME": str(tmp_path)})
    assert r.returncode == 0 and (root / "bootstrapped").exists()


def test_contract_file_criteria_keep_their_meaning(repo, monkeypatch, tmp_path):
    from daedalus import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    contract = tmp_path / "c.yml"
    contract.write_text("objective: o\nacceptance_criteria: [owner signed off on the security model]\n")
    assert cli.main(["-C", str(repo), "start", "--contract", str(contract)]) == 0
    a = Ariadne(repo)
    rid = a.active_run_id()
    (c1,) = a.state(rid).contract.acceptance_criteria
    assert c1.checks == ()  # a bare string in a contract file still needs a person's attestation
    a.cancel(rid, actor=HUMAN)
    a.close()
    assert cli.main(["-C", str(repo), "start", "o", "-c", "app says ok", "--attest", "looks right"]) == 0
    a = Ariadne(repo)
    assert [c.checks for c in a.state(a.active_run_id()).contract.acceptance_criteria] == [("tests",), ()]
    a.close()


def test_empty_amend_and_remainder_commits_get_no_trailers(ari, repo, daedalus_on_path):
    provenance.install_git_hook(repo)
    rid = ari.start({"objective": "two files"}, actor=HUMAN)
    fix(repo)
    (repo / "src" / "lib.py").write_text("x = 2\n")
    ari.verify(rid)
    assert ari.finish(rid).disposition.value == "ACCEPTED"
    git(repo, "add", "app.txt")
    git(repo, "commit", "-qm", "first half")  # part of the candidate
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the remainder")  # the rest: not the whole candidate on its own
    assert "Daedalus-Run" not in out(repo, "log", "-2", "--format=%B")
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    git(repo, "commit", "-q", "--amend", "--allow-empty", "-m", "reworded")
    assert "Daedalus-Run" not in out(repo, "log", "-1", "--format=%B")


def test_another_sessions_prompt_never_sets_the_base(ari, repo):
    claude_code.prompt_hook(payload(repo, prompt="theirs", session_id="other"), ari=ari)
    base = out(repo, "rev-parse", "HEAD")
    (repo / "src" / "lib.py").write_text("x = 5\n")
    git(repo, "commit", "-qam", "someone else's commit")
    claude_code.pre_edit_hook(payload(repo, session_id="mine", tool_input={"file_path": "app.txt"}), ari=ari)
    state = ari.state(ari.active_run_id())
    assert state.contract.base_revision == out(repo, "rev-parse", "HEAD") != base
    assert state.contract.objective == "Changes made in a Claude Code session"
