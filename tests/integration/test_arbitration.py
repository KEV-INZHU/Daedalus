"""Council arbitration (run 09eb250a50f2, spec §1, §10, §15)."""

from __future__ import annotations

import pytest

from conftest import BUILDER, HUMAN, fix, make_repo
from daedalus.adapters.base import ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core import run as ev
from daedalus.core.errors import AuthorizationError, ContractError
from daedalus.core.state_machine import Disposition
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import arbitrate, run_task

FINDING = {"title": "needs a test", "evidence": "app.txt has none", "severity": "BLOCKER", "required_change": "add one"}
OTHER = {"title": "naming", "evidence": "x is vague", "severity": "MINOR", "required_change": "rename"}
UPHOLD = {"decision": "uphold", "rationale": "the contract requires tests", "reversal_condition": "a test exists"}
OVERRULE = {"decision": "overrule", "rationale": "out of scope for this contract", "reversal_condition": "scope grows"}


def reviewed_run(a, root, *, extra=()):
    rid = a.start({"objective": "o"}, actor=HUMAN, mode="review")
    fix(root)
    a.verify(rid)
    a.submit_review(rid, reviewer="agent:rev", perspective="aristotle", findings=[FINDING], source="launched")
    for f in extra:
        a.submit_review(rid, reviewer="agent:rev2", perspective="mozi", findings=[f], source="launched")
    return rid


def test_dispute_needs_an_open_finding_and_a_reason(ari, repo):
    rid = reviewed_run(ari, repo)
    with pytest.raises(ContractError):
        ari.dispute(rid, "F1", actor=BUILDER, reason="  ")
    with pytest.raises(ContractError):
        ari.dispute(rid, "F9", actor=BUILDER, reason="no such finding")
    ari.dispute(rid, "F1", actor=BUILDER, reason="tests exist elsewhere")
    assert ari.state(rid).findings["F1"].disputes[0]["reason"] == "tests exist elsewhere"
    assert any(e.type == ev.FINDING_DISPUTED for e in ari.store.events(rid))


def test_arbitration_is_read_only_and_sees_every_perspective(ari, repo):
    rid = reviewed_run(ari, repo, extra=[OTHER])
    ari.dispute(rid, "F1", actor=BUILDER, reason="tests exist elsewhere")
    adapter = SimulatedAdapter({"plato": [ScriptedStep(structured=UPHOLD)]})
    assert arbitrate(ari, rid, "F1", adapter) == "uphold"
    req = adapter.requests[-1]
    assert req.role == "plato" and req.read_only
    assert "needs a test" in req.prompt and "naming" in req.prompt and "tests exist elsewhere" in req.prompt
    f = ari.state(rid).findings["F1"]
    assert f.ruling["decision"] == "uphold" and f.resolved_by is None
    assert "upheld" in next(r.message for r in ari.evaluate(rid).reasons if r.code == "finding:F1")


@pytest.mark.parametrize(
    "ruling",
    [None, {"decision": "maybe", "rationale": "x"}, {"decision": "overrule", "rationale": ""}, {"decision": "uphold"}],
)
def test_malformed_ruling_is_recorded_as_failed_and_resolves_nothing(ari, repo, ruling):
    rid = reviewed_run(ari, repo)
    ari.dispute(rid, "F1", actor=BUILDER, reason="r")
    adapter = SimulatedAdapter({"plato": [ScriptedStep(structured=ruling)]})
    assert arbitrate(ari, rid, "F1", adapter) is None
    state = ari.state(rid)
    assert state.failed_arbitrations and state.findings["F1"].ruling is None
    assert state.findings["F1"].resolved_by is None


def test_arbiter_that_edits_the_tree_is_discarded(ari, repo):
    rid = reviewed_run(ari, repo)
    ari.dispute(rid, "F1", actor=BUILDER, reason="r")
    adapter = SimulatedAdapter({"plato": [ScriptedStep(edits={"app.txt": "ok edited by plato\n"}, structured=OVERRULE)]})
    assert arbitrate(ari, rid, "F1", adapter) is None
    assert ari.state(rid).failed_arbitrations


def test_overrule_is_advisory_by_default(ari, repo):
    rid = reviewed_run(ari, repo)
    ari.dispute(rid, "F1", actor=BUILDER, reason="out of scope")
    arbitrate(ari, rid, "F1", SimulatedAdapter({"plato": [ScriptedStep(structured=OVERRULE)]}))
    d = ari.finish(rid)
    assert d.disposition is Disposition.BLOCKED
    r = next(r for r in d.reasons if r.code == "finding:F1")
    assert r.fixable_by == "human" and "overrule" in r.message and "daedalus resolve F1" in r.message
    ari.resolve_finding(rid, "F1", actor=HUMAN, note="agree with Plato")
    assert ari.finish(rid).disposition is Disposition.ACCEPTED


def test_overrule_resolves_when_policy_enables_it(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"policy": {"arbitration_resolves_findings": True}})
    a = Ariadne(root, clock=clock)
    rid = reviewed_run(a, root)
    a.dispute(rid, "F1", actor=BUILDER, reason="out of scope")
    arbitrate(a, rid, "F1", SimulatedAdapter({"plato": [ScriptedStep(structured=OVERRULE)]}))
    f = a.state(rid).findings["F1"]
    assert f.resolved_by.startswith("agent:plato:") and "overruled by arbitration" in f.resolution
    assert a.finish(rid).disposition is Disposition.ACCEPTED
    a.close()


def test_arbitration_never_overrides_checks(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"policy": {"arbitration_resolves_findings": True}})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN, mode="review")
    a.verify(rid)  # app.txt is still bad: the check fails
    a.submit_review(rid, reviewer="agent:rev", perspective="aristotle", findings=[FINDING], source="launched")
    a.dispute(rid, "F1", actor=BUILDER, reason="r")
    arbitrate(a, rid, "F1", SimulatedAdapter({"plato": [ScriptedStep(structured=OVERRULE)]}))
    d = a.finish(rid)
    assert d.disposition is Disposition.BLOCKED and "check:tests" in {r.code for r in d.reasons}
    a.close()


def test_only_a_launched_plato_may_record_a_ruling(ari, repo):
    rid = reviewed_run(ari, repo)
    ari.dispute(rid, "F1", actor=BUILDER, reason="r")
    for impostor in (BUILDER, HUMAN, "agent:reviewer:aristotle:x"):
        with pytest.raises(AuthorizationError):
            ari.record_arbitration(rid, "F1", arbiter=impostor, ruling=OVERRULE, candidate_id="c")
    with pytest.raises(ContractError):  # a ruling must be uphold or overrule
        ari.record_arbitration(rid, "F1", arbiter="agent:plato:x", ruling={"decision": "x"}, candidate_id="c")


def test_lifecycle_arbitrates_builder_disputes(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"policy": {"arbitration_resolves_findings": True}})
    a = Ariadne(root, clock=clock)
    builds = [
        ScriptedStep(edits={"app.txt": "ok\n"}, structured={"status": "done", "summary": "fixed"}),
        ScriptedStep(structured={"status": "done", "summary": "disagree",
                                 "disputes": [{"finding": "F1", "reason": "tests are out of scope"}]}),
    ]
    adapter = SimulatedAdapter(
        {
            "brunel": builds,
            "aristotle": [ScriptedStep(structured={"summary": "s", "findings": [FINDING]})],
            "plato": [ScriptedStep(structured=OVERRULE)],
        }
    )
    report = run_task(a, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter, mode="review")
    state = a.state(report.run_id)
    assert state.findings["F1"].disputes and state.findings["F1"].ruling["decision"] == "overrule"
    assert report.decision.disposition is Disposition.ACCEPTED and report.rounds == 2
    a.close()


def test_cli_dispute_arbitrate_and_metrics(repo, capsys, monkeypatch):
    from daedalus import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    a = Ariadne(repo)
    reviewed_run(a, repo)
    a.close()
    assert cli.main(["-C", str(repo), "dispute", "F1", "--reason", "tests exist elsewhere"]) == 0
    fake = SimulatedAdapter({"plato": [ScriptedStep(structured=UPHOLD)]})
    monkeypatch.setattr("daedalus.adapters.make_adapter", lambda cfg: fake)
    assert cli.main(["-C", str(repo), "arbitrate", "F1"]) == 0
    assert "uphold" in capsys.readouterr().out
    assert cli.main(["-C", str(repo), "metrics"]) == 0
    assert "disputes 1 · arbitrations 1" in capsys.readouterr().out


def test_a_ruling_is_final_for_its_candidate(ari, repo):
    rid = reviewed_run(ari, repo)
    ari.dispute(rid, "F1", actor=BUILDER, reason="r")
    assert arbitrate(ari, rid, "F1", SimulatedAdapter({"plato": [ScriptedStep(structured=UPHOLD)]})) == "uphold"
    ari.dispute(rid, "F1", actor=BUILDER, reason="please reconsider")
    second = SimulatedAdapter({"plato": [ScriptedStep(structured=OVERRULE)]})
    with pytest.raises(ContractError, match="already ruled"):
        arbitrate(ari, rid, "F1", second)
    assert not second.requests  # refused before spending a session
    cid = ari.capture(ari.state(rid)).candidate_id
    with pytest.raises(ContractError, match="already ruled"):
        ari.record_arbitration(rid, "F1", arbiter="agent:plato:y", ruling=OVERRULE, candidate_id=cid)
    (repo / "src" / "lib.py").write_text("x = 2\n", encoding="utf-8")  # new candidate: a new ruling is allowed
    assert arbitrate(ari, rid, "F1", SimulatedAdapter({"plato": [ScriptedStep(structured=OVERRULE)]})) == "overrule"


def test_resolved_findings_cannot_be_disputed(ari, repo):
    rid = reviewed_run(ari, repo)
    ari.resolve_finding(rid, "F1", actor=HUMAN, note="done")
    with pytest.raises(ContractError):
        ari.dispute(rid, "F1", actor=BUILDER, reason="r")


@pytest.mark.parametrize(
    "disputes",
    [5, "F1", [5], [{"finding": ["F1"], "reason": "r"}], [{"finding": "F1", "reason": ""}], [{"finding": "F9", "reason": "r"}]],
)
def test_malformed_builder_disputes_are_ignored(tmp_path, clock, disputes):
    root = make_repo(tmp_path / "r")
    a = Ariadne(root, clock=clock)
    adapter = SimulatedAdapter(
        {
            "brunel": [
                ScriptedStep(edits={"app.txt": "ok\n"}, structured={"status": "done"}),
                ScriptedStep(structured={"status": "done", "disputes": disputes}),
            ],
            "aristotle": [ScriptedStep(structured={"summary": "s", "findings": [FINDING]})] * 2,
        }
    )
    report = run_task(a, {"objective": "o"}, actor=HUMAN, adapter=adapter, mode="review", max_rounds=2)
    assert not any(r.role == "plato" for r in adapter.requests)
    assert not a.state(report.run_id).findings["F1"].disputes
    a.close()


def test_default_policy_lifecycle_leaves_an_overruled_finding_to_a_human(ari, repo):
    adapter = SimulatedAdapter(
        {
            "brunel": [
                ScriptedStep(edits={"app.txt": "ok\n"}, structured={"status": "done"}),
                ScriptedStep(structured={"status": "done", "disputes": [{"finding": "F1", "reason": "out of scope"}]}),
            ],
            "aristotle": [ScriptedStep(structured={"summary": "s", "findings": [FINDING]})],
            "plato": [ScriptedStep(structured=OVERRULE)],
        }
    )
    report = run_task(ari, {"objective": "o"}, actor=HUMAN, adapter=adapter, mode="review")
    assert report.decision.disposition is Disposition.BLOCKED
    r = next(r for r in report.decision.reasons if r.code == "finding:F1")
    assert r.fixable_by == "human" and "daedalus resolve F1" in r.message
    assert sum(1 for x in adapter.requests if x.role == "plato") == 1  # no re-arbitration of the same candidate


@pytest.mark.parametrize("value", ["true", "false", 1, "yes"])
def test_arbitration_flag_must_be_a_real_boolean(value):
    import yaml

    from daedalus.core.errors import PolicyError
    from daedalus.core.policy import default_policy_text, policy_from_dict

    raw = yaml.safe_load(default_policy_text())
    raw["arbitration_resolves_findings"] = value
    with pytest.raises(PolicyError):
        policy_from_dict(raw)


def test_ruling_on_an_older_candidate_is_not_quoted(ari, repo):
    rid = reviewed_run(ari, repo)
    ari.dispute(rid, "F1", actor=BUILDER, reason="r")
    arbitrate(ari, rid, "F1", SimulatedAdapter({"plato": [ScriptedStep(structured=OVERRULE)]}))
    (repo / "src" / "lib.py").write_text("x = 3\n", encoding="utf-8")
    r = next(r for r in ari.evaluate(rid).reasons if r.code == "finding:F1")
    assert "overrule" not in r.message and r.fixable_by == "agent"
