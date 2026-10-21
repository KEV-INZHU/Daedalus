"""Test tampering end to end: a failing function plus a test replaced with `assert True`
must not be accepted in the default `gate` mode (the product audit's reproduction)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import HUMAN, git, make_repo
from daedalus.core.state_machine import Disposition
from daedalus.orchestration.ariadne import Ariadne

CALC_BAD = "def add(a, b):\n    return a - b\n"
CALC_GOOD = "def add(a, b):\n    return a + b\n"
TEST_REAL = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_FAKE = "from calc import add\n\n\ndef test_add():\n    assert True\n"

PYTEST_CHECK = {
    "tests": {
        "command": [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        "timeout_s": 120,
    }
}


@pytest.fixture
def calc_repo(tmp_path: Path) -> Path:
    root = make_repo(tmp_path / "calc", {"checks": PYTEST_CHECK})
    (root / "calc.py").write_text(CALC_BAD, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n")
    (root / "tests" / "test_calc.py").write_text(TEST_REAL, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "calc with a bug")
    return root


@pytest.fixture
def ari(calc_repo: Path, clock):
    a = Ariadne(calc_repo, clock=clock)
    yield a
    a.close()


def codes(d) -> dict[str, str]:
    return {r.code: r.fixable_by for r in d.reasons}


def tamper(root: Path) -> None:
    (root / "tests" / "test_calc.py").write_text(TEST_FAKE, encoding="utf-8")


def test_replacing_a_failing_test_with_assert_true_is_not_accepted(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    (rec,) = ari.verify(rid)
    assert rec.result == "PASS"  # the cheat works against the check alone
    d = ari.finish(rid)
    assert d.disposition is not Disposition.ACCEPTED
    assert d.risk.effective_tier == "high"
    assert "test_integrity" in d.risk.explain()
    reason = next(r for r in d.reasons if r.code == "test_integrity")
    assert reason.fixable_by == "agent"
    assert "tests weakened: tests/test_calc.py: 'test_add' lost 1 assertion and now asserts `True`" in reason.message
    assert "restore the assertion or fix the code" in reason.message
    assert "risk-exception" in reason.message
    assert d.to_dict()["test_integrity"]["flagged"] is True


def test_restoring_the_test_with_a_real_fix_is_accepted(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.verify(rid)
    assert ari.finish(rid).disposition is not Disposition.ACCEPTED
    (calc_repo / "tests" / "test_calc.py").write_text(TEST_REAL, encoding="utf-8")
    (calc_repo / "calc.py").write_text(CALC_GOOD, encoding="utf-8")
    ari.verify(rid)
    d = ari.finish(rid)
    assert d.disposition is Disposition.ACCEPTED, d.to_dict()
    assert d.risk.effective_tier == "low"
    assert "test_integrity" not in codes(d)


def test_a_human_acknowledgement_clears_it(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.verify(rid)
    assert "test_integrity" in codes(ari.evaluate(rid))
    ari.risk_exception(rid, "low", actor=HUMAN, rationale="test was obsolete; replaced by an integration test")
    d = ari.finish(rid)
    assert "test_integrity" not in codes(d)
    assert d.disposition is Disposition.ACCEPTED, d.to_dict()


def test_an_agent_cannot_acknowledge_it(ari, calc_repo):
    from daedalus.core.errors import AuthorizationError

    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    with pytest.raises(AuthorizationError):
        ari.risk_exception(rid, "low", actor="agent:builder", rationale="trust me")
    assert "test_integrity" in codes(ari.evaluate(rid))


def test_an_acknowledgement_does_not_survive_another_edit(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.risk_exception(rid, "low", actor=HUMAN, rationale="deliberate")
    assert "test_integrity" not in codes(ari.evaluate(rid))
    (calc_repo / "tests" / "test_calc.py").write_text(TEST_FAKE + "\n# edited again\n", encoding="utf-8")
    assert "test_integrity" in codes(ari.evaluate(rid))


def test_independent_reviews_of_the_flagged_hunks_clear_it(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.verify(rid)
    for i, perspective in enumerate(("socrates", "aristotle", "james")):
        ari.submit_review(rid, reviewer=f"agent:rev{i}", perspective=perspective, source="launched")
    d = ari.finish(rid)
    assert "test_integrity" not in codes(d)
    assert d.disposition is Disposition.ACCEPTED, d.to_dict()


def test_submitted_reviews_do_not_clear_it(ari, calc_repo):
    """Only reviews Daedalus launched count as independent."""
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.verify(rid)
    for i, perspective in enumerate(("socrates", "aristotle", "james")):
        ari.submit_review(rid, reviewer=f"agent:rev{i}", perspective=perspective, source="submitted")
    assert "test_integrity" in codes(ari.evaluate(rid))


def test_adding_tests_does_not_change_the_tier(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    (calc_repo / "calc.py").write_text(CALC_GOOD, encoding="utf-8")
    (calc_repo / "tests" / "test_calc.py").write_text(
        TEST_REAL + "\n\ndef test_zero():\n    assert add(0, 0) == 0\n", encoding="utf-8"
    )
    ari.verify(rid)
    d = ari.finish(rid)
    assert d.disposition is Disposition.ACCEPTED, d.to_dict()
    assert d.risk.effective_tier == "low"


def test_the_policy_can_turn_it_off(tmp_path):
    root = make_repo(tmp_path / "off", {"checks": PYTEST_CHECK, "policy": {"test_integrity": {"enabled": False}}})
    (root / "calc.py").write_text(CALC_BAD, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n")
    (root / "tests" / "test_calc.py").write_text(TEST_REAL, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "calc")
    a = Ariadne(root)
    try:
        rid = a.start({"objective": "o"}, actor=HUMAN)
        tamper(root)
        a.verify(rid)
        assert "test_integrity" not in codes(a.evaluate(rid))
    finally:
        a.close()


def test_unparseable_test_file_blocks_as_a_signal(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    (calc_repo / "tests" / "test_calc.py").write_text("def test_add(:\n", encoding="utf-8")
    d = ari.evaluate(rid)
    assert "test_integrity" in codes(d)
    assert "could not analyze" in next(r.message for r in d.reasons if r.code == "test_integrity")


def test_flagged_hunks_reach_the_review_prompt(ari, calc_repo):
    from daedalus.orchestration import prompts

    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    section = ari.evaluate(rid).integrity.review_section()
    assert "Test integrity signals" in section and "+    assert True" in section
    contract = ari.state(rid).contract
    assert section in prompts.review_prompt("aristotle", contract, "diff", {}, [], integrity=section)
    assert "Test integrity signals" not in prompts.review_prompt("aristotle", contract, "diff", {}, [])


def test_launched_reviewers_are_shown_the_flagged_hunks_full_and_delta(ari, calc_repo):
    from daedalus.adapters.base import ScriptedStep
    from daedalus.adapters.simulated import SimulatedAdapter
    from daedalus.orchestration.lifecycle import launch_review

    clean = {"summary": "fine", "findings": []}
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.verify(rid)
    adapter = SimulatedAdapter({"aristotle": [ScriptedStep(structured=clean), ScriptedStep(structured=clean)]})
    launch_review(ari, rid, "aristotle", adapter)
    (calc_repo / "calc.py").write_text(CALC_GOOD, encoding="utf-8")  # a later change: the next review is a delta
    launch_review(ari, rid, "aristotle", adapter)
    full, delta = (r.prompt for r in adapter.requests)
    assert "review what changed since your last review" in delta
    for prompt in (full, delta):
        assert "Test integrity signals" in prompt
        assert "'test_add' lost 1 assertion and now asserts `True`" in prompt
        assert "+    assert True" in prompt


# ------------------------------------------------- review round 1: the acknowledgement path
def test_the_reason_names_the_tier_every_other_floor_yields(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    msg = next(r.message for r in ari.evaluate(rid).reasons if r.code == "test_integrity")
    assert "daedalus risk-exception low --rationale" in msg and "nothing else" in msg
    (calc_repo / "requirements.txt").write_text("pyyaml\n", encoding="utf-8")  # a medium (dependency) floor
    msg = next(r.message for r in ari.evaluate(rid).reasons if r.code == "test_integrity")
    assert "daedalus risk-exception medium --rationale" in msg


def test_acknowledging_tampering_waives_no_other_floor(ari, calc_repo):
    """Following the message's advice removes the integrity floor and nothing else: an auth change in
    the same candidate still needs its reviews and approval."""
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    (calc_repo / "src" / "auth").mkdir(parents=True)
    (calc_repo / "src" / "auth" / "login.py").write_text("x = 1\n", encoding="utf-8")
    ari.verify(rid)
    d = ari.evaluate(rid)
    msg = next(r.message for r in d.reasons if r.code == "test_integrity")
    assert "daedalus risk-exception high --rationale" in msg  # auth is high
    ari.risk_exception(rid, "high", actor=HUMAN, rationale="deliberate test rewrite")
    d = ari.evaluate(rid)
    assert "test_integrity" not in codes(d)
    assert d.risk.effective_tier == "high" and "auth" in d.risk.explain()
    assert {"review:socrates", "review:aristotle", "review:james"} <= set(codes(d))
    assert d.disposition is not Disposition.ACCEPTED


def test_acknowledging_with_the_named_tier_leaves_the_run_acceptable(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.verify(rid)
    ari.risk_exception(rid, "low", actor=HUMAN, rationale="deliberate")
    d = ari.finish(rid)
    assert d.disposition is Disposition.ACCEPTED, d.to_dict()
    assert d.risk.effective_tier == "low"
    assert ari.state(rid).risk_exceptions[-1]["integrity"]  # the signals the human was shown


def test_an_exception_recorded_without_seeing_the_signals_does_not_clear_them(ari, calc_repo):
    """An exception granted for another reason (or by an older Daedalus) carries no digests."""
    from daedalus.core import run as ev

    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    cid = ari.capture(ari.state(rid)).candidate_id
    ari._append(rid, ev.RISK_EXCEPTION, HUMAN,
                {"tier": "high", "rationale": "auth only", "candidate_id": cid, "interactive": True})
    assert "test_integrity" in codes(ari.evaluate(rid))


def test_an_exception_covers_only_the_signals_it_recorded(ari, calc_repo):
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    ari.risk_exception(rid, "low", actor=HUMAN, rationale="deliberate")
    state = ari.state(rid)
    cid = ari.capture(state).candidate_id
    state.risk_exceptions[-1]["integrity"] = ["0" * 16]  # as if it had been shown other signals
    from daedalus.core.acceptance import integrity_acknowledged

    assert not integrity_acknowledged(state, cid, ari.evaluate(rid).integrity)


def test_the_cli_shows_the_signals_it_acknowledges(calc_repo, monkeypatch, capsys):
    from daedalus import cli

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("DAEDALUS_AGENT", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["-C", str(calc_repo), "start", "fix add"]) == 0
    tamper(calc_repo)
    capsys.readouterr()
    assert cli.main(["-C", str(calc_repo), "risk-exception", "low", "--rationale", "deliberate"]) == 0
    out = capsys.readouterr().out
    assert "acknowledges the test-integrity signals" in out and "tests/test_calc.py" in out


def test_a_policy_file_without_the_key_is_still_scanned_in_new_runs(tmp_path):
    import yaml

    from daedalus.core.policy import default_policy_text

    pol = yaml.safe_load(default_policy_text())
    del pol["test_integrity"]  # a complete policy_file that predates the feature
    pol["checks"] = PYTEST_CHECK
    root = make_repo(tmp_path / "pf", {"policy_file": "policy.yml"})
    cfg = yaml.safe_load((root / ".daedalus.yml").read_text())
    cfg.pop("checks", None)
    (root / ".daedalus.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (root / "policy.yml").write_text(yaml.safe_dump(pol), encoding="utf-8")
    (root / "calc.py").write_text(CALC_BAD, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n")
    (root / "tests" / "test_calc.py").write_text(TEST_REAL, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "calc")
    a = Ariadne(root)
    try:
        assert a.config().policy.test_integrity.enabled  # what `daedalus ci` / `daedalus policy` report
        rid = a.start({"objective": "o"}, actor=HUMAN)
        assert a.state(rid).policy.test_integrity.enabled  # and what the run evaluates
        tamper(root)
        a.verify(rid)
        assert "test_integrity" in codes(a.evaluate(rid))
    finally:
        a.close()


def test_the_scan_is_computed_once_per_candidate(ari, calc_repo, monkeypatch):
    from daedalus.orchestration import ariadne

    calls = []
    real = ariadne.analyze_tests
    monkeypatch.setattr(ariadne, "analyze_tests", lambda *a, **k: calls.append(1) or real(*a, **k))
    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    for _ in range(3):
        ari.evaluate(rid)
    assert len(calls) == 1
    (calc_repo / "tests" / "test_calc.py").write_text(TEST_FAKE + "# again\n", encoding="utf-8")
    ari.evaluate(rid)
    assert len(calls) == 2


def test_a_risk_exception_refuses_when_the_tree_changed_after_it_was_shown(ari, calc_repo):
    from daedalus.core.errors import ContractError

    rid = ari.start({"objective": "fix add"}, actor=HUMAN)
    tamper(calc_repo)
    shown = ari.evaluate(rid)
    digests = tuple(shown.integrity.digests())
    (calc_repo / "tests" / "test_calc.py").write_text(TEST_FAKE + "\n\ndef test_extra():\n    assert True\n",
                                                      encoding="utf-8")
    with pytest.raises(ContractError, match="changed while you were confirming"):
        ari.risk_exception(rid, "low", actor=HUMAN, rationale="deliberate",
                           expected_candidate=shown.candidate_id, expected_integrity=digests)
    assert not ari.state(rid).risk_exceptions
    # the signals alone differing (same candidate id is impossible), so check the digests guard too
    tamper(calc_repo)
    again = ari.evaluate(rid)
    with pytest.raises(ContractError):
        ari.risk_exception(rid, "low", actor=HUMAN, rationale="deliberate",
                           expected_candidate=again.candidate_id, expected_integrity=("0" * 16,))
    ari.risk_exception(rid, "low", actor=HUMAN, rationale="deliberate",
                       expected_candidate=again.candidate_id, expected_integrity=tuple(again.integrity.digests()))
    assert len(ari.state(rid).risk_exceptions) == 1


def test_the_cli_prints_the_signals_before_confirming_even_with_yes(calc_repo, monkeypatch, capsys):
    from daedalus import cli

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("DAEDALUS_AGENT", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    assert cli.main(["-C", str(calc_repo), "start", "fix add"]) == 0
    tamper(calc_repo)
    capsys.readouterr()
    assert cli.main(["-C", str(calc_repo), "risk-exception", "low", "--rationale", "deliberate", "-y"]) == 0
    out = capsys.readouterr().out
    assert out.index("this exception would acknowledge") < out.index("recorded for the current candidate")
    assert "tests/test_calc.py" in out
