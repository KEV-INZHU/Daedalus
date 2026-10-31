"""The planted-bug benchmark scores recall, false alarms, calibration and cost."""

from __future__ import annotations

import json
import sys

import pytest

from daedalus.adapters.base import ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.bench import load_cases, names_bug, run_bench
from daedalus.core.errors import DaedalusError


@pytest.fixture(scope="module")
def cases():
    return {c.name: c for c in load_cases()}


def test_the_corpus_has_planted_bugs_and_clean_controls(cases):
    planted = [c for c in cases.values() if c.bug]
    assert len(planted) >= 10 and len(cases) - len(planted) >= 4
    assert all(c.bug_line() for c in planted)


def test_a_finding_names_the_bug_by_file_and_line_or_words(cases):
    c = cases["pagination-off-by-one"]
    assert names_bug(c, {"title": "wrong start", "evidence": "src/items.py:5 skips the first page"})
    assert names_bug(c, {"title": "page 1 skips items", "evidence": "items.py line 6: should be (page - 1)"})
    assert not names_bug(c, {"title": "wrong start", "evidence": "src/items.py:1 docstring"})
    assert not names_bug(c, {"title": "off-by-one", "evidence": "src/other.py:5"})  # another file
    assert not names_bug(cases["clean-helper"], {"title": "x", "evidence": "src/mathx.py:6"})


def _verdict(caught: bool, case, conf=0.8, extra=()):
    f = [{"title": "bug", "evidence": f"{case.bug['file']}:{case.bug_line()}", "severity": "MAJOR",
          "confidence": conf}] if caught else []
    return {"summary": "s", "findings": f + list(extra), "residual_risk": 0.2}


def test_scores_add_up(cases):
    picked = [cases["pagination-off-by-one"], cases["mutable-default"], cases["clean-helper"]]
    invented = {"title": "style", "evidence": "src/mathx.py:2", "severity": "MAJOR", "confidence": 0.6}
    steps = [_verdict(True, picked[0]), _verdict(False, picked[1]),
             {"summary": "s", "findings": [invented], "residual_risk": 0.1}]
    rev = SimulatedAdapter({"aristotle": [ScriptedStep(structured=b) for b in steps]})
    s = run_bench(picked, rev)
    assert (s["caught"], s["planted"], s["recall"]) == (1, 2, 0.5)
    assert (s["clean_cases_with_false_alarm"], s["clean"]) == (1, 1)
    assert s["findings_brier"]["n"] == 2  # the hit (0.8, 1) and the invented MAJOR (0.6, 0)
    assert s["findings_brier"]["brier"] == pytest.approx(((0.2) ** 2 + 0.6 ** 2) / 2)
    # residual: caught (0.2 vs 0), missed (0.2 vs 1), clean (0.1 vs 0)
    assert s["residual_brier"]["brier"] == pytest.approx((0.04 + 0.64 + 0.01) / 3)
    assert "three questions" not in rev.requests[0].prompt
    assert run_bench(picked[:1], SimulatedAdapter({"aristotle": [ScriptedStep(structured=steps[0])]}),
                     lite=True)["lite"]


def test_a_bad_cases_file_is_refused(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("cases:\n  - {name: x, objective: o, before: {}, after: {a.py: 'x = 1'}, "
                   "bug: {file: a.py, line: 'y = 2'}}\n")
    with pytest.raises(DaedalusError, match="not in a.py"):
        load_cases(bad)


def test_any_reviewer_command_can_be_benchmarked(tmp_path, capsys):
    from conftest import make_repo
    from daedalus.cli import main

    root = make_repo(tmp_path / "r")
    script = tmp_path / "rev.py"
    script.write_text("import sys, json\nsys.stdin.read()\n"
                      "print(json.dumps({'summary': 's', 'findings': [], 'residual_risk': 0.3}))\n")
    assert main(["-C", str(root), "bench", "--reviewer-cmd", f"{sys.executable} {script}",
                 "--only", "clean-helper", "mutable-default", "--json"]) == 0
    s = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert s["reviewer"] == "reviewer-cmd" and s["planted"] == 1 and s["caught"] == 0 and s["clean"] == 1
    assert list((root / ".daedalus" / "bench").glob("*.json"))
    assert main(["-C", str(root), "bench", "--only", "nope"]) == 2
