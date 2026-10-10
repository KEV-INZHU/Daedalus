"""Routing step 1: per-session usage, quota readings, and cash kept apart from estimates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import HUMAN
from daedalus.adapters.base import AgentRequest, AgentResult, ScriptedStep
from daedalus.adapters.command import CommandAdapter, claude_quota
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core import run as ev
from daedalus.core.acceptance import budget_exhausted
from daedalus.core.errors import ContractError
from daedalus.core.metrics import collect
from daedalus.orchestration.lifecycle import run_task

INIT = {"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "apiKeySource": "none"}


@pytest.fixture(autouse=True)
def no_ambient_billing(monkeypatch):
    """Results must not depend on billing variables in the developer's environment."""
    from daedalus.adapters.command import BILLING_ENV_VARS

    for v in BILLING_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
RATE = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "rateLimitType": "seven_day",
        "utilization": 0.87,
        "resetsAt": 1791622800,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.06, "resetsAt": 1791618000},
            "seven_day": {"utilization": 0.87, "resetsAt": 1791622800},
        },
    },
}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "result": "done\n```json\n{\"summary\": \"ok\"}\n```",
    "total_cost_usd": 0.0123, "num_turns": 3, "duration_ms": 900,
    "usage": {"input_tokens": 2, "output_tokens": 40, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 2400},
    "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 40, "cacheReadInputTokens": 500,
                                        "cacheCreationInputTokens": 2400, "costUSD": 0.0123}},
}


def fake_stream(tmp_path: Path, *messages: object) -> CommandAdapter:
    """A CLI that prints one JSON message per line, as `--output-format stream-json` does."""
    script = tmp_path / "fake_stream.py"
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    body = chr(10).join(lines) + chr(10)
    # UTF-8 bytes, as the real CLI writes them, whatever the console code page
    script.write_text(f"import sys\nsys.stdin.read()\nsys.stdout.buffer.write({body!r}.encode('utf-8'))\n",
                      encoding="utf-8")
    return CommandAdapter("claude-code", [sys.executable, str(script)], parse="claude-json")


def request(tmp_path: Path) -> AgentRequest:
    return AgentRequest("r", "t", "brunel", "prompt", tmp_path, timeout_s=60)


def test_stream_session_reports_result_usage_and_quota(tmp_path):
    res = fake_stream(tmp_path, INIT, {"type": "assistant"}, RATE, RESULT).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and res.structured == {"summary": "ok"}
    assert res.cost == pytest.approx(0.0123) and res.cost_basis == "api_equivalent"
    assert res.usage["model"] == "claude-haiku-5-5"
    assert res.usage["cache_creation_input_tokens"] == 2400 and res.usage["output_tokens"] == 40
    assert res.usage["models"]["claude-haiku-5-5"]["outputTokens"] == 40
    assert res.quota["status"] == "allowed_warning"
    assert res.quota["windows"]["seven_day"] == {"utilization": 0.87, "resets_at": 1791622800.0}
    assert res.quota["windows"]["five_hour"]["utilization"] == 0.06


def test_garbage_lines_in_a_stream_are_skipped(tmp_path):
    res = fake_stream(tmp_path, "not json", INIT, "{broken", RESULT).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and res.quota is None


@pytest.mark.parametrize(
    "event",
    [
        {"type": "rate_limit_event"},
        {"type": "rate_limit_event", "rate_limit_info": "nope"},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": "x"}}}},
    ],
)
def test_malformed_quota_reading_is_unknown_not_headroom(tmp_path, event):
    res = fake_stream(tmp_path, INIT, event, RESULT).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and res.quota is None


def test_quota_falls_back_to_the_single_window_fields():
    info = dict(RATE["rate_limit_info"]); info.pop("unifiedWindows")
    q = claude_quota([{"type": "rate_limit_event", "rate_limit_info": info}], now=5.0)
    assert q["windows"] == {"seven_day": {"utilization": 0.87, "resets_at": 1791622800.0}} and q["observed_at"] == 5.0


def test_a_failed_stream_session_still_reports_quota(tmp_path):
    err = {**RESULT, "subtype": "error_max_turns", "is_error": True}
    res = fake_stream(tmp_path, INIT, RATE, err).run_agent(request(tmp_path))
    assert res.status == "FAILED" and res.quota is not None and res.usage is not None


def test_cash_and_estimates_are_ledgered_apart(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "review-a", "aristotle", AgentResult("COMPLETED", cost=0.5))
    ari.record_session(rid, "review-b", "aristotle", AgentResult("COMPLETED", cost=0.25, cost_basis="cash"))
    ari.record_session(rid, "review-c", "aristotle", AgentResult("COMPLETED", cost=0.125, cost_basis="credits?"))
    ari._append(rid, ev.COST_CHARGED, "system:ariadne", {"cost": 1.0})  # recorded before bases existed
    s = ari.state(rid)
    assert s.cost_used == pytest.approx(1.875)
    assert s.cash_used == pytest.approx(0.375)  # unknown basis counts as cash: money is never under-counted


def test_usage_and_quota_become_events(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    usage = {"model": "m", "input_tokens": 10, "output_tokens": 5}
    quota = {"source": "t", "status": "allowed", "windows": {"five_hour": {"utilization": 0.1, "resets_at": 9.0}}}
    ari.record_session(rid, "build-1", "brunel", AgentResult("COMPLETED", usage=usage, quota=quota, session_id="s1"))
    s = ari.state(rid)
    assert s.sessions[0]["model"] == "m" and s.sessions[0]["session_id"] == "s1" and s.sessions[0]["role"] == "brunel"
    assert s.quota["windows"]["five_hour"]["utilization"] == 0.1


def test_max_cash_defaults_to_zero_and_any_cash_exhausts_it(ari, clock):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    assert ari.state(rid).contract.budget.max_cash == 0.0
    ari.record_session(rid, "r", "aristotle", AgentResult("COMPLETED", cost=5.0))  # estimate: not cash
    assert not any(x.startswith("cash") for x in budget_exhausted(ari.state(rid), clock()))
    ari.record_session(rid, "r", "aristotle", AgentResult("COMPLETED", cost=0.01, cost_basis="cash"))
    assert any(x.startswith("cash") for x in budget_exhausted(ari.state(rid), clock()))


def test_max_cash_is_validated(ari):
    rid = ari.start({"objective": "x", "budget": {"max_cash": 2.5}}, actor=HUMAN)
    assert ari.state(rid).contract.budget.max_cash == 2.5
    ari.cancel(rid, actor=HUMAN)
    with pytest.raises(ContractError):
        ari.start({"objective": "x", "budget": {"max_cash": -1}}, actor=HUMAN)


def test_every_launch_site_records_usage(ari, repo):
    usage = {"model": "sim", "input_tokens": 100, "output_tokens": 10}

    def builder(req):
        (repo / "app.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult("COMPLETED", structured={"status": "done", "summary": "s"}, cost=0.2, usage=usage)

    review = AgentResult("COMPLETED", structured={"summary": "fine", "findings": []}, cost=0.1, usage=usage)
    adapter = SimulatedAdapter({"brunel": [builder], "aristotle": [lambda req: review]})
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter, mode="review")
    s = ari.state(report.run_id)
    assert [x["role"] for x in s.sessions] == ["brunel", "aristotle"]
    assert s.cost_used == pytest.approx(0.3) and s.cash_used == 0.0
    m = next(r for r in collect(ari.store)[0] if r.run_id == report.run_id)
    assert (m.sessions, m.input_tokens, m.output_tokens, m.cash) == (2, 200, 20, 0.0)


def test_legacy_scripted_steps_still_charge_estimates(ari, repo):
    adapter = SimulatedAdapter({"brunel": [ScriptedStep(edits={"app.txt": "ok\n"}, structured={"status": "done"}, cost=0.5)]})
    report = run_task(ari, {"objective": "make app ok"}, actor=HUMAN, adapter=adapter)
    s = ari.state(report.run_id)
    assert s.cost_used == pytest.approx(0.5) and s.cash_used == 0.0 and s.sessions == []


# ---------------------------------------------------------------- review follow-ups (run a2022cb79686)
class Metered:
    """Wraps an adapter so every session reports usage and a quota reading."""

    def __init__(self, inner, basis="api_equivalent"):
        self.inner, self.basis = inner, basis
        self.name, self.capabilities = "metered", inner.capabilities

    def run_agent(self, req):
        from dataclasses import replace

        res = self.inner.run_agent(req)
        quota = {"source": "t", "status": "allowed", "windows": {"seven_day": {"utilization": 0.5, "resets_at": 1.0}}}
        return replace(res, cost=0.01, cost_basis=self.basis, quota=quota,
                       usage={"model": "m", "input_tokens": 3, "output_tokens": 1, "task_id": "spoof", "basis": "spoof"})

    def cancel(self, session_id):
        return False


def test_planner_fan_out_workers_and_arbitration_record_usage(tmp_path, clock):
    from conftest import make_repo
    from daedalus.orchestration.ariadne import Ariadne

    two = {"policy": {"default_budget": {"max_attempts": 40, "max_wall_seconds": 86400, "max_cost": 50.0,
                                         "max_workers": 2}, "arbitration_resolves_findings": True}}
    root = make_repo(tmp_path / "r", two)
    a = Ariadne(root, clock=clock)
    plan = {"packages": [{"id": "a", "owned_paths": ["src/a/**"]}, {"id": "b", "owned_paths": ["app.txt"]}]}
    finding = {"title": "needs a test", "evidence": "app.txt", "severity": "BLOCKER", "required_change": "add one"}

    def brunel(req):
        if req.task_id.startswith("plan-"):
            return AgentResult("COMPLETED", structured=plan)
        if req.task_id == "r1-a":
            (Path(req.cwd) / "src/a").mkdir(parents=True, exist_ok=True)
            (Path(req.cwd) / "src/a/x.py").write_text("x = 1\n", encoding="utf-8")
        elif req.task_id == "r1-b":
            (Path(req.cwd) / "app.txt").write_text("ok\n", encoding="utf-8")
        else:  # round 2, in place: dispute the finding
            return AgentResult("COMPLETED", structured={"status": "done", "disputes": [{"finding": "F1", "reason": "out of scope"}]})
        return AgentResult("COMPLETED", structured={"status": "done", "summary": req.task_id})

    ruling = {"decision": "overrule", "rationale": "out of scope", "reversal_condition": "scope grows"}
    sim = SimulatedAdapter({"brunel": [brunel] * 4,
                            "aristotle": [lambda r: AgentResult("COMPLETED", structured={"summary": "s", "findings": [finding]})],
                            "plato": [lambda r: AgentResult("COMPLETED", structured=ruling)]})
    report = run_task(a, {"objective": "o", "budget": {"max_workers": 2}}, actor=HUMAN, adapter=Metered(sim), mode="review")
    s = a.state(report.run_id)
    tasks = [x["task_id"] for x in s.sessions]
    assert {"plan-1", "r1-a", "r1-b", "review-aristotle", "build-2", "arbitrate-F1"} <= set(tasks), tasks
    assert all(x["basis"] == "api_equivalent" for x in s.sessions)  # adapter usage cannot overwrite ledger fields
    assert "spoof" not in tasks
    quota_events = [e for e in a.store.events(report.run_id) if e.type == ev.QUOTA_OBSERVED]
    assert len(quota_events) == len(s.sessions)
    a.close()


def test_cash_shows_in_run_metrics_and_aggregate(ari, repo):
    from daedalus.core.metrics import aggregate

    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "r1", "aristotle", AgentResult("COMPLETED", cost=0.4))
    ari.record_session(rid, "r2", "aristotle", AgentResult("COMPLETED", cost=0.1, cost_basis="cash"))
    runs = collect(ari.store)[0]
    m = next(r for r in runs if r.run_id == rid)
    assert (m.cost, m.cash) == (0.5, 0.1)
    assert aggregate(runs).cash_total == 0.1 and aggregate(runs).cost_total == 0.5


def test_unreadable_adapter_usage_is_dropped_not_fatal(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "t", "brunel", AgentResult("COMPLETED", usage=["not", "a", "dict"], quota={"x": float("nan")}))
    s = ari.state(rid)
    assert all("dropped" in x for x in s.sessions) and s.quota is None
    assert next(r for r in collect(ari.store)[0] if r.run_id == rid).sessions == 0


@pytest.mark.parametrize(
    "info",
    [
        {"unifiedWindows": ["five_hour"]},
        {"unifiedWindows": {}},
        {"unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": 1}, "seven_day": {"utilization": "?"}}},
        {"unifiedWindows": {"five_hour": {"utilization": float("inf"), "resetsAt": 1}}},
        {"utilization": 0.5, "resetsAt": 1},  # no window name
    ],
)
def test_quota_parser_never_raises_and_rejects_partial_readings(info):
    assert claude_quota([{"type": "rate_limit_event", "rate_limit_info": info}], now=1.0) is None


@pytest.mark.parametrize("bad", [{"usage": [1, 2]}, {"modelUsage": [1]}, {"usage": {"output_tokens": 1e999}},
                                 {"usage": {"output_tokens": -5}}, {"modelUsage": {"m": "x"}}])
def test_usage_parser_never_raises(tmp_path, bad):
    res = fake_stream(tmp_path, INIT, {**RESULT, **bad}).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and min(v for k, v in res.usage.items() if k.endswith("_tokens")) >= 0


@pytest.mark.parametrize(
    ("init", "env", "basis"),
    [
        ({"apiKeySource": "none"}, {}, "api_equivalent"),  # a subscription login, nothing billed in sight
        ({"apiKeySource": "none"}, {"ANTHROPIC_API_KEY": "k"}, "cash"),  # any billing signal wins
        ({"apiKeySource": "none"}, {"ANTHROPIC_BASE_URL": "https://gateway"}, "cash"),
        ({"apiKeySource": "none"}, {"CLAUDE_CODE_USE_BEDROCK": "1"}, "cash"),
        ({"apiKeySource": "ANTHROPIC_API_KEY"}, {}, "cash"),
        ({}, {"ANTHROPIC_API_KEY": "k"}, "cash"),  # no init (plain json) and a key present: assume billed
        ({}, {}, "api_equivalent"),  # plain json output, nothing billed in sight
        ({"model": "m"}, {}, "cash"),  # an init message that does not say "none"
    ],
)
def test_cost_basis_follows_the_billing_source(init, env, basis):
    from daedalus.adapters.command import claude_cost_basis

    msgs = [{"type": "system", "subtype": "init", **init}] if init else []
    assert claude_cost_basis(msgs, env) == basis


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.5])
def test_max_cash_rejects_values_that_would_disable_or_invert_the_cap(ari, value):
    with pytest.raises(ContractError):
        ari.start({"objective": "x", "budget": {"max_cash": value}}, actor=HUMAN)


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1.0])
def test_nonsense_costs_never_reach_the_ledger(ari, cost, clock):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "t", "brunel", AgentResult("COMPLETED", cost=cost, cost_basis="cash"))
    s = ari.state(rid)
    assert (s.cost_used, s.cash_used) == (0.0, 0.0) and any("dropped" in x for x in s.sessions)
    ari.record_session(rid, "t", "brunel", AgentResult("COMPLETED", cost=0.01, cost_basis="cash"))
    assert any(x.startswith("cash") for x in budget_exhausted(ari.state(rid), clock()))  # the cap still works


@pytest.mark.parametrize("raw", ['"total_cost_usd": NaN', '"total_cost_usd": -3', '"total_cost_usd": 1e999'])
def test_adapter_reports_unusable_costs_as_zero(tmp_path, raw):
    line = json.dumps({**RESULT, "total_cost_usd": 0}).replace('"total_cost_usd": 0', raw)
    res = fake_stream(tmp_path, INIT, line).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and res.cost == 0.0


def test_huge_integers_in_usage_never_raise(tmp_path):
    line = json.dumps(RESULT).replace('"output_tokens": 40', '"output_tokens": 1' + "0" * 400)
    res = fake_stream(tmp_path, INIT, line).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and res.usage["output_tokens"] == 0


def test_line_separators_inside_json_strings_do_not_break_the_stream(tmp_path):
    seps = "a\u2028b\u2029c\x85d"  # a Node CLI leaves these unescaped inside JSON strings
    line = json.dumps({**RESULT, "result": seps}, ensure_ascii=False)
    res = fake_stream(tmp_path, INIT, line).run_agent(request(tmp_path))
    assert res.status == "COMPLETED" and res.output == seps


def test_stopped_sessions_record_an_unknown_cost(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "build-1", "brunel", AgentResult("TIMED_OUT", error="agent exceeded 1s"))
    s = ari.state(rid)
    assert s.sessions[-1]["cost_unknown"] is True and s.cost_used == 0.0
    m = next(r for r in collect(ari.store)[0] if r.run_id == rid)
    assert (m.sessions, m.unknown_cost_sessions) == (0, 1)


def test_adapters_cannot_spoof_the_dropped_marker(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "t", "brunel", AgentResult("COMPLETED", usage={"model": "m", "dropped": "x", "output_tokens": 3}))
    m = next(r for r in collect(ari.store)[0] if r.run_id == rid)
    assert m.sessions == 1 and m.output_tokens == 3


def test_a_billed_session_with_an_unknown_cost_exhausts_max_cash(ari, clock):
    rid = ari.start({"objective": "x", "budget": {"max_cash": 5.0}}, actor=HUMAN)
    ari.record_session(rid, "build-1", "brunel", AgentResult("TIMED_OUT", cost_basis="cash"))
    assert ari.state(rid).cash_unknown
    assert any(x.startswith("cash unknown") for x in budget_exhausted(ari.state(rid), clock()))


def test_an_unbilled_stopped_session_is_not_cash(ari, clock):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "build-1", "brunel", AgentResult("CANCELLED"))
    assert not ari.state(rid).cash_unknown
    assert not any(x.startswith("cash") for x in budget_exhausted(ari.state(rid), clock()))


def test_failed_sessions_without_a_report_record_the_gap(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "review-x", "aristotle", AgentResult("FAILED", error="unparseable"))
    assert ari.state(rid).sessions[-1]["cost_unknown"] is True


def test_adapters_cannot_set_the_unknown_cost_marker(ari):
    rid = ari.start({"objective": "x"}, actor=HUMAN)
    ari.record_session(rid, "t", "brunel", AgentResult("COMPLETED", usage={"model": "m", "cost_unknown": True},
                                                       cost_basis="cash"))
    assert not ari.state(rid).cash_unknown


def test_a_stopped_claude_session_takes_its_basis_from_the_environment(tmp_path, monkeypatch):
    import sys as _sys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    script = tmp_path / "slow.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    ad = CommandAdapter("claude-code", [_sys.executable, str(script)], parse="claude-json")
    res = ad.run_agent(AgentRequest("r", "t", "brunel", "p", tmp_path, timeout_s=1))
    assert res.status == "TIMED_OUT" and res.cost_basis == "cash"
