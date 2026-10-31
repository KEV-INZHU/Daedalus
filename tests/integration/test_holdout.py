"""Holdout checks gate acceptance without ever being shown to the Builder."""

from __future__ import annotations

import sys

import pytest

from conftest import CHECK, HUMAN, make_repo
from daedalus.adapters.base import AgentRequest, AgentResult
from daedalus.adapters.simulated import SimulatedAdapter
from daedalus.core.config import load_config
from daedalus.core.errors import PolicyError
from daedalus.core.state_machine import Disposition
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import run_task
from daedalus.orchestration.prompts import HELD_OUT

SECRET = "zebra-quartz"  # what the held-out check looks for; must never reach a builder prompt
HOLDOUT = [sys.executable, "-c", f"import sys; sys.exit(0 if '{SECRET}' in open('app.txt').read() else 1)"]
CHECKS = {
    "checks": {
        "tests": {"command": CHECK, "timeout_s": 60},
        "secret": {"command": HOLDOUT, "timeout_s": 60, "holdout": True},
    }
}


def test_holdout_must_be_a_boolean(tmp_path):
    root = make_repo(tmp_path / "r", {"checks": {"h": {"command": HOLDOUT, "holdout": "yes"}}})
    with pytest.raises(PolicyError):
        load_config(root)


def test_the_builder_never_sees_the_holdout_and_must_still_pass_it(tmp_path, clock):
    root = make_repo(tmp_path / "r", CHECKS)
    seen: list[AgentRequest] = []

    def build(req: AgentRequest) -> AgentResult:
        seen.append(req)
        # Round 1 satisfies only the visible check; round 2 (told only that a holdout failed) does it right.
        body = "ok\n" if len(seen) == 1 else f"ok {SECRET}\n"
        (req.cwd / "app.txt").write_text(body, encoding="utf-8")
        return AgentResult("COMPLETED", structured={"status": "done", "summary": "s"})

    ari = Ariadne(root, clock=clock)
    try:
        spec = {
            "objective": "fix the app",
            "acceptance_criteria": [{"description": "app works", "checks": ["tests", "secret"]}],
        }
        report = run_task(
            ari, spec, actor=HUMAN, adapter=SimulatedAdapter({"brunel": [build, build]}), max_rounds=2
        )
        assert report.decision.disposition is Disposition.ACCEPTED, report.decision.to_dict()
        assert len(seen) == 2
        for req in seen:
            assert "secret" not in req.prompt and SECRET not in req.prompt
            assert all("secret" not in c and SECRET not in c for c in req.run_commands)
            assert f"verified by checks: tests, {HELD_OUT}" in req.prompt
        feedback = seen[1].prompt
        assert "holdout check" in feedback and "Log:" not in feedback
    finally:
        ari.close()


def test_a_failing_holdout_blocks_acceptance(tmp_path, clock):
    root = make_repo(tmp_path / "r", CHECKS)

    def build(req: AgentRequest) -> AgentResult:
        (req.cwd / "app.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult("COMPLETED", structured={"status": "done", "summary": "s"})

    ari = Ariadne(root, clock=clock)
    try:
        report = run_task(
            ari, {"objective": "o"}, actor=HUMAN, adapter=SimulatedAdapter({"brunel": [build]}), max_rounds=1
        )
        assert report.decision.disposition is not Disposition.ACCEPTED
        [reason] = [r for r in report.decision.reasons if r.code == "check:secret"]
        assert (
            reason.fixable_by == "agent"
            and "Log:" not in reason.message
            and "kept from the" in reason.message
        )
    finally:
        ari.close()
