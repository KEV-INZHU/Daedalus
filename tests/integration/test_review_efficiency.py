"""One review round should be enough: a thorough first pass, delta re-reviews, one follow-up."""

from __future__ import annotations

import pytest

from conftest import HUMAN, fix, make_repo
from daedalus.adapters.base import AgentResult, ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.acceptance import review_satisfied
from daedalus.core.errors import ContractError
from daedalus.core.metrics import run_metrics
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import launch_review, run_task
from daedalus.repository import snapshot

DONE = {"status": "done", "summary": "did it"}
CLEAN = {"summary": "fine", "findings": []}
MINOR = {"title": "empty input unhandled", "evidence": "lib.py:1", "severity": "MINOR",
         "required_change": "handle empty input"}


@pytest.fixture
def review_repo(tmp_path):
    return make_repo(tmp_path / "r", {"mode": "review"})


def reviewer(*bodies) -> SimulatedAdapter:
    return SimulatedAdapter({"aristotle": [ScriptedStep(structured=b) for b in bodies]})


def test_first_review_is_full_with_the_sweep_and_the_next_is_a_delta(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(review_repo)
    (review_repo / "src" / "lib.py").write_text("x = 1\ny = 2\n")
    a.verify(rid)
    rev = reviewer({"summary": "s", "findings": [MINOR]}, {"summary": "fixed", "findings": [], "resolves": ["F1"]})
    launch_review(a, rid, "aristotle", rev)
    first = rev.requests[0].prompt
    assert "only full review this candidate gets" in first and "Inputs at the edges" in first
    (review_repo / "src" / "lib.py").write_text("x = 1\ny = 2\nz = 3\n")  # the fix
    launch_review(a, rid, "aristotle", rev)
    second = rev.requests[1].prompt
    assert "review what changed since your last review" in second
    assert "+z = 3" in second and "app.txt" not in second.split("Changed since your last review")[1]
    state = a.state(rid)
    assert [r.scope for r in state.reviews] == ["full", "delta"]
    assert state.reviews[1].delta_from == state.reviews[0].candidate_id
    assert state.findings["F1"].resolved_by is not None
    assert review_satisfied(state, "aristotle", state.current_candidate_id, True)
    a.close()


def test_a_tampered_snapshot_means_a_full_review(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(review_repo)
    a.verify(rid)
    rev = reviewer(CLEAN, CLEAN)
    launch_review(a, rid, "aristotle", rev)
    snap = snapshot.snapshot_dir(a.state_dir / "runs" / rid, a.state(rid).reviews[0].candidate_id)
    (snap / "files" / "app.txt").write_text("not what was reviewed\n")
    (review_repo / "app.txt").write_text("ok again\n")
    launch_review(a, rid, "aristotle", rev)
    assert "Full re-review" in rev.requests[1].prompt
    assert a.state(rid).reviews[1].scope == "full"
    a.close()


def test_rereview_full_policy_disables_deltas(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review", "policy": {"rereview": "full"}})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(root)
    rev = reviewer(CLEAN, CLEAN)
    launch_review(a, rid, "aristotle", rev)
    (root / "app.txt").write_text("ok 2\n")
    launch_review(a, rid, "aristotle", rev)
    assert [r.scope for r in a.state(rid).reviews] == ["full", "full"]
    a.close()


def test_a_delta_counts_only_through_a_chain_to_a_full_review(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    with pytest.raises(ContractError, match="must extend"):
        ari.submit_review(rid, reviewer="agent:r", perspective="aristotle", source="launched",
                          scope="delta", delta_from="nothing-reviewed")
    c1 = ari.capture(ari.state(rid)).candidate_id
    ari.submit_review(rid, reviewer="agent:r", perspective="aristotle", source="launched", candidate_id=c1)
    (repo / "app.txt").write_text("ok 2\n")
    c2 = ari.record_candidate(rid, actor="agent:builder").candidate_id
    ari.submit_review(rid, reviewer="agent:r2", perspective="aristotle", source="launched",
                      candidate_id=c2, scope="delta", delta_from=c1)
    state = ari.state(rid)
    assert review_satisfied(state, "aristotle", c2, True)
    assert not review_satisfied(state, "socrates", c2, False)
    # A delta whose base has no satisfying review of the *current* contract version does not count.
    ari.amend(rid, {"objective": "changed"}, actor=HUMAN, rationale="r")
    assert not review_satisfied(ari.state(rid), "aristotle", c2, False)


def test_a_delta_chain_cycle_never_satisfies(ari, repo):
    from dataclasses import replace

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    state = ari.state(rid)
    from daedalus.core.run import Review

    loop = [
        Review("R1", "aristotle", "agent:r", "a", state.contract.contract_version, "launched", (), "", (), (), "delta", "b"),
        Review("R2", "aristotle", "agent:r", "b", state.contract.contract_version, "launched", (), "", (), (), "delta", "a"),
    ]
    assert not review_satisfied(replace(state, reviews=loop), "aristotle", "a", True)


def builder_then_fix(repo):
    calls = []

    def first(req):
        calls.append(req.prompt)
        fix(repo)
        return AgentResult("COMPLETED", structured=DONE)

    def second(req):
        calls.append(req.prompt)
        (repo / "src" / "lib.py").write_text("x = 1  # handles empty input\n")
        return AgentResult("COMPLETED", structured=DONE)

    return calls, SimulatedAdapter({"brunel": [first, second]})


def test_one_follow_up_round_fixes_minor_findings_and_is_delta_reviewed(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    calls, adapter = builder_then_fix(review_repo)
    adapter.script["aristotle"] = [
        ScriptedStep(structured={"summary": "s", "findings": [MINOR]}),
        ScriptedStep(structured={"summary": "fixed", "findings": [], "resolves": ["F1"]}),
    ]
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=3)
    assert report.decision.disposition.value == "ACCEPTED" and report.rounds == 2
    assert "one follow-up round" in calls[1] and "handle empty input" in calls[1]
    m = run_metrics(a.store.events(report.run_id))
    assert m.followup_rounds == 1 and m.delta_reviews == 1 and m.late_findings == 0
    a.close()


def test_follow_up_never_blocks_and_happens_once(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    calls, adapter = builder_then_fix(review_repo)
    still = {"title": "another nit", "evidence": "lib.py:1", "severity": "MINOR", "required_change": "rename"}
    adapter.script["aristotle"] = [
        ScriptedStep(structured={"summary": "s", "findings": [MINOR]}),
        ScriptedStep(structured={"summary": "new nit", "findings": [still]}),  # F1 not resolved, F2 new
    ]
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=5)
    assert report.decision.disposition.value == "ACCEPTED" and report.rounds == 2  # no second follow-up
    assert len(calls) == 2
    a.close()


def test_no_follow_up_on_the_last_round(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    calls, adapter = builder_then_fix(review_repo)
    adapter.script["aristotle"] = [ScriptedStep(structured={"summary": "s", "findings": [MINOR]})]
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=1)
    assert report.decision.disposition.value == "ACCEPTED" and len(calls) == 1
    a.close()


def test_a_follow_up_that_ends_worse_restores_the_accepted_candidate(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    _, adapter = builder_then_fix(review_repo)
    adapter.script["aristotle"] = [ScriptedStep(structured={"summary": "s", "findings": [MINOR]})]  # re-review fails
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=2)
    assert report.decision.disposition.value == "ACCEPTED"
    assert any("restored" in n for n in report.notes)
    assert (review_repo / "src" / "lib.py").read_text() == "x = 1\n"  # the follow-up's edit is gone
    a.close()


def test_a_follow_up_that_breaks_the_checks_is_reverted(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)

    def ok(req):
        fix(review_repo)
        return AgentResult("COMPLETED", structured=DONE)

    def breaks(req):
        (review_repo / "app.txt").write_text("nope\n")
        return AgentResult("COMPLETED", structured=DONE)

    adapter = SimulatedAdapter({"brunel": [ok, breaks],
                                "aristotle": [ScriptedStep(structured={"summary": "s", "findings": [MINOR]})]})
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=2)
    assert report.decision.disposition.value == "ACCEPTED"
    assert (review_repo / "app.txt").read_text() == "ok\n"
    a.close()


def test_advisory_findings_get_no_follow_up_by_default(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    calls, adapter = builder_then_fix(review_repo)
    advisory = dict(MINOR, severity="ADVISORY")
    adapter.script["aristotle"] = [ScriptedStep(structured={"summary": "s", "findings": [advisory]})]
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=3)
    assert report.rounds == 1 and len(calls) == 1
    a.close()


def test_stop_hook_offers_the_follow_up_once(review_repo, clock, monkeypatch):
    from daedalus.adapters.base import AGENT_MARKER
    from daedalus.harness import claude_code

    monkeypatch.delenv(AGENT_MARKER, raising=False)
    a = Ariadne(review_repo, clock=clock)
    rev = reviewer({"summary": "s", "findings": [MINOR]}, {"summary": "ok", "findings": [], "resolves": ["F1"]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: rev)
    payload = {"cwd": str(review_repo), "session_id": "s"}
    claude_code.prompt_hook(dict(payload, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(dict(payload, tool_input={"file_path": "app.txt"}), ari=a)
    fix(review_repo)
    res = claude_code.stop_hook(payload, ari=a)
    assert res["decision"] == "block" and "handle empty input" in res["reason"]
    (review_repo / "src" / "lib.py").write_text("x = 1  # empty input handled\n")
    res = claude_code.stop_hook(payload, ari=a)
    assert "ACCEPTED" in res["systemMessage"]
    state = a.state(a.store.run_ids()[-1])
    assert [r.scope for r in state.reviews] == ["full", "delta"]
    a.close()


def test_late_findings_count_only_full_re_reviews(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    c1 = ari.capture(ari.state(rid)).candidate_id
    ari.submit_review(rid, reviewer="agent:r", perspective="aristotle", source="launched", candidate_id=c1,
                      findings=[MINOR])
    (repo / "app.txt").write_text("ok 2\n")
    c2 = ari.record_candidate(rid, actor="agent:builder").candidate_id
    ari.submit_review(rid, reviewer="agent:r2", perspective="aristotle", source="launched", candidate_id=c2,
                      findings=[dict(MINOR, title="found late")])
    assert run_metrics(ari.store.events(rid)).late_findings == 1


def test_snapshot_fallbacks(review_repo, tmp_path):
    from daedalus.repository.candidate import capture, git_head

    base = git_head(review_repo)
    run_dir = tmp_path / "run"
    fix(review_repo)
    c1 = capture(review_repo, base)
    assert snapshot.save(review_repo, run_dir, c1.candidate_id, c1.manifest)
    (review_repo / "app.txt").write_text("ok 2\n")
    c2 = capture(review_repo, base)
    diff, touched = snapshot.inter_diff(review_repo, run_dir, c1.candidate_id, c1.manifest, c2.manifest)
    assert touched == ["app.txt"] and "+ok 2" in diff
    with pytest.raises(snapshot.NoDelta, match="base revision"):
        snapshot.inter_diff(review_repo, run_dir, c1.candidate_id, dict(c1.manifest, base_revision="x"), c2.manifest)
    with pytest.raises(snapshot.NoDelta, match="no snapshot"):
        snapshot.inter_diff(review_repo, run_dir, "never-saved", c1.manifest, c2.manifest)
    (review_repo / "app.txt").write_text("edited after capture\n")  # live side no longer matches c2
    with pytest.raises(snapshot.NoDelta, match="cannot prove"):
        snapshot.inter_diff(review_repo, run_dir, c1.candidate_id, c1.manifest, c2.manifest)
    assert not snapshot.save(review_repo, run_dir, c2.candidate_id, c2.manifest)  # changed since capture
    assert not (snapshot.snapshot_dir(run_dir, c2.candidate_id) / ".complete").exists()


def test_snapshots_are_pruned_when_the_run_finishes(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(review_repo)
    a.verify(rid)
    launch_review(a, rid, "aristotle", reviewer(CLEAN))
    assert (a.state_dir / "runs" / rid / "snapshots").exists()
    assert a.finish(rid).disposition.value == "ACCEPTED"
    assert not (a.state_dir / "runs" / rid / "snapshots").exists()
    a.close()


def test_review_policy_keys_are_validated(tmp_path):
    from daedalus.core.config import load_config
    from daedalus.core.errors import PolicyError

    for bad in ({"rereview": "sometimes"}, {"followup_severities": ["NIT"]}, {"followup_severities": "MINOR"}):
        root = make_repo(tmp_path / str(len(list(tmp_path.iterdir()))), {"policy": bad})
        with pytest.raises(PolicyError):
            load_config(root)


BLOCKER = {"title": "follow-up broke parsing", "evidence": "lib.py:1", "severity": "BLOCKER",
           "required_change": "revert the parser change"}


def test_a_blocker_raised_against_a_reverted_follow_up_is_moot(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    calls, adapter = builder_then_fix(review_repo)
    adapter.script["aristotle"] = [
        ScriptedStep(structured={"summary": "s", "findings": [MINOR]}),
        ScriptedStep(structured={"summary": "worse", "findings": [BLOCKER]}),  # the delta review of the follow-up
    ]
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=4)
    assert report.decision.disposition.value == "ACCEPTED" and len(calls) == 2  # exactly one follow-up round
    state = a.state(report.run_id)
    blocker = next(f for f in state.findings.values() if f.severity.value == "BLOCKER")
    assert blocker.resolved_by is None and blocker.candidate_id in state.reverted_candidates
    a.close()


def test_a_full_review_finding_on_reverted_code_is_never_moot(ari, repo):
    from daedalus.core.acceptance import is_moot

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    c1 = ari.capture(ari.state(rid)).candidate_id
    ari.submit_review(rid, reviewer="agent:r", perspective="aristotle", source="launched", candidate_id=c1)
    ari.request_followup(rid, [], c1)
    (repo / "app.txt").write_text("ok 2\n")
    c2 = ari.record_candidate(rid, actor="agent:builder").candidate_id
    ari.submit_review(rid, reviewer="agent:r2", perspective="aristotle", source="launched", candidate_id=c2,
                      findings=[BLOCKER])  # a FULL review
    (repo / "app.txt").write_text("ok\n")
    ari.record_revert(rid, c1)
    state = ari.state(rid)
    f = next(iter(state.findings.values()))
    assert c2 in state.reverted_candidates and not is_moot(state, f, c1)
    assert "finding:" in " ".join(r.code for r in ari.evaluate(rid).reasons)


def test_restore_command_undoes_a_follow_up_at_the_stop_hook(review_repo, clock, monkeypatch):
    from daedalus import cli
    from daedalus.adapters.base import AGENT_MARKER
    from daedalus.harness import claude_code

    monkeypatch.delenv(AGENT_MARKER, raising=False)
    a = Ariadne(review_repo)  # real clock: the CLI below uses it too
    rev = reviewer({"summary": "s", "findings": [MINOR]}, {"summary": "worse", "findings": [BLOCKER]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: rev)
    payload = {"cwd": str(review_repo), "session_id": "s"}
    claude_code.prompt_hook(dict(payload, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(dict(payload, tool_input={"file_path": "app.txt"}), ari=a)
    fix(review_repo)
    assert claude_code.stop_hook(payload, ari=a)["decision"] == "block"  # the one follow-up pass
    (review_repo / "src" / "lib.py").write_text("x = 'made it worse'\n")
    res = claude_code.stop_hook(payload, ari=a)
    assert res["decision"] == "block" and "daedalus restore" in res["reason"]
    a.close()
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["-C", str(review_repo), "restore"]) == 0
    assert (review_repo / "src" / "lib.py").read_text() == "x = 1\n"
    a = Ariadne(review_repo)
    assert "ACCEPTED" in claude_code.stop_hook(payload, ari=a)["systemMessage"]
    a.close()


def test_restore_without_a_follow_up_is_refused(ari, repo):
    from daedalus.core.errors import DaedalusError
    from daedalus.orchestration.lifecycle import restore_followup

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    with pytest.raises(DaedalusError, match="no follow-up"):
        restore_followup(ari, rid)


def test_a_follow_up_builder_that_reports_blocked_does_not_cost_the_acceptance(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)

    def ok(req):
        fix(review_repo)
        return AgentResult("COMPLETED", structured=DONE)

    def gives_up(req):
        (review_repo / "src" / "lib.py").write_text("half a fix\n")
        return AgentResult("COMPLETED", structured={"status": "blocked", "summary": "s", "blocker": "unclear"})

    adapter = SimulatedAdapter({"brunel": [ok, gives_up],
                                "aristotle": [ScriptedStep(structured={"summary": "s", "findings": [MINOR]})]})
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=3)
    assert report.decision.disposition.value == "ACCEPTED"
    assert not a.state(report.run_id).blockers and (review_repo / "src" / "lib.py").read_text() == "x = 1\n"
    a.close()


def test_the_stop_hook_undoes_a_regressed_follow_up_before_giving_up(tmp_path, monkeypatch):
    from daedalus.adapters.base import AGENT_MARKER
    from daedalus.harness import claude_code

    monkeypatch.delenv(AGENT_MARKER, raising=False)
    root = make_repo(tmp_path / "r", {"mode": "review", "harness": {"max_stop_blocks": 1}})
    a = Ariadne(root)
    rev = reviewer({"summary": "s", "findings": [MINOR]})
    monkeypatch.setattr(claude_code, "_review_adapter", lambda _root: rev)
    payload = {"cwd": str(root), "session_id": "s"}
    claude_code.prompt_hook(dict(payload, prompt="fix"), ari=a)
    claude_code.pre_edit_hook(dict(payload, tool_input={"file_path": "app.txt"}), ari=a)
    fix(root)
    assert claude_code.stop_hook(payload, ari=a)["decision"] == "block"  # the follow-up pass
    (root / "app.txt").write_text("nope\n")  # now the checks fail (the check wants "ok")
    claude_code.stop_hook(payload, ari=a)  # blocked once (the cap)
    res = claude_code.stop_hook(payload, ari=a)  # cap reached: the gate undoes the pass itself
    assert "ACCEPTED" in res["systemMessage"] and (root / "app.txt").read_text() == "ok\n"
    a.close()


def test_a_failed_restore_puts_the_tree_back(review_repo, tmp_path):
    from daedalus.repository.candidate import capture, git_head

    base = git_head(review_repo)
    run_dir = tmp_path / "run"
    fix(review_repo)
    c1 = capture(review_repo, base)
    assert snapshot.save(review_repo, run_dir, c1.candidate_id, c1.manifest)
    (review_repo / "app.txt").write_text("later\n")
    (review_repo / "zzz").mkdir()
    (review_repo / "zzz" / "new.txt").write_text("added later\n")
    c2 = capture(review_repo, base)
    manifest = dict(c1.manifest, files={**c1.manifest["files"], "zzz": c1.manifest["files"]["app.txt"]})
    snap = snapshot.snapshot_dir(run_dir, c1.candidate_id) / "files"
    (snap / "zzz").write_text("ok\n")  # restoring "zzz" (a directory now) as a file will fail mid-way
    assert not snapshot.restore(review_repo, run_dir, c1.candidate_id, manifest, c2.manifest)
    assert (review_repo / "app.txt").read_text() == "later\n"  # rolled back
    assert (review_repo / "zzz" / "new.txt").read_text() == "added later\n"


def test_undone_follow_up_work_is_kept_as_a_patch_and_its_resolutions_reopen(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    calls, adapter = builder_then_fix(review_repo)
    adapter.script["aristotle"] = [
        ScriptedStep(structured={"summary": "s", "findings": [MINOR]}),
        ScriptedStep(structured={"summary": "fixed, but", "findings": [BLOCKER], "resolves": ["F1"]}),
    ]
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, max_rounds=3)
    assert report.decision.disposition.value == "ACCEPTED"
    kept = list((a.state_dir / "runs" / report.run_id).glob("followup-undone-*.diff"))
    assert len(kept) == 1 and "handles empty input" in kept[0].read_text()  # the work is recoverable
    f1 = a.state(report.run_id).findings["F1"]
    assert f1.resolved_by is None  # its fix was undone, so the finding is open again
    assert any(str(kept[0].name) in n for n in report.notes)
    a.close()


def test_restore_keeps_the_executable_bit(review_repo, tmp_path):
    import os

    from conftest import git
    from daedalus.repository.candidate import capture, git_head

    tool = review_repo / "run.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    git(review_repo, "add", "run.sh")
    git(review_repo, "commit", "-qm", "a script")
    base = git_head(review_repo)
    run_dir = tmp_path / "run"
    (review_repo / "bin.sh").write_text("#!/bin/sh\necho hi\n")
    (review_repo / "bin.sh").chmod(0o755)
    c1 = capture(review_repo, base)
    assert snapshot.save(review_repo, run_dir, c1.candidate_id, c1.manifest)
    (review_repo / "bin.sh").write_text("changed\n")
    tool.write_text("changed too\n")
    c2 = capture(review_repo, base)
    assert snapshot.restore(review_repo, run_dir, c1.candidate_id, c1.manifest, c2.manifest)
    assert os.access(review_repo / "bin.sh", os.X_OK) and os.access(tool, os.X_OK)
    assert capture(review_repo, base).candidate_id == c1.candidate_id


def test_a_failed_restore_command_exits_non_zero(review_repo, monkeypatch):
    from daedalus import cli
    from daedalus.repository import snapshot as snap

    monkeypatch.setenv("CLAUDECODE", "1")
    a = Ariadne(review_repo)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(review_repo)
    cand = a.capture(a.state(rid))
    snap.save(review_repo, a.state_dir / "runs" / rid, cand.candidate_id, cand.manifest)
    a.request_followup(rid, [], cand.candidate_id)
    (review_repo / "app.txt").write_text("ok later\n")
    a.close()
    monkeypatch.setattr(snap, "restore", lambda *x, **k: False)
    assert cli.main(["-C", str(review_repo), "restore"]) == 2
    assert (review_repo / "app.txt").read_text() == "ok later\n"
