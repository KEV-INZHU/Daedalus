"""Metrics: what review spend bought, and what running packages in parallel saved."""

from __future__ import annotations

from test_fanout import PLAN, SPEC, TWO_WORKERS, WORK, ScriptedBrunel

from conftest import BUILDER, HUMAN, make_repo
from daedalus.core.metrics import aggregate, collect, run_metrics
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import run_task

FINDING = {"severity": "MINOR", "title": "t", "evidence": "e"}


def test_review_spend_by_perspective(repo, ari):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    (repo / "app.txt").write_text("ok\n", encoding="utf-8")
    ari.record_candidate(rid, actor=BUILDER)
    ari.charge(rid, cost=0.6, note="aristotle review-aristotle")
    ari.charge(rid, cost=0.4, note="aristotle review-aristotle")
    ari.charge(rid, cost=0.3, note="mozi review-mozi")
    ari.charge(rid, cost=9.0, note="brunel build-1")  # building is not review spend
    ari.submit_review(
        rid, reviewer="agent:reviewer:aristotle:a", perspective="aristotle", findings=[FINDING] * 4
    )
    ari.submit_review(rid, reviewer="agent:reviewer:mozi:m", perspective="mozi")
    m = run_metrics(ari.store.events(rid))
    assert m.review_spend == {"aristotle": {"cost": 1.0, "findings": 4}, "mozi": {"cost": 0.3, "findings": 0}}
    agg = aggregate([m])
    assert agg.review_spend["aristotle"]["cost_per_finding"] == 0.25
    assert agg.review_spend["mozi"]["cost_per_finding"] is None  # spend that bought no findings


INDEPENDENT = {"packages": [
    {"id": "a", "description": "module a", "owned_paths": ["src/a/**"]},
    {"id": "b", "description": "the app", "owned_paths": ["app.txt"]},
]}
INDEPENDENT_WORK = {"a": {"src/a/x.py": "x = 1\n"}, "b": {"app.txt": "ok\n"}}


def fan_out_metrics(tmp_path, plan, work, rendezvous):
    root = make_repo(tmp_path / "r", TWO_WORKERS)
    ari = Ariadne(root)  # real time: the workers really overlap
    try:
        report = run_task(ari, SPEC, actor=HUMAN, adapter=ScriptedBrunel(plan, work, rendezvous=rendezvous))
        runs, _ = collect(ari.store)
        return run_metrics(ari.store.events(report.run_id)), aggregate(runs)
    finally:
        ari.close()


def test_independent_packages_save_time(tmp_path):
    m, agg = fan_out_metrics(tmp_path, INDEPENDENT, INDEPENDENT_WORK, {"a", "b"})
    assert m.parallel_workers == 2 and m.integration_failures == 0
    assert m.parallel_span_s > 0 and m.parallel_saved_s > 0  # a and b ran side by side
    assert m.integration_s >= 0
    assert agg.parallel_runs == 1 and agg.parallel_saved_s == m.parallel_saved_s


def test_a_dependency_chain_can_cost_more_than_it_saves(tmp_path):
    # c waits for a to finish and be integrated, so the round spans more than the work itself:
    # the metric says so (serial minus span may be negative) rather than claiming a saving.
    m, _ = fan_out_metrics(tmp_path, PLAN, WORK, {"a", "b"})
    assert m.parallel_workers == 3
    assert m.parallel_saved_s == round(m.parallel_serial_s - m.parallel_span_s, 3)


def test_in_place_runs_report_no_parallel_work(repo, ari):
    from conftest import fix

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    m = run_metrics(ari.store.events(rid))
    assert m.parallel_workers == 0 and m.parallel_saved_s == 0
    assert "parallel_saved_s" in m.to_dict()


def test_cli_prints_spend(repo, ari, capsys):
    from daedalus.cli import main

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.charge(rid, cost=0.5, note="aristotle review-aristotle")
    assert main(["-C", str(repo), "metrics"]) == 0
    assert "review spend: quality review (Aristotle) 0.50 for 0 (no findings)" in capsys.readouterr().out
