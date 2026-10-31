"""Proposed contracts: an agent suggests the objective and criteria, a person confirms them."""

from __future__ import annotations

import pytest

from conftest import HUMAN
from daedalus.core.errors import AuthorizationError, ContractError
from daedalus.harness.claude_code import AUTO_ACTOR, opened_message, proposal_note

AGENT = "agent:harness"


@pytest.fixture
def run(ari):
    return ari.start(
        {"objective": "create this The messaging platform is a shared, local fi..."}, actor=AUTO_ACTOR
    )


def propose(ari, run, **kw):
    args = {
        "objective": "Add a coordination channel",
        "criteria": ["late joiners see decisions"],
        "actor": AGENT,
    }
    return ari.propose_contract(run, **{**args, **kw})


def test_proposing_changes_nothing_until_a_person_confirms(ari, run):
    before = ari.state(run).contract
    pid = propose(ari, run, attest=["the docs read clearly"], rationale="the prompt was pasted whole")
    state = ari.state(run)
    assert state.contract == before
    assert state.pending_proposal["proposal_id"] == pid and state.pending_proposal["proposed_by"] == AGENT

    with pytest.raises(AuthorizationError):  # agents never hold contract_change
        ari.confirm_proposal(run, actor=AGENT)
    new = ari.confirm_proposal(run, pid, actor=HUMAN)
    assert new.contract_version == before.contract_version + 1
    assert new.objective == "Add a coordination channel"
    c1, c2 = new.acceptance_criteria
    assert (c1.criterion_id, c1.description, c1.checks) == ("C1", "late joiners see decisions", ("tests",))
    assert (c2.criterion_id, c2.description, c2.checks) == ("C2", "the docs read clearly", ())
    state = ari.state(run)
    assert state.pending_proposal is None and state.proposals[pid]["confirmed"]["by"] == HUMAN


def test_rejecting_and_proposing_again(ari, run):
    first = propose(ari, run)
    with pytest.raises(AuthorizationError):
        ari.reject_proposal(run, actor=AGENT)
    ari.reject_proposal(run, actor=HUMAN, reason="too vague")
    assert ari.state(run).pending_proposal is None
    with pytest.raises(ContractError):
        ari.confirm_proposal(run, first, actor=HUMAN)
    second = propose(ari, run, objective="Better objective")
    assert ari.state(run).pending_proposal["proposal_id"] == second != first


def test_an_amendment_retires_older_proposals(ari, run):
    pid = propose(ari, run)
    ari.amend(run, {"objective": "set by hand"}, actor=HUMAN, rationale="changed my mind")
    assert ari.state(run).pending_proposal is None
    with pytest.raises(ContractError):
        ari.confirm_proposal(run, pid, actor=HUMAN)


def test_only_the_pending_proposal_can_be_confirmed(ari, run):
    old = propose(ari, run)
    propose(ari, run, objective="newer")
    with pytest.raises(ContractError):
        ari.confirm_proposal(run, old, actor=HUMAN)
    assert ari.confirm_proposal(run, actor=HUMAN).objective == "newer"


@pytest.mark.parametrize(
    "bad",
    [{"objective": "  "}, {"criteria": []}, {"criteria": ["ok", " "]}, {"objective": "x" * 601}],
)
def test_bad_proposals_are_refused(ari, run, bad):
    with pytest.raises(ContractError):
        propose(ari, run, **bad)


def test_the_harness_asks_for_a_proposal_and_surfaces_it(ari, run):
    assert "daedalus propose" in opened_message(ari, run)
    assert proposal_note(ari.state(run)) is None
    propose(ari, run)
    note = proposal_note(ari.state(run))
    assert note.startswith("[human]") and "daedalus confirm" in note and "Add a coordination channel" in note


def test_a_run_a_person_started_is_not_asked_for_a_proposal(ari):
    rid = ari.start({"objective": "o", "acceptance_criteria": ["x works"]}, actor=HUMAN)
    assert "daedalus propose" not in opened_message(ari, rid)


def test_cli_propose_in_an_agent_session_and_confirm_needs_a_person(repo, ari, run, monkeypatch, capsys):
    from daedalus import cli
    from daedalus.cli import main

    monkeypatch.setenv("DAEDALUS_AGENT", "1")
    assert (
        main(["-C", str(repo), "propose", "Ship the bus", "-c", "tests cover it", "--attest", "reads well"])
        == 0
    )
    out = capsys.readouterr().out
    assert "P1" in out and "daedalus confirm" in out
    assert main(["-C", str(repo), "confirm", "-y"]) == 2  # refused inside an agent session
    assert main(["-C", str(repo), "status"]) == 0
    assert "Proposed contract P1" in capsys.readouterr().out

    monkeypatch.delenv("DAEDALUS_AGENT")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    assert main(["-C", str(repo), "confirm", "-y"]) == 0
    assert ari.state(run).contract.objective == "Ship the bus"
