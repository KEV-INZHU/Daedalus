"""The cross-run evidence cache: a byte-identical tree is verified and reviewed once."""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from conftest import CHECK, HUMAN, fix, git, make_repo
from daedalus.adapters.base import ScriptedStep
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.ci import run_ci
from daedalus.core import run as ev
from daedalus.core.acceptance import review_satisfied
from daedalus.core.errors import AuditIntegrityError, PolicyError
from daedalus.core.metrics import aggregate, run_metrics
from daedalus.core.state_machine import CheckState
from daedalus.orchestration import prompts
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import launch_review
from daedalus.verification.evidence_cache import check_key, review_key

CLEAN = {"summary": "fine", "findings": []}
BLOCKER = {"title": "data loss", "evidence": "lib.py:1", "severity": "BLOCKER", "required_change": "do not"}
MINOR = {"title": "naming", "evidence": "lib.py:1", "severity": "MINOR", "required_change": "rename"}


def reused(a: Ariadne, rid: str) -> list[dict]:
    return [e.payload["reused_from"] for e in a.store.events(rid) if e.type == ev.EVIDENCE_RECORDED
            and e.payload.get("reused_from")]


def ran(a: Ariadne, rid: str) -> int:
    return sum(1 for e in a.store.events(rid) if e.type == ev.CHECK_STARTED)


def first_run(a: Ariadne, root, **spec) -> str:
    """A run that verified the fixed tree and was then abandoned, leaving the edit in place."""
    rid = a.start({"objective": "o", **spec}, actor=HUMAN)
    fix(root)
    a.verify(rid)
    return rid


def reopen(a: Ariadne, first: str, **spec) -> str:
    a.abandon(first, actor=HUMAN, reason="start over")
    return a.start({"objective": "o", **spec}, actor=HUMAN)


# ------------------------------------------------------------------- checks
def test_identical_tree_in_a_new_run_reuses_the_pass_and_still_accepts(repo, ari):
    r1 = first_run(ari, repo)
    r2 = reopen(ari, r1)
    recs = ari.verify(r2)
    assert [r.result for r in recs] == ["PASS"] and ran(ari, r2) == 0  # nothing was executed
    (src,) = reused(ari, r2)
    first = next(e for e in ari.store.events(r1) if e.type == ev.EVIDENCE_RECORDED)
    assert (src["run_id"], src["seq"], src["hash"]) == (r1, first.seq, first.hash)
    d = ari.evaluate(r2)
    assert d.check_states["tests"][0] is CheckState.PASS
    assert recs[0].run_id == r2 and recs[0].candidate_id == ari.state(r2).current_candidate_id
    assert ari.finish(r2).acceptable
    assert ari.store.verify_chain() > 0


def test_one_changed_byte_is_a_miss(repo, ari):
    r1 = first_run(ari, repo)
    (repo / "app.txt").write_text("ok \n")  # still passes, but it is a different tree
    r2 = reopen(ari, r1)
    ari.verify(r2)
    assert reused(ari, r2) == [] and ran(ari, r2) == 1


def test_check_command_or_policy_change_is_a_miss(tmp_path, clock):
    # Excluding the config from the candidate lets the same tree meet a changed definition.
    root = make_repo(tmp_path / "r", {"policy": {"candidate": {"exclude": [".daedalus.yml"]}}})
    a = Ariadne(root, clock=clock)
    base = git_head(root)
    r1 = first_run(a, root)
    a.abandon(r1, actor=HUMAN, reason="x")

    def commit_config(**changes):
        cfg = yaml.safe_load((root / ".daedalus.yml").read_text())
        cfg.update(changes)
        (root / ".daedalus.yml").write_text(yaml.safe_dump(cfg))
        git(root, "add", ".daedalus.yml")
        git(root, "commit", "-qm", "config")

    def second(**changes):
        commit_config(**changes)
        rid = a.start({"objective": "o"}, actor=HUMAN, base_revision=base)
        a.verify(rid)
        out = (reused(a, rid), ran(a, rid))
        a.abandon(rid, actor=HUMAN, reason="x")
        return out

    # Each commit changes one thing the key covers while the candidate stays the same.
    assert second(checks={"tests": {"command": [*CHECK, "extra"], "timeout_s": 60}}) == ([], 1)  # other argv
    assert second(checks={"tests": {"command": CHECK, "timeout_s": 61}}) == ([], 1)  # other timeout
    assert second(checks={"tests": {"command": CHECK, "timeout_s": 60},
                          "lint": {"command": CHECK}}) == ([], 2)  # same check, but another policy
    a.close()


def git_head(root) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_check_key_covers_every_input():
    from daedalus.core.policy import CheckDefinition

    d = CheckDefinition("t", ("pytest",), 60.0)
    base = ("cand", d.check_id, d.command, d.timeout_s, d.env_inputs, "fp", "policy", "1.0")
    k = check_key(*base)
    assert k == check_key(*base)
    for i, other in enumerate(("cand2", "t2", ("pytest", "-x"), 61.0, ("TOKEN",), "fp2", "policy2", "2.0")):
        changed = list(base)
        changed[i] = other
        assert check_key(*changed) != k, i


def test_fail_is_never_reused(repo, ari):
    r1 = ari.start({"objective": "o"}, actor=HUMAN)
    assert [r.result for r in ari.verify(r1)] == ["FAIL"]  # app.txt still says "bad"
    r2 = reopen(ari, r1)
    assert [r.result for r in ari.verify(r2)] == ["FAIL"]
    assert reused(ari, r2) == [] and ran(ari, r2) == 1


def test_error_and_untrusted_results_are_never_reused(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": ["definitely-not-a-program"], "max_attempts": 1}}})
    a = Ariadne(root, clock=clock)
    r1 = a.start({"objective": "o"}, actor=HUMAN)
    assert a.verify(r1)[0].result == "ERROR"
    r2 = reopen(a, r1)
    assert a.verify(r2)[0].result == "ERROR" and reused(a, r2) == []
    a.close()
    # Evidence from an untrusted verifier is ERROR already, and never a source either.
    root = make_repo(tmp_path / "u")
    a = Ariadne(root, clock=clock, verifier_id="rogue@box")
    r1 = a.start({"objective": "o"}, actor=HUMAN)
    fix(root)
    a.verify(r1)
    r2 = reopen(a, r1)
    a.verify(r2)
    assert reused(a, r2) == []
    a.close()


def test_check_marked_cache_false_is_always_rerun(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": CHECK, "timeout_s": 60, "cache": False}}})
    a = Ariadne(root, clock=clock)
    r1 = first_run(a, root)
    r2 = reopen(a, r1)
    a.verify(r2)
    assert reused(a, r2) == [] and ran(a, r2) == 1
    a.close()


def test_config_switch_and_no_cache_flag_disable_reuse(tmp_path, clock):
    off = make_repo(tmp_path / "off", {"cache": {"checks": False}})
    a = Ariadne(off, clock=clock)
    r2 = reopen(a, first_run(a, off))
    a.verify(r2)
    assert reused(a, r2) == [] and ran(a, r2) == 1
    a.close()

    on = make_repo(tmp_path / "on")  # the default is on
    a = Ariadne(on, clock=clock)
    r1 = first_run(a, on)
    a.no_cache = True
    r2 = reopen(a, r1)
    a.verify(r2)
    assert reused(a, r2) == [] and ran(a, r2) == 1
    a.close()


def test_cli_no_cache_flag(repo, monkeypatch, capsys):
    from daedalus import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    a = Ariadne(repo)
    r2 = reopen(a, first_run(a, repo))
    a.close()
    assert cli.main(["-C", str(repo), "verify", "--no-review", "--no-cache"]) == 0
    assert "reused" not in capsys.readouterr().out
    a = Ariadne(repo)
    assert reused(a, r2) == [] and ran(a, r2) == 1
    a.abandon(r2, actor=HUMAN, reason="x")
    r3 = a.start({"objective": "o"}, actor=HUMAN)
    a.close()
    assert cli.main(["-C", str(repo), "verify", "--no-review"]) == 0
    assert "reused from run" in capsys.readouterr().out


def test_ci_never_reuses_a_recorded_pass(tmp_path, clock):
    root = make_repo(tmp_path / "r")
    a = Ariadne(root, clock=clock)
    first_run(a, root)
    a.close()
    git(root, "checkout", "-q", "-b", "feature")
    git(root, "commit", "-qam", "fix")  # the tree the run verified, now committed, with its audit db
    rep = run_ci(root, "main")
    assert rep.ok and rep.checks == {"tests": ("PASS", "")}


# ------------------------------------------------------------------ reviews
@pytest.fixture
def review_repo(tmp_path):
    return make_repo(tmp_path / "rv", {"mode": "review"})


def scripted(*bodies, cost: float = 0.0, model: str = "m1") -> SimulatedAdapter:
    ad = SimulatedAdapter({"aristotle": [ScriptedStep(structured=b, cost=cost) for b in bodies]})
    ad.model = model
    return ad


def reviewed_once(a: Ariadne, root, body=CLEAN, **kw) -> str:
    r1 = first_run(a, root)
    launch_review(a, r1, "aristotle", scripted(body, **kw))
    return r1


def test_review_is_reused_for_the_same_candidate_contract_prompt_and_model(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo, {"summary": "ok", "findings": [MINOR]}, cost=0.5)
    r2 = reopen(a, r1)
    a.verify(r2)
    ad = scripted(model="m1")  # no script: launching would fail
    rid = launch_review(a, r2, "aristotle", ad)
    assert rid and ad.requests == []
    state = a.state(r2)
    assert review_satisfied(state, "aristotle", state.current_candidate_id, True)
    assert [f.title for f in state.findings.values()] == ["naming"]  # non-blocking findings travel with it
    ev_ = next(e for e in a.store.events(r2) if e.type == ev.REVIEW_RECORDED)
    src = ev_.payload["review"]["reused_from"]
    orig = next(e for e in a.store.events(r1) if e.type == ev.REVIEW_RECORDED)
    assert (src["run_id"], src["seq"], src["hash"]) == (r1, orig.seq, orig.hash) and src["cost"] == 0.5
    assert state.cost_used == 0  # reuse is free
    assert a.store.verify_chain() > 0
    m = run_metrics(a.store.events(r2))
    assert (m.cache_review_hits, m.cache_check_hits, m.cache_cost_saved) == (1, 1, 0.5)
    assert aggregate([run_metrics(a.store.events(r)) for r in a.store.run_ids()]).cache_hits == 2
    a.close()


def test_review_miss_when_prompt_model_contract_or_policy_changes(review_repo, clock, monkeypatch):
    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo)
    r2 = reopen(a, r1)
    a.verify(r2)

    other_model = scripted(CLEAN, model="m2")
    assert launch_review(a, r2, "aristotle", other_model) and len(other_model.requests) == 1  # model

    monkeypatch.setattr(prompts, "REVIEW_OUTPUT", prompts.REVIEW_OUTPUT + "\nBe harsher.\n")
    other_prompt = scripted(CLEAN, model="m1")
    assert launch_review(a, r2, "aristotle", other_prompt) and len(other_prompt.requests) == 1  # prompt
    monkeypatch.undo()

    r3 = reopen(a, r2, acceptance_criteria=[{"description": "also handles empty input"}])
    a.verify(r3)
    other_contract = scripted(CLEAN, model="m1")
    assert launch_review(a, r3, "aristotle", other_contract) and len(other_contract.requests) == 1
    assert [e.payload["review"].get("reused_from") for r in (r2, r3) for e in a.store.events(r)
            if e.type == ev.REVIEW_RECORDED] == [None, None, None]
    a.close()


def test_review_key_covers_every_input(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    state = a.state(a.start({"objective": "o"}, actor=HUMAN))
    c, pol, ad, tpl = state.contract, state.policy_hash, scripted(), "tpl"
    k = review_key("cand", "aristotle", pol, c, tpl, ad)
    assert k == review_key("cand", "aristotle", pol, c, tpl, ad)
    assert review_key("cand2", "aristotle", pol, c, tpl, ad) != k
    assert review_key("cand", "socrates", pol, c, tpl, ad) != k
    assert review_key("cand", "aristotle", "other", c, tpl, ad) != k
    assert review_key("cand", "aristotle", pol, c, "tpl2", ad) != k
    assert review_key("cand", "aristotle", pol, c, tpl, scripted(model="m2")) != k
    assert review_key("cand", "aristotle", pol, c, tpl, ad, version="9") != k
    a.close()


def test_review_with_a_blocking_finding_is_never_reused(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo, {"summary": "no", "findings": [BLOCKER]})
    r2 = reopen(a, r1)
    a.verify(r2)
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1 and a.state(r2).findings == {}
    a.close()


def test_review_cache_switch_and_no_cache(tmp_path, clock):
    root = make_repo(tmp_path / "rv", {"mode": "review", "cache": {"reviews": False}})
    a = Ariadne(root, clock=clock)
    r2 = reopen(a, reviewed_once(a, root))
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1
    a.close()
    root = make_repo(tmp_path / "rv2", {"mode": "review"})
    a = Ariadne(root, clock=clock)
    r1 = reviewed_once(a, root)
    a.no_cache = True
    r2 = reopen(a, r1)
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1
    a.close()


# -------------------------------------------------------------------- trust
def tamper(a: Ariadne) -> None:
    """Edit an old event in place, as an agent with file access might to plant a result."""
    a.store.close()
    db = sqlite3.connect(a.store.path)
    db.execute("UPDATE events SET actor = 'system:forged' WHERE type = 'evidence.recorded'")
    db.commit()
    db.close()
    a.store.__init__(a.store.path)


def test_a_corrupted_log_disables_reuse(repo, ari):
    r1 = first_run(ari, repo)
    r2 = reopen(ari, r1)
    tamper(ari)
    with pytest.raises(AuditIntegrityError):
        ari.store.verify_chain()
    assert ari.evidence_cache("checks") is None and ari.evidence_cache("reviews") is None
    ari.verify(r2)
    assert reused(ari, r2) == [] and ran(ari, r2) == 1  # fails closed to "re-run"


def test_a_corrupted_log_disables_review_reuse(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo)
    r2 = reopen(a, r1)
    a.verify(r2)
    tamper(a)
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1
    a.close()


# ------------------------------------------------------- review key binding
def _review_events(a: Ariadne, rid: str):
    return [e for e in a.store.events(rid) if e.type == ev.REVIEW_RECORDED]


def test_review_with_a_tampered_or_mismatched_key_is_not_reused(review_repo, clock, monkeypatch):
    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo)
    r2 = reopen(a, r1)
    a.verify(r2)
    r = _review_events(a, r1)[0].payload["review"]
    assert r["cache_inputs"]["adapter"].endswith(":m1")
    # a source the lookup cannot validate: key and inputs disagree, however the chain is handled
    cache = a.evidence_cache("reviews")
    key = r["cache_key"]
    forged = dict(r, cache_inputs={**r["cache_inputs"], "adapter": "x:other"})
    monkeypatch.setattr(cache.store, "events_of_type", lambda *t, **k: [
        type("E", (), {"payload": {"review": forged}, "actor": "a", "run_id": r1, "seq": 1, "hash": "h",
                           "ts": a.clock()})()])
    assert cache.find_review(key, candidate_id=r["candidate_id"], blocking=set()) is None
    forged = dict(r, cache_key="0" * 64)
    monkeypatch.setattr(cache.store, "events_of_type", lambda *t, **k: [
        type("E", (), {"payload": {"review": forged}, "actor": "a", "run_id": r1, "seq": 1, "hash": "h",
                           "ts": a.clock()})()])
    assert cache.find_review("0" * 64, candidate_id=r["candidate_id"], blocking=set()) is None
    monkeypatch.undo()
    # and a different current candidate never matches the stored one
    assert cache.find_review(key, candidate_id="other", blocking=set()) is None
    assert cache.find_review(key, candidate_id=r["candidate_id"], blocking=set()) is not None
    a.close()


def test_review_whose_candidate_changed_before_submission_is_not_a_source(review_repo, clock, monkeypatch):
    from daedalus.orchestration import lifecycle
    a = Ariadne(review_repo, clock=clock)
    r1 = first_run(a, review_repo)
    real = lifecycle._review_key
    monkeypatch.setattr(lifecycle, "_review_key", lambda *x, **k: ("stale-candidate", *real(*x, **k)[1:]))
    launch_review(a, r1, "aristotle", scripted(CLEAN))
    monkeypatch.undo()
    rec = _review_events(a, r1)[0].payload["review"]
    assert "cache_key" not in rec and "cache_inputs" not in rec
    r2 = reopen(a, r1)
    a.verify(r2)
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1  # launched for real: the first review was no source
    a.close()


# ------------------------------------------------------ age, inputs, model, pass
def test_a_source_older_than_max_age_is_not_reused(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"mode": "review", "cache": {"max_age_s": 100}})
    a = Ariadne(root, clock=clock)
    r1 = reviewed_once(a, root)
    clock.advance(100)  # exactly the limit still counts
    r2 = reopen(a, r1)
    a.verify(r2)
    ad = scripted()
    assert launch_review(a, r2, "aristotle", ad) and ad.requests == [] and len(reused(a, r2)) == 1
    r3 = reopen(a, r2)
    clock.advance(1)  # now r1's results are older than 100s
    a.verify(r3)
    ad = scripted(CLEAN)
    launch_review(a, r3, "aristotle", ad)
    assert reused(a, r3) == [] and ran(a, r3) == 1 and len(ad.requests) == 1
    a.close()


def test_default_max_age_is_one_day(repo, ari, clock):
    r1 = first_run(ari, repo)
    clock.advance(86_401)
    r2 = reopen(ari, r1)
    ari.verify(r2)
    assert reused(ari, r2) == [] and ran(ari, r2) == 1
    assert ari.config().cache_max_age_s == 86400


def test_a_change_in_cache_inputs_is_a_miss(tmp_path, clock):
    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": CHECK, "timeout_s": 60,
                                                           "cache_inputs": [".venv/pyvenv.cfg", "locks/*.lock"]}}})
    (root / ".gitignore").write_text(".daedalus/\n.venv/\nlocks/\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / "locks").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("version = 3.12\n")
    (root / "locks" / "a.lock").write_text("dep==1\n")
    a = Ariadne(root, clock=clock)
    r1 = first_run(a, root)
    r2 = reopen(a, r1)
    a.verify(r2)
    assert len(reused(a, r2)) == 1 and ran(a, r2) == 0  # inputs unchanged: a hit
    for change in (lambda: (root / ".venv" / "pyvenv.cfg").write_text("version = 3.13\n"),
                   lambda: (root / "locks" / "b.lock").write_text("dep==2\n")):
        change()
        rid = reopen(a, r2)
        a.verify(rid)
        assert reused(a, rid) == [] and ran(a, rid) == 1  # an edited file, or a new glob match
        r2 = rid
    a.close()


def test_review_with_no_configured_model_is_neither_reused_nor_a_source(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    r1 = first_run(a, review_repo)
    launch_review(a, r1, "aristotle", scripted(CLEAN, model=None))
    rec = _review_events(a, r1)[0].payload["review"]
    assert "cache_key" not in rec and "cache_inputs" not in rec
    r2 = reopen(a, r1)
    a.verify(r2)
    ad = scripted(CLEAN, model=None)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1 and "reused_from" not in _review_events(a, r2)[0].payload["review"]
    a.close()


def test_adapter_identity_needs_a_model_and_names_the_type_when_unnamed():
    from daedalus.verification.evidence_cache import adapter_identity

    class Bare:
        model = "m"

    assert adapter_identity(object()) is None and adapter_identity(scripted(model=None)) is None
    assert adapter_identity(Bare()).endswith("Bare:m") and "test_evidence_cache" in adapter_identity(Bare())
    bare = Bare()
    bare.review_model = "r"
    assert adapter_identity(bare).endswith("Bare:r")  # the model a review runs on
    assert adapter_identity(scripted(model="m1")) == "simulated:m1"


def test_a_re_review_is_not_a_source(review_repo, clock):
    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo, {"summary": "x", "findings": [MINOR]})
    first = scripted(CLEAN)
    launch_review(a, r1, "aristotle", first)  # same perspective again, with its own finding shown
    assert len(first.requests) == 1  # launched: a re-review is never served from the cache
    again = _review_events(a, r1)[1].payload["review"]
    assert "cache_key" not in again and "cache_inputs" not in again
    key = _review_events(a, r1)[0].payload["review"]["cache_key"]
    assert review_key("c", "aristotle", "p", a.state(r1).contract, "t", scripted(), first_pass=False) != \
        review_key("c", "aristotle", "p", a.state(r1).contract, "t", scripted(), first_pass=True)
    assert key  # the first-pass review stays a source
    a.close()


def test_review_is_not_submitted_when_the_tree_moves_after_the_lookup(review_repo, clock, monkeypatch):
    from daedalus.verification.evidence_cache import EvidenceCache

    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo)
    r2 = reopen(a, r1)
    a.verify(r2)
    real = EvidenceCache.find_review

    def hit_then_move(self, *args, **kw):
        found = real(self, *args, **kw)
        assert found is not None
        (review_repo / "app.txt").write_text("ok, moved\n")
        return found

    monkeypatch.setattr(EvidenceCache, "find_review", hit_then_move)
    labels_before = [e.payload.get("label") for e in a.store.events(r2) if e.type == ev.CANDIDATE_RECORDED]
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1  # a real launch, for the tree as it now is
    (rev,) = _review_events(a, r2)
    assert "reused_from" not in rev.payload["review"]
    assert rev.payload["review"]["candidate_id"] == a.state(r2).current_candidate_id
    labels = [e.payload.get("label") for e in a.store.events(r2) if e.type == ev.CANDIDATE_RECORDED]
    assert labels[len(labels_before):] == ["review aristotle"]  # no stray "(reused)" candidate
    a.close()


@pytest.mark.parametrize("damage", [{"findings": [{"title": "", "evidence": "", "severity": "MINOR"}]},
                                    {"reviewer": "system:cache"}])
def test_unusable_source_review_falls_through_without_a_stray_candidate(review_repo, clock, monkeypatch, damage):
    from daedalus.verification.evidence_cache import EvidenceCache

    a = Ariadne(review_repo, clock=clock)
    r1 = reviewed_once(a, review_repo)
    r2 = reopen(a, r1)
    a.verify(r2)
    real = EvidenceCache.find_review

    def damaged(self, *args, **kw):
        r, src = real(self, *args, **kw)
        return {**r, **damage}, src

    monkeypatch.setattr(EvidenceCache, "find_review", damaged)
    n = len([e for e in a.store.events(r2) if e.type == ev.CANDIDATE_RECORDED])
    ad = scripted(CLEAN)
    launch_review(a, r2, "aristotle", ad)
    assert len(ad.requests) == 1 and "reused_from" not in _review_events(a, r2)[0].payload["review"]
    labels = [e.payload.get("label") for e in a.store.events(r2) if e.type == ev.CANDIDATE_RECORDED][n:]
    assert not any("reused" in (x or "") for x in labels)  # no stray candidate for the refused reuse
    a.close()


# ------------------------------------------------------------ config values
@pytest.mark.parametrize("cache", [
    {"checks": "yes"}, {"reviews": 1}, {"checks": 0}, {"max_age_s": 0}, {"max_age_s": -5}, {"max_age_s": "60"},
    {"max_age_s": 1.5}, {"max_age_s": True}, {"extra": True}, "on", [True],
])
def test_bad_cache_config_is_rejected(tmp_path, cache):
    from daedalus.core.config import load_config

    root = make_repo(tmp_path / "r", {"cache": cache})
    with pytest.raises(PolicyError):
        load_config(root)


def test_cache_config_accepts_null_as_the_default(tmp_path):
    from daedalus.core.config import load_config

    for cache in (None, {"checks": None, "reviews": None, "max_age_s": None}, {}):
        cfg = load_config(make_repo(tmp_path / f"r{id(cache)}", {"cache": cache}))
        assert (cfg.cache_checks, cfg.cache_reviews, cfg.cache_max_age_s) == (True, True, 86400)
    cfg = load_config(make_repo(tmp_path / "set", {"cache": {"checks": False, "max_age_s": 60}}))
    assert (cfg.cache_checks, cfg.cache_reviews, cfg.cache_max_age_s) == (False, True, 60)


@pytest.mark.parametrize("entry", [{"cache": "no"}, {"cache": 0}, {"cache_inputs": "lock"},
                                   {"cache_inputs": ["../outside"]}, {"cache_inputs": ["/etc/passwd"]}])
def test_bad_check_cache_values_are_rejected(tmp_path, entry):
    from daedalus.core.config import load_config

    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": CHECK, **entry}}})
    with pytest.raises(PolicyError):
        load_config(root)


def test_check_cache_null_is_the_default(tmp_path):
    from daedalus.core.config import load_config

    root = make_repo(tmp_path / "r", {"checks": {"tests": {"command": CHECK, "cache": None}}})
    assert load_config(root).policy.checks["tests"].cache is True
