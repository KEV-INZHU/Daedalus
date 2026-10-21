"""Follow-up fixes: the CI verdict file, the sticky comment, fork detection, report attribution and
escaping, hosted-reviewer cost, base_url hygiene, symlinks and the context budget."""

from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from conftest import HUMAN, fix, git, make_repo
from daedalus import cli
from daedalus.adapters import make_adapter
from daedalus.adapters.base import AgentResult
from daedalus.adapters.openai_compat import HttpReviewAdapter, _Failure, build_review_prompt, is_local_url
from daedalus.ci import COMMENT_MARKER, CIReport, _tier_source
from daedalus.core.errors import CapabilityError
from daedalus.core.risk import RiskAssessment, RiskContribution
from daedalus.orchestration.lifecycle import refuse_unaffordable
from daedalus.report import build_report, render_html, render_markdown
from daedalus.report.markdown import esc
from daedalus.report.model import FindingRow

ROOT = Path(__file__).resolve().parents[2]
STEPS = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))["runs"]["steps"]


def step(name: str) -> dict:
    return next(s for s in STEPS if s.get("name") == name)


def ci_report(**kw) -> CIReport:
    base = dict(base="origin/main", changed=["a.py"], tier="high", risk="tier high", reviews=(), approvals=(),
                checks={"tests": ("PASS", "")}, commits=[("a" * 40, "x", "r1")], tier_source="path")
    base.update(kw)
    return CIReport(**base)


# ------------------------------------------------------------- F2 / F4: the verdict file
def branch(root, edit=fix):
    git(root, "checkout", "-q", "-b", "feature")
    edit(root)
    git(root, "commit", "-qam", "change")


def test_result_file_says_passed_blocked_or_error(tmp_path, monkeypatch):
    import json

    root = make_repo(tmp_path / "r")
    branch(root)
    out = tmp_path / "result.json"
    assert cli.main(["--dir", str(root), "ci", "--base", "main", "--result-out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "passed" and len(data["head_sha"]) == 40
    git(root, "checkout", "-q", "main")
    git(root, "checkout", "-q", "-B", "bad")
    (root / "app.txt").write_text("nope\n")
    git(root, "commit", "-qam", "break")
    assert cli.main(["--dir", str(root), "ci", "--base", "main", "--result-out", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "blocked"
    # a gate error (the base is not there) is an error verdict, exit 2
    assert cli.main(["--dir", str(root), "ci", "--base", "nope", "--result-out", str(out)]) == 2
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "error"


def test_a_crash_writes_an_error_verdict_and_still_raises(tmp_path, monkeypatch):
    import json

    import daedalus.ci as ci

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(ci, "run_ci", boom)
    out = tmp_path / "result.json"
    with pytest.raises(RuntimeError):
        cli.main(["--dir", str(tmp_path), "ci", "--base", "main", "--result-out", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "error" and "RuntimeError" in data["detail"]


def test_unwritable_markdown_out_is_a_gate_error_not_a_traceback(tmp_path, capsys):
    root = make_repo(tmp_path / "r")
    branch(root)
    code = cli.main(["--dir", str(root), "ci", "--base", "main", "--markdown-out", str(tmp_path / "no" / "such" / "c.md")])
    assert code == 2 and "cannot write the comment body" in capsys.readouterr().err


def test_headline_names_the_head_sha_in_every_state():
    sha = "0123456789abcdef" * 2 + "01234567"
    for r in (ci_report(head_sha=sha), ci_report(head_sha=sha, checks={"t": ("FAIL", "exit 1")}),
              ci_report(head_sha=sha, require_provenance=True, commits=[("b" * 40, "x", None)])):
        assert "@ `0123456789`" in r.headline()
    assert "@" not in ci_report().headline()


def run_gate_step(tmp_path, fake_daedalus: str, *, code_env: dict | None = None) -> dict[str, str]:
    """Run the action's Gate step in bash with a stand-in `daedalus` and return its step outputs."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "daedalus"
    exe.write_text("#!/bin/bash\n" + fake_daedalus + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    outputs = tmp_path / "out.txt"
    outputs.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin",
        "BASE_INPUT": "", "BASE_REF": "main", "REQUIRE_PROVENANCE": "false", "EXTRA_ARGS": "",
        "ANNOTATIONS": "false", "WANT_COMMENT": "true", "EVENT_NAME": "pull_request",
        "RUNNER_TEMP_DIR": str(tmp_path), "PR_HEAD_SHA": "f" * 40, "GITHUB_OUTPUT": str(outputs),
        **(code_env or {}),
    }
    r = subprocess.run(["bash", "-eo", "pipefail", "-c", step("Gate")["run"]], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = [ln.split("=", 1) for ln in outputs.read_text().splitlines()]
    return {k: v for k, v in lines}  # the last write of a key wins


WRITE = 'while [ $# -gt 0 ]; do case "$1" in --result-out) f="$2";; esac; shift; done; '


def test_gate_step_trusts_the_result_file_not_exit_code_one(tmp_path):
    crash = run_gate_step(tmp_path, "echo Traceback >&2; exit 1")  # a Python crash exits 1 and writes nothing
    assert crash["verdict"] == "error" and crash["exit-code"] == "1"
    blocked = run_gate_step(tmp_path, WRITE + 'echo \'{"verdict": "blocked"}\' > "$f"; exit 1')
    assert blocked["verdict"] == "blocked"
    passed = run_gate_step(tmp_path, WRITE + 'echo \'{"verdict": "passed"}\' > "$f"; exit 0')
    assert passed["verdict"] == "passed"
    # a verdict that disagrees with the exit code, or garbage, is an error
    assert run_gate_step(tmp_path, WRITE + 'echo \'{"verdict": "passed"}\' > "$f"; exit 1')["verdict"] == "error"
    assert run_gate_step(tmp_path, WRITE + 'echo nonsense > "$f"; exit 0')["verdict"] == "error"
    # a stale result from an earlier run is removed before the gate runs
    (tmp_path / "daedalus-result.json").write_text('{"verdict": "passed"}')
    assert run_gate_step(tmp_path, "exit 0")["verdict"] == "error"


def run_verdict_step(verdict: str, fail_on: str = "blocked") -> int:
    env = {"PATH": "/usr/bin:/bin", "VERDICT": verdict, "FAIL_ON": fail_on, "EXIT_CODE": "1"}
    return subprocess.run(["bash", "-eo", "pipefail", "-c", step("Verdict")["run"]], env=env,
                          capture_output=True, text=True).returncode


def test_verdict_step_fail_on_never_cannot_turn_an_error_green():
    assert run_verdict_step("passed") == 0
    assert run_verdict_step("blocked") == 1 and run_verdict_step("blocked", "never") == 0
    assert run_verdict_step("error", "never") == 1 and run_verdict_step("", "never") == 1


def test_comment_step_replaces_a_stale_pass_with_an_error_body(tmp_path):
    run = step("Pull request comment")["run"]
    head = run.split('if [ "$IS_FORK"')[0]  # the part that decides the body
    comment = tmp_path / "c.md"
    comment.write_text("Daedalus CI: ✅ passed\n" + COMMENT_MARKER + "\n")  # as if left over
    env = {"PATH": "/usr/bin:/bin", "VERDICT": "error", "COMMENT_FILE": str(comment), "PR_HEAD_SHA": "abcdef0123456789"}
    subprocess.run(["bash", "-eo", "pipefail", "-c", head], env=env, check=True)
    body = comment.read_text(encoding="utf-8")
    assert "error running the gate" in body and "abcdef0123" in body and COMMENT_MARKER in body
    assert "passed" not in body
    comment.unlink()  # a missing report is an error body too, even when the verdict claims otherwise
    subprocess.run(["bash", "-eo", "pipefail", "-c", head], env={**env, "VERDICT": "passed"}, check=True)
    assert "error running the gate" in comment.read_text(encoding="utf-8")


# ------------------------------------------------------------- F3 / F10: the comment step
def test_fork_means_a_different_head_repository_not_the_fork_flag():
    env = step("Pull request comment")["env"]
    assert env["IS_FORK"] == "${{ github.event.pull_request.head.repo.full_name != github.repository }}"
    assert "repo.fork" not in str(env)
    assert "403" in step("Pull request comment")["run"]  # the fallback stays


def test_first_marker_comment_is_taken_without_head_under_pipefail():
    run = step("Pull request comment")["run"]
    code = "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))
    assert "| head -n 1" not in code
    assert "ids=" in run and "${ids%%$'\\n'*}" in run
    # the shell expansion takes the first of several ids
    out = subprocess.run(["bash", "-c", "ids=$'11\\n22\\n33'; id=\"${ids%%$'\\n'*}\"; echo $id"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "11"


# ------------------------------------------------------------- F11 / F17
def test_truncated_comment_closes_details_and_never_cuts_an_entity():
    r = ci_report(commits=[("c" * 40, "x" * 4000 + "&|<", None)] * 49,
                  signals=[("k", "p", "d" * 3000 + "|")] * 49)
    for limit in (5000, 17000, 30000):
        import daedalus.ci as ci

        old = ci._MAX_COMMENT
        ci._MAX_COMMENT = limit
        try:
            body = r.pr_comment()
        finally:
            ci._MAX_COMMENT = old
        assert len(body) <= limit + 10
        assert body.count("<details>") == body.count("</details>")
        assert body.rstrip().endswith(COMMENT_MARKER) and body.count(COMMENT_MARKER) == 1
        assert not re.search(r"&#?\w*$", body.split("\n\n(truncated)")[0])  # no half entity at the cut
        assert "(truncated)" in body


def test_tier_source_follows_the_effective_tier():
    def risk(effective, computed, *contribs):
        return RiskAssessment(effective, computed, tuple(contribs))

    path = RiskContribution("path", "auth", "high", "a.py")
    assert _tier_source(risk("high", "high", RiskContribution("declared", "contract", "low"), path)) == "path"
    # effective above everything that contributed: say it was escalated, never borrow a lower tier's source
    assert _tier_source(risk("critical", "high", path)) == "escalated"
    assert _tier_source(risk("low", "low", RiskContribution("declared", "contract", "low"))) == ""
    assert "(escalated)" in ci_report(tier_source="escalated").headline()
    assert "floor" not in ci_report(tier_source="escalated").headline()
    assert "(behavioral floor)" in ci_report(tier_source="behavioral").headline()


# ------------------------------------------------------------- F5 / F12 / F13 / F18: report
def test_a_finished_run_does_not_borrow_the_live_trees_signals(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    open_report = build_report(ari, rid)
    assert open_report.tamper_known and "not recorded" not in render_markdown(open_report).lower()
    ari.finish(rid)
    r = build_report(ari, rid)
    assert r.final and not r.tamper_known and r.tamper_signals == []
    assert "Not recorded" in render_markdown(r) and "Not recorded" in render_html(r)
    assert r.tier_reason == "recorded when the run finished"


def test_review_model_is_matched_by_session_not_by_order(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.record_session(rid, "review-aristotle", "aristotle",
                       AgentResult("FAILED", session_id="aaaaaaaa1111", usage={"model": "model-of-the-failed-one"}))
    ari.record_session(rid, "review-aristotle", "aristotle",
                       AgentResult("COMPLETED", session_id="bbbbbbbb2222", usage={"model": "model-that-reviewed"}))
    ari.submit_review(rid, reviewer="agent:reviewer:aristotle:bbbbbbbb", perspective="aristotle",
                      source="launched", findings=[])
    assert [v.model for v in build_report(ari, rid).reviews] == ["model-that-reviewed"]


def test_every_list_is_capped_not_only_findings():
    from test_report import sample
    from daedalus.report.model import CheckRow, HumanAct, ReviewRow, SignalRow

    n = 700
    r = sample(
        checks=[CheckRow(f"c{i}", ["x" * 100], "PASS", "PASS", 1.0, 0.0, "c", "v", True) for i in range(n)],
        reviews=[ReviewRow(f"R{i}", "p" * 80, "full", "r", "m" * 80, "c", False, {"MAJOR": 1}) for i in range(n)],
        tamper_signals=[SignalRow("k" * 20, "p" * 100, "d" * 200) for _ in range(n)],
        human_acts=[HumanAct("approval", "h" * 50, 0.0, "d" * 200) for _ in range(n)],
        findings=[FindingRow(f"F{i}", "t" * 190, "MINOR", "open", "p", "R1", "y" * 190) for i in range(n)],
    )
    md = render_markdown(r)
    assert len(md) <= 60_000 and md.count("<details") == md.count("</details>")
    assert "more" in md and md.rstrip().endswith("</sub>")
    # as a last resort the text is cut on a line with its sections closed
    tiny = render_markdown(r, max_chars=1500)
    assert len(tiny) <= 1500 and tiny.count("<details") == tiny.count("</details>") and "cut to fit" in tiny


def test_control_characters_and_autolinks_are_neutralised():
    hostile = "\x1b[31mred\x1b[0m \x07bell \u202eevil https://evil.example/x www.evil.example mailto:a@b.co"
    out = esc(hostile)
    assert "\x1b" not in out and "\x07" not in out and "\u202e" not in out
    assert "://" not in re.sub("://\u200b", "", out) and "www.\u200b" in out
    from daedalus.report.page import h

    assert "\x1b" not in h(hostile) and "\x07" not in h(hostile)
    md = render_markdown(__import__("test_report").sample(objective=hostile))
    assert "\x1b" not in md and not re.search(r"https?://[^\u200b]", md)


# ------------------------------------------------------------- F14: init --preset on an existing config
def test_init_says_when_a_preset_was_not_applied(tmp_path, capsys):
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--preset", "strict"]) == 0
    out = capsys.readouterr().out
    assert "already exists" in out and "NOT applied" in out and "preset: strict" in out
    assert "daedalus policy --preset strict --diff" in out


# ------------------------------------------------------------- F7 / F8 / F9: the adapter
@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434/v1", True), ("http://127.0.0.1/v1", True), ("http://127.8.8.8/v1", True),
    ("http://[::1]:8080/v1", True), ("http://0.0.0.0:1/v1", True), ("http://[::ffff:127.0.0.1]/v1", True),
    ("http://127.evil.example/v1", False), ("https://api.openai.com/v1", False), ("http://10.0.0.5/v1", False),
    ("http://localhost.evil.example/v1", False), ("http://127.0.0.1.evil.example/v1", False),
])
def test_is_local_url_parses_the_host(url, local):
    assert is_local_url(url) is local


@pytest.mark.parametrize("url", ["https://user:pass@host/v1", "https://:tok@host/v1", "https://user@host/v1",
                                 "https://host/v1?api_key=sk-1", "https://host/v1#frag"])
def test_base_url_with_credentials_or_query_is_refused(url):
    with pytest.raises(CapabilityError, match="base_url") as e:
        make_adapter({"name": "openai-compatible", "base_url": url, "review_model": "m"})
    assert "user:pass@host" not in str(e.value) and "sk-1" not in str(e.value) and "tok" not in str(e.value).replace("token", "")


def test_a_429_response_is_closed(monkeypatch):
    closed = []

    class Err(urllib.error.HTTPError):
        def close(self):
            closed.append(True)
            super().close()

    class Opener:
        def open(self, req, timeout=None):
            raise Err("http://x", 429, "slow down", {"Retry-After": "0"}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    a = make_adapter({"name": "openai-compatible", "base_url": "http://localhost:1/v1", "review_model": "m"})
    assert a._post(b"{}", {}, 1.0) == 0.0 and closed
    closed.clear()

    class Err500(Err):
        pass

    class Opener500:
        def open(self, req, timeout=None):
            raise Err500("http://x", 500, "boom", {}, io.BytesIO(b"oops"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener500())
    with pytest.raises(_Failure, match="HTTP 500"):
        a._post(b"{}", {}, 1.0)
    assert closed


# ------------------------------------------------------------- F6: hosted reviewers without prices
def hosted(**extra) -> HttpReviewAdapter:
    return make_adapter({"name": "openai-compatible", "review_model": "gpt-x", "api_key_env": "K", **extra})


def test_hosted_adapter_without_prices_is_cash_with_unknown_cost():
    a = hosted()
    assert a.cost_untracked and a.expected_cost_basis() == "cash"
    assert not hosted(cost_per_mtok={"input": 1, "output": 2}).cost_untracked
    local = make_adapter({"name": "openai-compatible", "base_url": "http://localhost:1/v1", "review_model": "m"})
    assert not local.cost_untracked and local.expected_cost_basis() is None  # local stays a free estimate


def test_unpriced_hosted_review_is_recorded_as_cash_unknown_and_the_budget_fails_closed(ari, repo, monkeypatch, tmp_path):
    from test_openai_compat import Stub, adapter, review_request

    from daedalus.core.errors import BudgetExhausted
    from daedalus.core.metrics import run_metrics

    stub = Stub()
    try:
        a = adapter(stub)
        monkeypatch.setattr(HttpReviewAdapter, "local", property(lambda self: False))  # as if it were hosted
        res = a.run_agent(review_request(tmp_path))
        assert res.status == "COMPLETED" and res.cost_unknown and res.cost_basis == "cash"
        rid = ari.start({"objective": "o"}, actor=HUMAN)
        state = ari.state(rid)
        with pytest.raises(BudgetExhausted):  # max_cash is 0 by default: refused before anything is billed
            refuse_unaffordable(state, a, "a review")
        ari.record_session(rid, "review-aristotle", "aristotle", res)
        state = ari.state(rid)
        assert state.cash_unknown
        m = run_metrics(ari.store.events(rid))
        assert m.unknown_cost_sessions == 1 and m.input_tokens == 1000 and m.sessions == 1
        with pytest.raises(BudgetExhausted):  # and stays refused, whatever max_cash says
            refuse_unaffordable(state, a, "a review")
        # a priced hosted adapter is an ordinary cash session
        priced = adapter(stub, cost_per_mtok={"input": 1, "output": 1})
        res2 = priced.run_agent(review_request(tmp_path))
        assert not res2.cost_unknown and res2.cost > 0
        # a local, unpriced one stays a free estimate
        monkeypatch.undo()
        assert not adapter(stub).run_agent(review_request(tmp_path)).cost_unknown
    finally:
        stub.server.shutdown()


def test_doctor_warns_when_a_hosted_reviewer_has_no_prices(tmp_path, monkeypatch):
    from test_openai_compat import doctor_repo, findings

    monkeypatch.setenv("DAEDALUS_TEST_KEY", "x")
    cfg = {"name": "openai-compatible", "review_model": "gpt-x", "api_key_env": "DAEDALUS_TEST_KEY"}
    f = findings(doctor_repo(tmp_path, cfg))
    assert f["review cost tracking"].warn and "cost_per_mtok" in f["review cost tracking"].fix
    priced = findings(doctor_repo(tmp_path / "p", {**cfg, "cost_per_mtok": {"input": 1, "output": 2}}))
    assert "review cost tracking" not in priced
    local = findings(doctor_repo(tmp_path / "l", {"name": "openai-compatible", "base_url": "http://localhost:1/v1",
                                                   "review_model": "m"}))
    assert "review cost tracking" not in local


# ------------------------------------------------------------- F16 / F24: the review context
def test_a_prompt_that_leaves_no_room_for_the_diff_fails_clearly(tmp_path):
    from test_openai_compat import Stub, adapter, review_request

    prompt = "Instructions. " * 400 + "\n```diff\n" + "+x\n" * 3000 + "\n```"
    with pytest.raises(_Failure, match="max_context_chars"):
        build_review_prompt(prompt, tmp_path, 2000, ())
    stub = Stub()
    try:
        res = adapter(stub, max_context_chars=2000).run_agent(review_request(tmp_path, prompt))
        assert res.status == "FAILED" and "max_context_chars" in (res.error or "") and "too small" in res.error
        assert stub.requests == []  # nothing was sent
    finally:
        stub.server.shutdown()


def test_the_prompt_stays_near_the_budget_including_the_notes(tmp_path):
    root = tmp_path / "ctx"
    root.mkdir()
    for n in range(20):
        (root / f"f{n}.py").write_text("x" * 5000)
    names = tuple(f"f{n}.py" for n in range(20))
    prompt = "Review.\n```diff\n" + "+y\n" * 4000 + "\n```"
    out = build_review_prompt(prompt, root, 6000, names)
    assert len(out) <= 6000 + 100 and "the diff was cut" in out


def test_symlinks_among_the_changed_paths_are_never_followed(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('ok')\n")
    (root / ".env").write_text("API_KEY=TOPSECRET")
    try:
        (root / "notes.txt").symlink_to(".env")
        (root / "alias").symlink_to("src", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    prompt = "Review.\n```diff\n+x\n```"
    out = build_review_prompt(prompt, root, 100_000, ("notes.txt", "alias/a.py", "src/a.py"))
    assert "TOPSECRET" not in out and "### notes.txt" not in out and "### alias/a.py" not in out
    assert "### src/a.py" in out  # a regular file still goes in
