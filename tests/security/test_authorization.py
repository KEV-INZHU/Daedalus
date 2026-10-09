"""Authorization, authority and reviewer-independence rules (spec §10, §15)."""

from __future__ import annotations

import pytest

from conftest import BUILDER, HUMAN, fix
from daedalus.core.errors import AuthorizationError, PolicyError
from daedalus.core.policy import policy_from_dict
from daedalus.core.state_machine import Disposition


def accepted_run(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    assert ari.finish(rid).disposition is Disposition.ACCEPTED
    return rid


def test_agents_never_hold_authority_even_if_policy_says_so():
    raw = {"policy_version": "1", "tiers": ["low"], "checks": {}, "authorities": {"merge": ["agent:builder"]}}
    with pytest.raises(PolicyError):
        policy_from_dict(raw)


def test_agent_cannot_approve(ari, repo):
    rid = accepted_run(ari, repo)
    with pytest.raises(AuthorizationError):
        ari.approve(rid, "merge", actor=BUILDER)


def test_approved_action_runs_once(ari, repo):
    rid = accepted_run(ari, repo)
    ari.approve(rid, "merge", actor=HUMAN)
    assert ari.execute_action(rid, "merge", actor=HUMAN, runner=lambda: 0)["ok"]
    with pytest.raises(AuthorizationError, match="already used"):
        ari.execute_action(rid, "merge", actor=HUMAN, runner=lambda: 0)


def test_expired_authorization_denies_action(ari, repo, clock):
    rid = accepted_run(ari, repo)
    ari.approve(rid, "merge", actor=HUMAN, expires_in=60)
    clock.advance(120)
    with pytest.raises(AuthorizationError, match="expired"):
        ari.execute_action(rid, "merge", actor=HUMAN, runner=lambda: 0)


def test_approval_is_bound_to_candidate(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    (repo / "src" / "auth").mkdir()
    (repo / "src" / "auth" / "a.py").write_text("", encoding="utf-8")
    ari.approve(rid, "merge", actor=HUMAN)
    assert "approval:merge" not in {r.code for r in ari.evaluate(rid).reasons}
    (repo / "src" / "auth" / "a.py").write_text("changed = 1\n", encoding="utf-8")
    assert "approval:merge" in {r.code for r in ari.evaluate(rid).reasons}


def test_denied_required_authorization_rejects(ari, repo):
    rid = ari.start({"objective": "o", "approval_requirements": ["deploy"]}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.deny(rid, "deploy", actor=HUMAN, rationale="not this week")
    assert ari.finish(rid).disposition is Disposition.REJECTED


def test_restricted_action_requires_acceptance(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.approve(rid, "merge", actor=HUMAN)
    with pytest.raises(AuthorizationError, match="ACCEPTED"):
        ari.execute_action(rid, "merge", actor=HUMAN, runner=lambda: 0)


def test_author_cannot_review_own_candidate(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.record_candidate(rid, actor=BUILDER)
    with pytest.raises(AuthorizationError):
        ari.submit_review(rid, reviewer=BUILDER, perspective="aristotle", summary="lgtm")


def test_submitted_review_does_not_satisfy_independent_requirement(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    (repo / "src" / "auth").mkdir()
    (repo / "src" / "auth" / "a.py").write_text("", encoding="utf-8")
    ari.verify(rid)
    ari.submit_review(rid, reviewer="agent:someone", perspective="aristotle", summary="fine")
    assert "review:aristotle" in {r.code for r in ari.evaluate(rid).reasons}


def test_blocker_finding_blocks_until_its_reviewer_resolves(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN, mode="review")
    fix(repo)
    ari.verify(rid)
    finding = {"title": "bug", "evidence": "line 1", "severity": "BLOCKER", "required_change": "fix"}
    ari.submit_review(rid, reviewer="agent:rev", perspective="aristotle", findings=[finding], source="launched")
    assert ari.finish(rid).disposition is Disposition.BLOCKED
    with pytest.raises(AuthorizationError):
        ari.resolve_finding(rid, "F1", actor=BUILDER)
    ari.submit_review(rid, reviewer="agent:rev2", perspective="aristotle", resolves=["F1"], source="launched")
    assert ari.finish(rid).disposition is Disposition.ACCEPTED


def test_human_cli_commands_refuse_inside_agent_session(monkeypatch):
    from daedalus import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(AuthorizationError):
        cli.human_actor("approve merge", assume_yes=True)


def test_human_cli_commands_refuse_without_tty(monkeypatch):
    from daedalus import cli

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("DAEDALUS_AGENT", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    with pytest.raises(AuthorizationError):
        cli.human_actor("approve merge", assume_yes=True)


def test_check_env_scrubs_credentials(monkeypatch):
    from daedalus.verification.check_runner import scrubbed_env

    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("MY_API_KEY", "x")
    monkeypatch.setenv("HARMLESS", "x")
    env = scrubbed_env(())
    assert "GITHUB_TOKEN" not in env and "MY_API_KEY" not in env and "HARMLESS" in env
