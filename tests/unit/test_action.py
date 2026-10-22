"""The PR comment body, the workflow annotations, and the security shape of action.yml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from daedalus.ci import COMMENT_MARKER, CIReport

ROOT = Path(__file__).resolve().parents[2]
HOSTILE = "evil|<img src=x onerror=alert(1)>[x](http://e.com)`\n::error::pwned,x%.py"


def report(**kw) -> CIReport:
    base = dict(
        base="origin/main", changed=["a.py"], tier="high", risk="tier high: path floor `auth`",
        reviews=("socrates",), approvals=(), checks={"tests": ("PASS", "")},
        commits=[("a" * 40, "fix it", "run123")], tier_source="path", durations={"tests": 1.234},
    )
    base.update(kw)
    return CIReport(**base)


def test_comment_starts_with_a_headline_and_ends_with_the_marker():
    body = report().pr_comment()
    assert body.splitlines()[0] == "Daedalus CI: ✅ passed · tier high (path floor) · tests PASS · tamper scan clean"
    assert body.rstrip().endswith(COMMENT_MARKER) and COMMENT_MARKER == "<!-- daedalus-ci -->"
    assert "1.2s" in body and "<details>" in body
    assert "listed, not verified" in body and "socrates review" in body
    assert "`run123`" in body


def test_failed_headline_and_signal_count():
    r = report(checks={"tests": ("FAIL", "exit 1")}, signals=[("deleted", "t.py", "gone")])
    head = r.headline()
    assert "❌ failed" in head and "tests FAIL" in head and "1 signal" in head


def test_repository_text_cannot_inject_markup_in_the_comment():
    r = report(
        signals=[("weakened", HOSTILE, "<script>alert(1)</script> | x")],
        commits=[("b" * 40, "<b>hi</b> [a](http://e.com)\n# head", HOSTILE)],
        changed=[HOSTILE], checks={HOSTILE: ("FAIL", "<iframe>")},
    )
    body = r.pr_comment()
    for raw in ("<script>", "<iframe>", "<img", "<b>", "](http://e.com)"):
        assert raw not in body.replace("<details>", "").replace("<summary>", "")
    # only our own tags and the one marker remain
    tags = set(re.findall(r"<[^>]+>", body))
    assert tags <= {"<details>", "</details>", "<summary>", "</summary>", COMMENT_MARKER}
    assert body.count(COMMENT_MARKER) == 1
    assert "\n# head" not in body  # a newline in a subject can't start a heading


def test_oversized_comment_is_truncated_but_keeps_the_marker():
    body = report(commits=[("c" * 40, "x" * 5000, None)] * 40, signals=[("k", "p", "d" * 5000)] * 40).pr_comment()
    assert len(body) < 65536 and body.rstrip().endswith(COMMENT_MARKER)


def test_annotations_for_signals_and_failures():
    r = report(checks={"tests": ("FAIL", "exit 1")}, signals=[("deleted", "tests/test_a.py", "lost 2 asserts")])
    lines = r.annotations()
    assert lines[0] == "::warning file=tests/test_a.py,line=1,title=Daedalus%3A deleted::lost 2 asserts"
    assert lines[1].startswith("::error title=Daedalus check failed::") and "check tests FAIL: exit 1" in lines[1]
    assert report().annotations() == []


def test_annotation_escaping_blocks_command_injection():
    r = report(checks={"t": ("FAIL", "boom\n::error::fake")}, signals=[("k", HOSTILE, "100%\r\n::warning::x")])
    lines = r.annotations()
    assert len(lines) == 2
    for line in lines:  # a newline would start a second, attacker-written command
        assert "\n" not in line and "\r" not in line
    w = lines[0]
    header = w.split("::")[1]
    assert header.count(",") == 2  # only the separators; the comma in the path is escaped
    assert "%2C" in header and "%3A" in header and "%0A" in header and "%25" in header
    assert w.endswith("::100%25%0D%0A::warning::x")


# --- action.yml --------------------------------------------------------------------------------------

ACTION = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
STEPS = ACTION["runs"]["steps"]


def test_inputs_exist_with_safe_defaults():
    ins = ACTION["inputs"]
    assert ins["comment"]["default"] == "true" and ins["annotations"]["default"] == "true"
    assert ins["fail-on"]["default"] == "blocked"


def test_no_expression_is_interpolated_into_a_script():
    for step in STEPS:
        assert "${{" not in step.get("run", ""), step.get("name")
    # every input reaches a script only through env
    envs = [v for s in STEPS for v in (s.get("env") or {}).values()]
    joined = " ".join(envs)
    for name in ("inputs.base", "inputs.annotations", "inputs.comment", "inputs.fail-on", "inputs.extra-args"):
        assert name in joined or name in " ".join(s.get("if", "") for s in STEPS), name


def test_never_runs_on_pull_request_target():
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    example = (ROOT / "examples" / "workflows" / "daedalus.yml").read_text(encoding="utf-8")
    assert "pull_request_target" not in re.sub(r"#.*", "", example)


def test_comment_step_updates_by_marker_and_survives_forks():
    step = next(s for s in STEPS if s.get("name") == "Pull request comment")
    assert "pull_request" in step["if"] and "inputs.comment" in step["if"]
    run = step["run"]
    assert COMMENT_MARKER in run and "PATCH" in run and "POST" in run
    assert "github-actions[bot]" in run  # only its own comment is updated
    assert "403" in run and "exit 0" in run and "IS_FORK" in run
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "gh api" in run


def test_token_is_only_given_to_the_comment_step():
    for s in STEPS:
        if s.get("name") != "Pull request comment":
            assert "GH_TOKEN" not in (s.get("env") or {}) and "github.token" not in str(s.get("env") or {})


def test_verdict_step_honours_fail_on():
    gate = next(s for s in STEPS if s.get("id") == "gate")
    assert "|| code=$?" in gate["run"]  # the comment is posted before the job fails
    verdict = next(s for s in STEPS if s.get("name") == "Verdict")
    assert verdict["env"]["FAIL_ON"] == "${{ inputs.fail-on }}" and "never" in verdict["run"]


def test_example_workflow_is_least_privilege():
    wf = yaml.safe_load((ROOT / "examples" / "workflows" / "daedalus.yml").read_text(encoding="utf-8"))
    assert wf[True] == {"pull_request": None}  # YAML reads `on` as True
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write"}
    uses = [s["uses"] for s in wf["jobs"]["gate"]["steps"]]
    assert any(u.startswith("KEV-INZHU/Daedalus@") for u in uses)
