"""Phase 0 acceptance invariants (spec §6, §7, §16)."""

from __future__ import annotations

import pytest
import yaml

from conftest import BUILDER, HUMAN, fix, git
from daedalus.core.state_machine import CheckState, Disposition
from daedalus.orchestration.ariadne import Ariadne


def reason_codes(d):
    return {r.code for r in d.reasons}


def test_mandatory_check_failure_is_never_accepted(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (recs) = ari.verify(rid)
    assert recs[0].result == "FAIL"
    d = ari.finish(rid)
    assert d.disposition is Disposition.BLOCKED
    assert d.check_states["tests"][0] is CheckState.FAIL
    assert not ari.state(rid).is_finished


def test_required_check_absent_from_results_makes_acceptance_impossible(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    d = ari.finish(rid)  # never verified
    assert d.disposition is Disposition.BLOCKED
    assert d.check_states["tests"][0] is CheckState.NOT_RUN


def test_no_configured_checks_cannot_accept(tmp_path, clock):
    from conftest import make_repo

    root = make_repo(tmp_path / "r", {"checks": {}})
    a = Ariadne(root, clock=clock)
    rid = a.start({"objective": "o"}, actor=HUMAN)
    d = a.finish(rid)
    assert d.disposition is Disposition.BLOCKED
    assert "no_checks" in reason_codes(d)
    a.close()


def test_candidate_change_after_pass_makes_evidence_stale(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    (repo / "src" / "lib.py").write_text("x = 2\n", encoding="utf-8")
    d = ari.finish(rid)
    assert d.check_states["tests"][0] is CheckState.STALE
    assert d.disposition is Disposition.BLOCKED
    assert "working tree changed afterward" in d.check_states["tests"][1]


def test_untracked_file_changes_candidate_identity(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    state = ari.state(rid)
    before = ari.capture(state).candidate_id
    (repo / "src" / "new_module.py").write_text("y = 1\n", encoding="utf-8")
    assert ari.capture(state).candidate_id != before


def test_candidate_identity_change_mid_run_cannot_reuse_prior_evidence(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    first = ari.evaluate(rid).candidate_id
    (repo / "app.txt").write_text("ok but different\n", encoding="utf-8")
    d = ari.evaluate(rid)
    assert d.candidate_id != first
    assert d.check_states["tests"][0] is CheckState.STALE
    # Reverting restores the exact candidate the evidence was bound to.
    fix(repo)
    assert ari.evaluate(rid).check_states["tests"][0] is CheckState.PASS


def test_untrusted_verifier_fails_closed(repo, clock):
    a = Ariadne(repo, clock=clock, verifier_id="rogue-verifier")
    rid = a.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    recs = a.verify(rid)
    assert recs[0].result == "ERROR" and not recs[0].verifier_trusted
    d = a.finish(rid)
    assert d.check_states["tests"][0] is CheckState.ERROR
    assert d.disposition is not Disposition.ACCEPTED
    assert any(r.fixable_by == "human" for r in d.reasons if r.code == "check:tests")
    a.close()


def test_sensitive_path_enforces_risk_floor(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    (repo / "src" / "auth").mkdir()
    (repo / "src" / "auth" / "login.py").write_text("ok = True\n", encoding="utf-8")
    ari.verify(rid)
    d = ari.finish(rid)
    assert d.risk.effective_tier == "high"
    assert d.disposition is Disposition.BLOCKED
    assert {"review:socrates", "review:aristotle", "review:james", "approval:merge"} <= reason_codes(d)


def test_declared_risk_cannot_lower_floor(ari, repo):
    rid = ari.start({"objective": "o", "risk_tier": "low"}, actor=HUMAN)
    (repo / "src" / "auth").mkdir()
    (repo / "src" / "auth" / "x.py").write_text("", encoding="utf-8")
    assert ari.evaluate(rid).risk.effective_tier == "high"


def test_contract_criterion_change_advances_version_and_stales_evidence(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    assert ari.evaluate(rid).check_states["tests"][0] is CheckState.PASS
    new = ari.amend(
        rid, {"acceptance_criteria": ["app says ok", "lib unchanged"]}, actor=HUMAN, rationale="clarified"
    )
    assert new.contract_version == 2
    d = ari.evaluate(rid)
    assert d.check_states["tests"][0] is CheckState.STALE
    assert d.disposition is Disposition.BLOCKED


def test_agent_cannot_amend_contract(ari):
    from daedalus.core.errors import AuthorizationError

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    with pytest.raises(AuthorizationError):
        ari.amend(rid, {"objective": "easier"}, actor=BUILDER, rationale="nope")


def test_unauthorized_contract_drift_never_yields_acceptance(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    path = repo / ".daedalus" / "runs" / rid / "contract.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    body["objective"] = "something easier"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    d = ari.finish(rid)
    assert d.disposition is Disposition.REJECTED
    assert any("contract_drift" in r.code or "contract file" in r.message for r in d.reasons)


def test_policy_drift_never_yields_acceptance(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    cfg = yaml.safe_load((repo / ".daedalus.yml").read_text(encoding="utf-8"))
    cfg["checks"]["tests"]["timeout_s"] = 5
    (repo / ".daedalus.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    git(repo, "commit", "-qam", "weaken policy")
    d = ari.finish(rid)
    assert d.disposition is Disposition.REJECTED


def test_out_of_scope_change_is_a_violation(ari, repo):
    rid = ari.start({"objective": "o", "scope": ["app.txt"]}, actor=HUMAN)
    fix(repo)
    (repo / "src" / "lib.py").write_text("x = 3\n", encoding="utf-8")
    ari.verify(rid)
    d = ari.finish(rid)
    assert d.disposition is Disposition.REJECTED
    assert any("outside the contract scope" in r.message for r in d.reasons)


def test_budget_exhausted_with_blocker_is_rejected(ari, repo):
    rid = ari.start({"objective": "o", "budget": {"max_attempts": 1}}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.raise_blocker(rid, "unclear requirement", actor=BUILDER)
    d = ari.finish(rid)
    assert d.disposition is Disposition.REJECTED
    assert "budget_exhausted" in reason_codes(d)


def test_wall_clock_budget_with_unmet_conditions_rejects(ari, repo, clock):
    rid = ari.start({"objective": "o", "budget": {"max_wall_seconds": 100}}, actor=HUMAN)
    clock.advance(500)
    d = ari.finish(rid)
    assert d.disposition is Disposition.REJECTED


def test_open_blocker_blocks_and_resolution_by_raiser_unblocks(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    bid = ari.raise_blocker(rid, "double-check edge case", actor=BUILDER)
    assert ari.finish(rid).disposition is Disposition.BLOCKED
    ari.resolve_blocker(rid, bid, actor=BUILDER)
    assert ari.finish(rid).disposition is Disposition.ACCEPTED


def test_criterion_without_check_needs_human_attestation(ari, repo):
    from daedalus.core.errors import AuthorizationError

    rid = ari.start({"objective": "o", "acceptance_criteria": ["docs read well"]}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    d = ari.finish(rid)
    assert "criterion:C1" in reason_codes(d)
    with pytest.raises(AuthorizationError):
        ari.attest(rid, "C1", "pass", actor=BUILDER, evidence="trust me")
    ari.attest(rid, "C1", "pass", actor=HUMAN, evidence="read it")
    assert ari.finish(rid).disposition is Disposition.ACCEPTED


def test_tree_change_during_check_is_error(ari, repo):
    import sys

    cfg = yaml.safe_load((repo / ".daedalus.yml").read_text(encoding="utf-8"))
    cfg["checks"]["tests"]["command"] = [
        sys.executable,
        "-c",
        "open('app.txt','w').write('ok mutated\\n')",
    ]
    (repo / ".daedalus.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    git(repo, "commit", "-qam", "self-mutating check")
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    recs = ari.verify(rid)
    assert recs[0].result == "ERROR"
    assert "changed while" in (recs[0].error or "")


def test_acceptance_is_terminal_and_later_edits_need_a_new_run(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    assert ari.finish(rid).disposition is Disposition.ACCEPTED
    (repo / "app.txt").write_text("ok edited later\n", encoding="utf-8")
    d = ari.evaluate(rid)
    assert d.disposition is Disposition.ACCEPTED and d.terminal
    assert "post_acceptance_change" in reason_codes(d)


def test_uncommitted_policy_refuses_to_start(ari, repo):
    from daedalus.core.errors import PolicyError

    (repo / ".daedalus.yml").write_text("enabled: true\nchecks: {}\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        ari.start({"objective": "o"}, actor=HUMAN)
