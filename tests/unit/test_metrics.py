"""Metrics are a read-only fold over the audit log (spec §17, §19)."""

from __future__ import annotations

import json

import pytest

from conftest import BUILDER, HUMAN, fix
from daedalus.core.metrics import aggregate, run_metrics


def metrics(ari):
    return {rid: run_metrics(ari.store.events(rid)) for rid in ari.store.run_ids()}


def test_run_metrics_come_from_recorded_events(ari, repo, clock):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.verify(rid)  # FAIL on the first candidate
    fix(repo)
    ari.verify(rid)  # PASS on the second
    ari.charge(rid, cost=0.75)
    ari.submit_review(
        rid,
        reviewer="agent:rev",
        perspective="aristotle",
        findings=[{"title": "t", "evidence": "e", "severity": "MINOR"}],
        source="launched",
    )
    clock.advance(120)
    ari.finish(rid)
    m = metrics(ari)[rid]
    assert m.disposition == "ACCEPTED"
    assert m.wall_seconds == pytest.approx(120)
    assert m.cost == pytest.approx(0.75)
    assert m.candidates_verified == 2 and m.check_failures == 1
    assert m.reviews == 1 and m.findings == {"MINOR": 1}
    assert not m.first_pass


def test_first_pass_and_human_interventions(ari, repo):
    rid = ari.start({"objective": "o", "acceptance_criteria": ["looks right"]}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.attest(rid, "C1", "pass", actor=HUMAN, evidence="checked")
    ari.finish(rid)
    m = metrics(ari)[rid]
    assert m.first_pass
    assert m.human_interventions == 1  # the attestation; creating the run is not an intervention


def test_aggregate_excludes_open_runs_from_rates(ari, repo):
    a = ari.start({"objective": "accepted"}, actor=HUMAN)
    fix(repo)
    ari.verify(a)
    ari.finish(a)
    b = ari.start({"objective": "rejected", "budget": {"max_attempts": 1}}, actor=HUMAN)
    ari.verify(b)
    ari.raise_blocker(b, "stuck", actor=BUILDER)
    ari.finish(b)
    ari.start({"objective": "still open"}, actor=HUMAN)
    agg = aggregate(metrics(ari).values())
    assert (agg.runs, agg.open, agg.accepted, agg.rejected) == (3, 1, 1, 1)
    assert agg.acceptance_rate == 0.5
    assert agg.first_pass_rate == 0.5
    assert agg.rework_per_accepted == 0


def test_empty_log_has_no_rates():
    agg = aggregate([])
    assert agg.acceptance_rate is None and agg.cost_per_accepted is None


def test_metrics_never_write_to_the_log(ari, repo, capsys):
    from daedalus import cli

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    before = len(ari.store.events())
    run_metrics(ari.store.events(rid))
    assert cli.main(["-C", str(repo), "metrics"]) == 0
    assert "acceptance rate" in capsys.readouterr().out
    assert len(ari.store.events()) == before


def test_cli_json_matches_table_data(ari, repo, capsys):
    from daedalus import cli

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    fix(repo)
    ari.verify(rid)
    ari.finish(rid)
    assert cli.main(["-C", str(repo), "metrics", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["aggregate"]["accepted"] == 1
    assert doc["runs"][0]["run_id"] == rid and doc["runs"][0]["first_pass"] is True


def test_abandon_counts_as_one_intervention(ari, repo):
    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.abandon(rid, actor=HUMAN, reason="requirement withdrawn")
    m = metrics(ari)[rid]
    assert m.disposition == "REJECTED" and m.human_interventions == 1


def test_cancel_failed_review_stop_block_and_recovery_are_counted(ari, repo):
    from daedalus.core import run as ev

    rid = ari.start({"objective": "o"}, actor=HUMAN)
    ari.record_failed_review(rid, "aristotle", "no JSON")
    ari.note_stop_blocked(rid, ["[agent] check failed"])
    ari._append(rid, ev.RECOVERY, "system:test", {"notes": ["drill"]})
    ari.cancel(rid, actor=HUMAN, reason="stop")
    m = metrics(ari)[rid]
    assert (m.disposition, m.failed_reviews, m.stop_blocks, m.recoveries) == ("CANCELLED", 1, 1, 1)
    assert m.human_interventions == 1  # the cancel request
    agg = aggregate([m])
    assert agg.cancelled == 1 and agg.acceptance_rate == 0.0 and agg.cost_per_accepted is None


def test_latency_and_cost_per_accepted_across_runs(ari, repo, clock):
    for minutes, cost in ((2, 1.0), (6, 2.0)):
        rid = ari.start({"objective": "o"}, actor=HUMAN)
        fix(repo)
        ari.verify(rid)
        ari.charge(rid, cost=cost)
        clock.advance(minutes * 60)
        ari.finish(rid)
    rid = ari.start({"objective": "fails", "budget": {"max_attempts": 1}}, actor=HUMAN)
    (repo / "app.txt").write_text("bad\n", encoding="utf-8")
    ari.verify(rid)
    ari.charge(rid, cost=3.0)
    ari.raise_blocker(rid, "stuck", actor=BUILDER)
    ari.finish(rid)
    open_run = ari.start({"objective": "open"}, actor=HUMAN)
    ari.charge(open_run, cost=10.0)
    agg = aggregate(metrics(ari).values())
    assert agg.median_latency_accepted_s == pytest.approx(240)
    assert agg.cost_per_accepted == pytest.approx(3.0)  # (1 + 2 + 3) over 2 accepted; open spend excluded
    assert agg.cost_total == pytest.approx(16.0)


def test_unreplayable_run_is_reported_not_fatal(ari, repo):
    from daedalus.core import run as ev
    from daedalus.core.metrics import collect

    good = ari.start({"objective": "o"}, actor=HUMAN)
    ari.cancel(good, actor=HUMAN)
    ari.store.append("brokenrun", ev.EXECUTION, "system:x", {"from": "RUNNING", "to": "FINISHED"}, 0.0)
    runs, errors = collect(ari.store)
    assert [m.run_id for m in runs] == [good] and "brokenrun" in errors
