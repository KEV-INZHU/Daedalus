"""The published JSON schemas must accept what the code actually produces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from conftest import HUMAN, fix
from daedalus.core.policy import default_policy_text

jsonschema = pytest.importorskip("jsonschema")
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def test_default_policy_matches_schema():
    jsonschema.validate(yaml.safe_load(default_policy_text()), schema("policy.schema.json"))


def test_run_records_match_schemas(ari, repo):
    rid = ari.start({"objective": "o", "acceptance_criteria": ["app ok"]}, actor=HUMAN)
    fix(repo)
    (rec,) = ari.verify(rid)
    ari.finish(rid)
    approval = ari.approve(rid, "merge", actor=HUMAN)
    state = ari.state(rid)
    jsonschema.validate(state.contract.to_dict(), schema("task-contract.schema.json"))
    jsonschema.validate(rec.to_dict(), schema("evidence-record.schema.json"))
    jsonschema.validate(approval.to_dict(), schema("approval-record.schema.json"))


def test_agent_authority_rejected_by_policy_schema():
    raw = yaml.safe_load(default_policy_text())
    raw["authorities"]["merge"] = ["agent:builder"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, schema("policy.schema.json"))
