"""First-day honesty and friction: criteria nothing checks, compact block output, why init set no
reviewer, and `daedalus risk`."""

from __future__ import annotations

import argparse
import json

from conftest import CHECK, HUMAN, fix, git, make_repo
from daedalus.cli import main
from daedalus.cli._common import disposition_lines
from daedalus.core.contracts import Criterion
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.prompts import contract_block

WEAK_TEST = "def test_add():\n    assert True\n"


def test_a_text_criterion_bound_only_to_the_general_checks_says_so(repo, capsys):
    assert main(["-C", str(repo), "start", "make add multiply", "-c", "add(2,3) == 6"]) == 0
    out = capsys.readouterr().out
    assert "C1: add(2,3) == 6\n" in out
    assert "no check tests C1 specifically; only the general checks (tests) run" in out
    fix(repo)
    assert main(["-C", str(repo), "verify"]) == 0
    out = capsys.readouterr().out
    assert "ACCEPTED" in out and "no check tests C1 specifically" in out  # still accepted, but not oversold
    ari = Ariadne(repo)
    try:
        [c] = ari.state(ari.store.run_ids()[-1]).contract.acceptance_criteria
        assert c.general and c.checks == ("tests",)
    finally:
        ari.close()
    assert main(["-C", str(repo), "report", "--format", "md"]) == 0
    assert "(not checked specifically; only the general checks ran: tests)" in capsys.readouterr().out


def test_naming_a_dedicated_check_makes_a_criterion_specific(tmp_path, capsys):
    checks = {"tests": {"command": CHECK}, "mul": {"command": CHECK, "mandatory": False}}
    root = make_repo(tmp_path / "r", {"checks": checks})
    assert main(["-C", str(root), "start", "o", "-c", "add(2,3) == 6", "--check", "mul"]) == 0
    out = capsys.readouterr().out
    assert "no check tests" not in out


def test_general_is_recorded_only_when_true():
    assert "general" not in Criterion("C1", "d", ("tests",)).to_dict()  # older contracts hash the same
    assert Criterion("C1", "d", ("tests",), general=True).to_dict()["general"] is True


def test_the_builder_is_told_to_add_a_test_for_a_general_criterion(ari):
    rid = ari.start({"objective": "o", "acceptance_criteria": [
        {"description": "adds", "checks": ["tests"], "general": True},
        {"description": "fast", "checks": ["tests"]},
    ]}, actor=HUMAN)
    block = contract_block(ari.state(rid).contract)
    assert "C1: adds (no check tests this yet" in block and "add a test that fails unless it holds" in block
    assert "C2: fast (verified by checks: tests)" in block


def test_missing_reviews_are_one_line_and_the_remedy_is_said_once(repo, ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_add.py").write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a test")
    ari.cancel(rid, actor=HUMAN)
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (repo / "tests" / "test_add.py").write_text(WEAK_TEST, encoding="utf-8")
    fix(repo)
    ari.verify(rid)
    lines = disposition_lines(ari.evaluate(rid), repo)
    review_lines = [ln for ln in lines if "review" in ln and "requires" in ln]
    assert len(review_lines) == 1, lines
    assert "validation review (Socrates), quality review (Aristotle), functionality review (James)" in review_lines[0]
    assert sum("No adapter here can launch reviews" in ln for ln in lines) == 1
    integrity = next(ln for ln in lines if "tests weakened" in ln)
    assert "waives the test-integrity floor and nothing else" in integrity
    assert not any("the tier every other floor yields" in ln or "With a launching adapter" in ln for ln in lines)
    assert lines.index(review_lines[0]) == 1  # where the first review reason was, before the test signal


def test_next_hands_unlaunchable_reviews_to_a_person(repo, ari):
    from daedalus.cli.next import next_step

    rid = ari.start({"objective": "o", "risk_tier": "high"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    step = next_step(ari, rid)
    assert step["who"] == "human" and "No adapter here can launch reviews" in step["action"]


def test_init_says_why_it_set_no_reviewer(tmp_path, monkeypatch):
    from daedalus.cli import lifecycle

    args = argparse.Namespace(no_hooks=False, harness=["claude-code"])
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name, *a, **k: None)
    assert "Install Claude Code" in lifecycle._choose_adapter(tmp_path, args)[1]
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name, *a, **k: "/bin/" + name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    why = lifecycle._choose_adapter(tmp_path, args)[1]
    assert "would be billed" in why and "max_cash" in why
    assert lifecycle._choose_adapter(tmp_path, argparse.Namespace(no_hooks=True, harness=None)) == (None, None)


def test_risk_previews_the_working_tree(repo, capsys):
    assert main(["-C", str(repo), "risk"]) == 0
    assert capsys.readouterr().out.startswith("tier low: nothing has changed")
    (repo / "src" / "auth").mkdir()
    (repo / "src" / "auth" / "login.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["-C", str(repo), "risk"]) == 0
    out = capsys.readouterr().out
    assert "tier high" in out and "path floor `auth` -> high: src/auth/login.py" in out
    assert "needs independent reviews: validation review (Socrates)" in out
    assert "No adapter here can launch reviews" in out


def test_risk_flags_a_weakened_test(repo, capsys):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_add.py").write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a test")
    (repo / "tests" / "test_add.py").write_text(WEAK_TEST, encoding="utf-8")
    assert main(["-C", str(repo), "risk", "--json"]) == 0
    p = json.loads(capsys.readouterr().out)
    assert p["tier"] == "high" and p["changed"] == ["tests/test_add.py"]
    assert any(f["floor"] == "test_integrity" for f in p["floors"]) and p["test_signals"]


def test_risk_of_named_paths_ignores_the_tree(repo, capsys):
    assert main(["-C", str(repo), "risk", "README.md"]) == 0
    out = capsys.readouterr().out
    assert "tier low for 1 file(s)" in out and "needs: the checks only" in out


def test_risk_counts_the_reviews_the_mode_requires(tmp_path, capsys):
    root = make_repo(tmp_path / "r", {"mode": "review"})
    assert main(["-C", str(root), "risk", "README.md"]) == 0
    assert "needs reviews: quality review (Aristotle)" in capsys.readouterr().out


def weakened(root):
    (root / "tests").mkdir()
    (root / "tests" / "test_add.py").write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "a test")


def test_with_a_launching_adapter_the_reviews_are_left_to_daedalus(tmp_path, monkeypatch):
    from daedalus.adapters.command import BILLING_ENV_VARS

    for v in BILLING_ENV_VARS:  # a subscription login: nothing is billed, so reviews launch
        monkeypatch.delenv(v, raising=False)
    root = make_repo(tmp_path / "r", {"adapter": "claude-code"})
    ari = Ariadne(root)
    try:
        rid = ari.start({"objective": "o", "risk_tier": "high"}, actor=HUMAN)
        fix(root)
        ari.verify(rid)
        lines = disposition_lines(ari.evaluate(rid), root)
    finally:
        ari.close()
    assert any(ln.startswith("  [system] tier high requires independent reviews") for ln in lines), lines
    assert any("Daedalus launches the missing reviews" in ln for ln in lines)


def test_mode_driven_reviews_are_not_offered_a_risk_exception(repo, ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN, mode="review")
    fix(repo)
    ari.verify(rid)
    lines = disposition_lines(ari.evaluate(rid), repo)
    assert any(ln.startswith("  [human] review mode requires reviews") for ln in lines), lines
    remedy = next(ln for ln in lines if "No adapter here" in ln)
    assert "risk-exception" not in remedy and "mode asks for some of them" in remedy


def test_general_survives_the_contract_round_trip_and_needs_checks(ari):
    from daedalus.core.contracts import contract_from_dict

    rid = ari.start({"objective": "o", "acceptance_criteria": [
        {"description": "a", "checks": ["tests"], "general": True},
        {"description": "b", "checks": [], "general": True},  # attested by a person: never `general`
    ]}, actor=HUMAN)
    c = ari.state(rid).contract
    assert [x.general for x in c.acceptance_criteria] == [True, False]
    assert [x.general for x in contract_from_dict(c.to_dict()).acceptance_criteria] == [True, False]


def test_the_note_lists_every_general_check():
    from daedalus.cli._common import general_criteria_note

    class C:
        acceptance_criteria = (Criterion("C1", "a", ("tests",), True), Criterion("C2", "b", ("lint",), True))

    note = general_criteria_note(C())
    assert "C1, C2" in note and "(tests, lint)" in note


def test_the_html_report_marks_a_general_criterion(repo, capsys):
    assert main(["-C", str(repo), "start", "o", "-c", "it adds"]) == 0
    fix(repo)
    assert main(["-C", str(repo), "verify"]) == 0
    capsys.readouterr()
    assert main(["-C", str(repo), "report", "--format", "html"]) == 0
    assert "tests (general only: nothing checks it specifically)" in capsys.readouterr().out


def test_init_prints_why_it_set_no_reviewer(tmp_path, monkeypatch, capsys):
    from daedalus.cli import lifecycle

    monkeypatch.setattr(lifecycle.shutil, "which", lambda name, *a, **k: None)
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    assert main(["-C", str(root), "init"]) == 0
    out = capsys.readouterr().out
    assert "adapter: none, so risky changes" in out and "Install Claude Code" in out


def test_init_has_nothing_to_explain_when_claude_would_not_bill(tmp_path, monkeypatch):
    from daedalus.adapters.command import BILLING_ENV_VARS
    from daedalus.cli import lifecycle

    for v in BILLING_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name, *a, **k: "/bin/" + name)
    args = argparse.Namespace(no_hooks=False, harness=["claude-code"])
    assert lifecycle._choose_adapter(tmp_path, args) == ("claude-code", None)


def test_risk_resolves_paths_as_git_does(repo, capsys, monkeypatch):
    monkeypatch.chdir(repo / "src")
    assert main(["risk", "auth/login.py"]) == 0  # from where you are
    out = capsys.readouterr().out
    assert "src/auth/login.py" in out and "risk floors only" in out
    assert "not in the working tree (assessed as a deletion): src/auth/login.py" in out
    assert main(["-C", str(repo), "risk", "--json", "src/auth/login.py"]) == 0  # from the -C directory
    assert json.loads(capsys.readouterr().out)["changed"] == ["src/auth/login.py"]
    assert main(["risk", "../../elsewhere.py"]) == 2
    assert main(["risk", ""]) == 2


def test_risk_before_the_first_commit(tmp_path, capsys):
    root = tmp_path / "new"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".daedalus.yml").write_text("enabled: true\nchecks: {}\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["-C", str(root), "risk"]) == 0
    out = capsys.readouterr().out  # with no commit, every file is new, the gate's own config included
    assert "tier critical for 2 changed file(s)" in out and "`verification_policy` -> critical: .daedalus.yml" in out


def test_a_billed_adapter_with_no_cash_budget_is_not_promised_to_launch(tmp_path, monkeypatch):
    from daedalus.cli._common import review_blocker

    root = make_repo(tmp_path / "r", {"adapter": "claude-code"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert "would be billed" in review_blocker(root) and "max_cash" in review_blocker(root)
    root2 = make_repo(tmp_path / "r2", {"adapter": "claude-code", "policy": {"default_budget": {"max_cash": 5}}})
    assert review_blocker(root2) is None


def test_reviews_asked_by_tier_and_mode_get_both_remedies():
    from daedalus.cli._common import review_remedy

    both = review_remedy("No adapter.", tier_driven=True, mode_driven=True)
    assert "risk-exception" in both and "mode asks for some of them" in both
    assert review_remedy(None).startswith("Daedalus launches")


def test_risk_skips_paths_that_are_never_part_of_a_candidate(repo, capsys):
    assert main(["-C", str(repo), "risk", ".daedalus/x.json"]) == 0
    out = capsys.readouterr().out
    assert "no files to assess" in out and "never part of a candidate" in out


def test_a_broken_budget_or_adapter_never_crashes_status(tmp_path, monkeypatch):
    from daedalus.cli._common import review_blocker

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # billed, so the cash budget is read

    root = make_repo(tmp_path / "r", {"adapter": "claude-code"})
    cfg = (root / ".daedalus.yml").read_text(encoding="utf-8")
    (root / ".daedalus.yml").write_text(cfg + "policy:\n  default_budget: {max_cash: lots}\n", encoding="utf-8")
    assert "does not load" in review_blocker(root)
    assert "configured adapter" in review_blocker(None)


def test_a_named_adapter_that_cannot_launch_is_named(tmp_path):
    from daedalus.cli._common import review_blocker

    root = make_repo(tmp_path / "r", {"adapter": {"name": "inline"}})
    assert review_blocker(root).startswith("No adapter here can launch reviews")


def test_risk_says_when_the_test_scan_is_skipped(tmp_path, capsys):
    root = tmp_path / "new"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".daedalus.yml").write_text("enabled: true\nchecks: {}\n", encoding="utf-8")
    assert main(["-C", str(root), "risk"]) == 0
    assert "weakened-test scan skipped" in capsys.readouterr().out


def test_a_general_flag_without_checks_is_dropped_on_load():
    from daedalus.core.contracts import contract_from_dict

    d = {"run_id": "r", "contract_version": 1, "objective": "o", "scope": ["**"], "risk_tier": "low",
         "required_checks": ["tests"], "budget": {},
         "acceptance_criteria": [{"id": "C1", "description": "a", "checks": [], "general": True}]}
    assert contract_from_dict(d).acceptance_criteria[0].general is False


def test_risk_expands_a_named_directory(repo, capsys):
    (repo / "src" / "auth").mkdir()
    (repo / "src" / "auth" / "login.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["-C", str(repo), "risk", "src"]) == 0
    out = capsys.readouterr().out
    assert "tier high for 2 file(s)" in out and "path floor `auth` -> high: src/auth/login.py" in out


def test_a_shared_dedicated_check_is_said_to_be_shared(tmp_path, capsys):
    checks = {"tests": {"command": CHECK}, "mul": {"command": CHECK, "mandatory": False}}
    root = make_repo(tmp_path / "r", {"checks": checks})
    assert main(["-C", str(root), "start", "o", "-c", "a", "-c", "b", "--check", "mul"]) == 0
    assert "C1, C2 share the checks tests, mul" in capsys.readouterr().out


def test_status_judges_billed_reviews_by_the_runs_budget(tmp_path, monkeypatch):
    from daedalus.cli._common import review_blocker

    root = make_repo(tmp_path / "r", {"adapter": "claude-code", "policy": {"default_budget": {"max_cash": 1}}})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ari = Ariadne(root)
    try:
        rid = ari.start({"objective": "o", "budget": {"max_cash": 0}}, actor=HUMAN)
        state = ari.state(rid)
        assert review_blocker(root) is None  # the repository's default allows billed spend
        assert "cash budget cannot cover it" in review_blocker(root, state)  # this run's does not
    finally:
        ari.close()


def test_a_negative_cash_budget_does_not_load(tmp_path, monkeypatch):
    from daedalus.cli._common import review_blocker

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # billed, so the cash budget is read

    root = make_repo(tmp_path / "r", {"adapter": "claude-code", "policy": {"default_budget": {"max_cash": -1}}})
    assert "does not load" in review_blocker(root)


def test_tier_and_mode_reviews_are_two_lines_and_one_remedy(repo, ari):
    from daedalus.cli.next import next_step

    rid = ari.start({"objective": "o", "risk_tier": "high"}, actor=HUMAN, mode="council")
    fix(repo)
    ari.verify(rid)
    d = ari.evaluate(rid)
    lines = disposition_lines(d, repo, ari.state(rid))
    groups = [ln for ln in lines if " requires " in ln and "reviews of candidate" in ln]
    # the tier asks for three reviews; council mode adds the simplicity review the tier does not
    assert len(groups) == 2, lines
    assert groups[0].startswith("  [human] tier high") and groups[1].startswith("  [human] council mode")
    assert "simplicity review (Mozi)" in groups[1]
    remedies = [ln for ln in lines if "No adapter here can launch reviews" in ln]
    assert len(remedies) == 1
    assert "risk-exception" in remedies[0] and "mode asks for some of them" in remedies[0]
    assert next_step(ari, rid)["who"] == "human"


def test_next_on_a_mode_review_offers_no_risk_exception(repo, ari):
    from daedalus.cli.next import next_step

    rid = ari.start({"objective": "o"}, actor=HUMAN, mode="review")
    fix(repo)
    ari.verify(rid)
    step = next_step(ari, rid)
    assert step["who"] == "human" and "risk-exception" not in step["action"]


def test_a_bad_cash_budget_does_not_matter_when_nothing_is_billed(tmp_path, monkeypatch):
    from daedalus.adapters.command import BILLING_ENV_VARS
    from daedalus.cli._common import review_blocker

    for v in BILLING_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    root = make_repo(tmp_path / "r", {"adapter": "claude-code", "policy": {"default_budget": {"max_cash": -1}}})
    assert review_blocker(root) is None
