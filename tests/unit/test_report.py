"""The verdict report: data from the audit log, Markdown and HTML renderers, escaping, and the CLI.

The golden files in report_golden/ come from a hand-built Report, so they do not depend on git hashes
or wall-clock time. Regenerate them with `UPDATE_GOLDEN=1 pytest tests/unit/test_report.py`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from conftest import HUMAN, fix
from daedalus import cli
from daedalus.report import FOOTER, Report, build_report, render_html, render_markdown
from daedalus.report.model import (
    Audit, CheckRow, Cost, CriterionRow, FindingRow, HumanAct, ReasonRow, ReviewRow, SignalRow, TimelineRow,
)

GOLDEN = Path(__file__).parent / "report_golden"
T0 = 1_700_000_000.0


def sample(**over) -> Report:
    base = dict(
        run_id="run-0123456789", generated_at=T0 + 600, objective="Fix the add() function", mode="review",
        scope=["src/**"], criteria=[CriterionRow("C1", "add() returns the sum", ["tests"]),
                                    CriterionRow("C2", "the API reads well", [])],
        contract_version=1, base_revision="a" * 40, candidate_id="b" * 64, tier="high",
        tier_reason="tier high: path floor `auth` via src/auth.py", disposition="ACCEPTED", final=True,
        reasons=[], check_states={"tests": "PASS"},
        checks=[
            CheckRow("tests", ["python", "-m", "pytest", "-q"], "FAIL", "", 2.5, T0, "c" * 64, "system:daedalus", True),
            CheckRow("tests", ["python", "-m", "pytest", "-q"], "PASS", "PASS", 0.0, T0 + 60, "b" * 64,
                     "system:daedalus", True, True,
                     {"run_id": "run-older", "seq": 17, "hash": "d" * 64, "duration_s": 2.5, "cost": 0.0,
                      "basis": "cash"}),
        ],
        reviews=[
            ReviewRow("R1", "aristotle", "full", "agent:reviewer-aristotle", "claude-opus-4", "c" * 64, False,
                      {"MAJOR": 1, "MINOR": 1}),
            ReviewRow("R2", "aristotle", "delta", "agent:reviewer-aristotle", "claude-opus-4", "b" * 64, False, {}),
            ReviewRow("R3", "james", "full", "agent:reviewer-james", "", "b" * 64, True, {},
                      {"run_id": "run-older", "seq": 23, "hash": "e" * 64, "duration_s": 40.0, "cost": 0.5,
                       "basis": "api_equivalent"}),
        ],
        findings=[
            FindingRow("F1", "Off-by-one in loop", "MAJOR", "resolved", "aristotle", "R1",
                       "resolved by aristotle review R2", "agent:reviewer-aristotle"),
            FindingRow("F2", "Naming", "MINOR", "open", "aristotle", "R1"),
        ],
        blocking_open=0,
        tamper_signals=[SignalRow("assert_removed", "tests/test_add.py", "'test_add' lost 1 assertion")],
        human_acts=[HumanAct("risk_exception", "human:kev", T0 + 120, "tier medium: only docs changed")],
        cost=Cost(total=0.41, cash=0.0, api_equivalent=0.41, cache_hits=2, saved_total=1.1, saved_cash=0.0,
                  saved_seconds=42.5, sessions=2, input_tokens=12_000, output_tokens=800),
        audit=Audit(True, "f" * 64, 40, 12),
        timeline=[TimelineRow(T0, "human:kev", "run.created", ""),
                  TimelineRow(T0 + 60, "system:daedalus", "evidence.recorded", "tests PASS (reused)"),
                  TimelineRow(T0 + 300, "system:daedalus", "disposition.recorded", "ACCEPTED")],
    )
    base.update(over)
    return Report(**base)


def check_golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8"), f"{name} changed; rerun with UPDATE_GOLDEN=1 if intended"


# ---------------------------------------------------------------- golden
def test_markdown_golden():
    check_golden("report.md", render_markdown(sample()))


def test_html_golden():
    check_golden("report.html", render_html(sample()))


def test_headline_and_footer():
    md = render_markdown(sample())
    assert md.splitlines()[0] == (
        "### ✅ ACCEPTED · tier high · tests PASS · 3 reviews (0 blocking) · $0.41 · chain verified"
    )
    assert "CI re-verifies checks independently" in md and "CI re-verifies checks independently" in render_html(sample())
    assert FOOTER.startswith("Generated locally from")


def test_a_broken_chain_and_a_blocked_run_are_not_dressed_up():
    r = sample(disposition="BLOCKED", final=False, audit=Audit(False, "", 3, 3, "hash chain broken at seq 2"),
               reasons=[ReasonRow("check:tests", "tests failed", "agent")], check_states={"tests": "FAIL"})
    line = render_markdown(r).splitlines()[0]
    assert "BLOCKED" in line and "chain NOT verified" in line and "tests: " not in line
    assert "tests FAIL" in line
    assert "hash chain broken" in render_html(r)


# ------------------------------------------------------------- escaping
HOSTILE = [
    "<script>alert(1)</script>",
    "`rm -rf /` `` ``` ",
    "a | b | c",
    "| leading pipe",
    "# Heading\n</details><img src=x onerror=alert(1)>",
    "[click](javascript:alert(1)) @everyone",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_markdown_neutralises_hostile_finding_text(text):
    r = sample(findings=[FindingRow("F1", text, "MAJOR", "open", "aristotle", "R1", text)], objective=text)
    md = render_markdown(r)
    assert "<script" not in md and "<img" not in md and "</details><" not in md
    assert md.count("<details") == md.count("</details>")
    assert "`" not in md  # hostile backticks become entities; our own text uses <code>
    for line in md.splitlines():
        if line.startswith("| F1 "):
            assert line.count("|") - line.count("\\|") == 6  # id, severity, status, title, resolution: 5 cells
        assert not line.startswith("#") or line.startswith("### ")


def test_markdown_findings_cannot_add_table_cells_or_headings():
    r = sample(findings=[FindingRow("F1", "a | b\n# h", "MAJOR", "open", "aristotle", "R1")])
    row = next(ln for ln in render_markdown(r).splitlines() if ln.startswith("| F1 "))
    assert row.count("|") == 6 and "# h" not in row.split("|")[4].lstrip().split(" ")[0]


@pytest.mark.parametrize("text", HOSTILE)
def test_html_escapes_everything(text):
    r = sample(findings=[FindingRow("F1", text, "MAJOR", "open", "aristotle", "R1", text)], objective=text,
               tamper_signals=[SignalRow("k", text, text)], human_acts=[HumanAct("approval", text, T0, text)],
               timeline=[TimelineRow(T0, text, "run.created", text)])
    page = render_html(r)
    assert "<script" not in page and "<img" not in page
    assert "onerror=alert(1)>" not in page.replace("&gt;", "")  # the tag never closes unescaped


def test_html_is_self_contained():
    page = render_html(sample())
    assert not re.search(r"(src|href)\s*=|@import|url\(|<script|<link|https?://", page)
    assert "prefers-color-scheme:dark" in page and "<title>Daedalus report run-0123456789</title>" in page


def test_long_reports_are_cut_to_fit():
    many = [FindingRow(f"F{i}", "x" * 190, "MINOR", "open", "aristotle", "R1", "y" * 190) for i in range(600)]
    md = render_markdown(sample(findings=many))
    assert len(md) < 60_000
    assert "left out to keep this comment short" in md and "### ✅ ACCEPTED" in md


# ------------------------------------------------------------------ JSON
def test_json_round_trip():
    r = sample()
    again = Report.from_dict(json.loads(json.dumps(r.to_dict())))
    assert again == r


# ----------------------------------------------------------- real runs
def test_build_report_from_a_real_run(ari, repo, clock):
    rid = ari.start({"objective": "fix app", "acceptance_criteria": [{"description": "app is ok", "checks": ["tests"]}]},
                    actor=HUMAN)
    ari.verify(rid)  # FAIL
    fix(repo)
    clock.advance(30)
    ari.verify(rid)  # PASS
    ari.charge(rid, cost=0.25, basis="cash")
    ari.submit_review(rid, reviewer="agent:rev", perspective="aristotle", source="launched", findings=[
        {"title": "Use <b>strict</b> | mode", "evidence": "e", "severity": "MINOR"}])
    ari.resolve_finding(rid, "F1", actor=HUMAN, note="fine with me")
    clock.advance(30)
    ari.finish(rid)

    r = build_report(ari, rid)
    assert (r.disposition, r.final, r.objective) == ("ACCEPTED", True, "fix app")
    assert r.check_states == {"tests": "PASS"} and [c.result for c in r.checks] == ["FAIL", "PASS"]
    assert r.checks[-1].state == "PASS" and r.checks[0].state == ""
    assert r.base_revision and r.candidate_id and r.tier
    assert [(v.perspective, v.scope, v.counts) for v in r.reviews] == [("aristotle", "full", {"MINOR": 1})]
    f = r.findings[0]
    assert (f.status, f.resolution, f.resolved_by) == ("resolved", "fine with me", HUMAN)
    assert r.cost.cash == 0.25 and r.cost.total == 0.25
    assert [a.kind for a in r.human_acts] == ["resolution"] and r.human_acts[0].actor == HUMAN
    assert r.audit.verified and len(r.audit.head_hash) == 64
    assert r.timeline[0].type == "run.created" and r.timeline[-1].type == "disposition.recorded"
    # Reading never writes.
    n = len(ari.store.events())
    render_markdown(r), render_html(r)
    build_report(ari, rid)
    assert len(ari.store.events()) == n


def test_open_run_that_meets_every_condition_reads_ready(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    r = build_report(ari, rid)
    assert (r.disposition, r.final) == ("READY", False)
    assert "READY" in render_markdown(r).splitlines()[0]


def test_blocked_run_lists_reasons(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.verify(rid)  # fails
    r = build_report(ari, rid)
    assert r.disposition == "BLOCKED" and r.reasons and "tests" in r.check_states
    assert r.check_states["tests"] != "PASS"


def test_reused_check_is_marked_with_its_source(ari, repo):
    a = ari.start({"objective": "first"}, actor=HUMAN)
    fix(repo)
    ari.verify(a)
    ari.finish(a)
    b = ari.start({"objective": "second"}, actor=HUMAN)
    ari.verify(b)
    r = build_report(ari, b)
    reused = [c for c in r.checks if c.reused]
    if reused:  # the cache applies on an identical tree; when it does the source must be named
        assert reused[0].reused_from["run_id"] == a
        assert "reused" in render_markdown(r)


# ------------------------------------------------------------------- CLI
def test_cli_report_formats(ari, repo, tmp_path, capsys):
    rid = ari.start({"objective": "fix app"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.finish(rid)
    base = ["-C", str(repo), "report"]

    assert cli.main(base) == 0
    assert capsys.readouterr().out.startswith("### ✅ ACCEPTED")

    assert cli.main([*base, "--format", "json", "--run", rid[:8]]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["run_id"] == rid and doc["disposition"] == "ACCEPTED"

    out = tmp_path / "r.html"
    assert cli.main([*base, "--format", "html", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "wrote html report" in capsys.readouterr().out


def test_cli_report_without_a_run_is_an_error(repo, capsys):
    assert cli.main(["-C", str(repo), "report"]) == 2


def test_report_is_listed_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "report" in capsys.readouterr().out

