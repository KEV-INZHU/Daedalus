"""Policy presets: solo | standard | strict overlay the default policy, and the repository's keys win."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from daedalus import cli
from daedalus.core.config import PRESETS, load_config, load_preset
from daedalus.core.errors import PolicyError
from daedalus.core.policy import default_policy_text

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "policy.schema.json"


def _repo(tmp_path: Path, **cfg) -> Path:
    root = tmp_path / "r"
    root.mkdir(parents=True)
    base = {"checks": {"tests": "python -m pytest -q"}, **cfg}
    (root / ".daedalus.yml").write_text(yaml.safe_dump(base), encoding="utf-8")
    return root


@pytest.mark.parametrize("name", PRESETS)
def test_each_preset_loads_and_validates_against_the_schema(tmp_path, name):
    import jsonschema  # a dev dependency: the test must run, not skip
    cfg = load_config(_repo(tmp_path, preset=name))
    assert cfg.preset == name
    jsonschema.validate(cfg.policy_raw, json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert cfg.policy.policy_hash  # resolves to a real, hashable policy


def test_standard_is_the_defaults(tmp_path):
    cfg = load_config(_repo(tmp_path, preset="standard"))
    plain = load_config(_repo(tmp_path / "x"))  # no preset
    assert cfg.policy_raw == plain.policy_raw
    assert cfg.mode == "gate"


def test_solo_is_review_mode_with_three_reviewers_and_no_human_on_high(tmp_path):
    cfg = load_config(_repo(tmp_path, preset="solo"))
    tiers = cfg.policy_raw["tier_requirements"]
    assert cfg.mode == "review" and cfg.cache_checks and cfg.cache_reviews
    assert tiers["medium"]["reviews"] == ["aristotle"]
    assert tiers["high"]["reviews"] == ["socrates", "aristotle", "james"] and tiers["high"]["approvals"] == []
    assert tiers["critical"]["approvals"] == ["merge"]  # untouched tiers keep the default


def test_strict_raises_high_and_requires_provenance_on_push(tmp_path):
    cfg = load_config(_repo(tmp_path, preset="strict"))
    for tier in ("high", "critical"):
        req = cfg.policy_raw["tier_requirements"][tier]
        assert req["approvals"] == ["merge"] and len(req["reviews"]) == 4
    assert cfg.harness["require_provenance_on_push"] is True


def test_the_preset_is_read_at_load_time_so_it_is_not_copied_into_the_config(tmp_path):
    root = _repo(tmp_path, preset="strict")
    assert "tier_requirements" not in (root / ".daedalus.yml").read_text(encoding="utf-8")
    assert load_preset("strict")["policy"]["tier_requirements"]["high"]["approvals"] == ["merge"]


def test_user_keys_win_over_the_preset(tmp_path):
    mine = {"high": {"reviews": ["aristotle"], "approvals": []}}
    cfg = load_config(_repo(tmp_path, preset="strict", policy={"tier_requirements": mine},
                            mode="gate", harness={"require_provenance_on_push": False}))
    assert cfg.policy_raw["tier_requirements"] == mine
    assert cfg.mode == "gate"
    assert cfg.harness["require_provenance_on_push"] is False


def test_a_user_cache_setting_wins_over_the_preset(tmp_path):
    cfg = load_config(_repo(tmp_path, preset="solo", cache={"reviews": False}))
    assert cfg.cache_checks is True and cfg.cache_reviews is False


def test_a_preset_changes_the_pinned_policy_hash(tmp_path):
    plain = load_config(_repo(tmp_path / "a")).policy.policy_hash
    solo = load_config(_repo(tmp_path / "b", preset="strict")).policy.policy_hash
    assert plain != solo


def test_unknown_preset_and_policy_file_combination_are_refused(tmp_path):
    with pytest.raises(PolicyError, match="unknown preset"):
        load_config(_repo(tmp_path, preset="paranoid"))
    (tmp_path / "pf").mkdir(exist_ok=True)
    root = _repo(tmp_path / "pf", preset="solo", policy_file="p.yaml")
    (root / "p.yaml").write_text(default_policy_text(), encoding="utf-8")
    with pytest.raises(PolicyError, match="cannot be combined"):
        load_config(root)


def test_init_preset_writes_the_preset_and_no_mode_line(tmp_path, capsys):
    root = tmp_path / "r"
    root.mkdir(parents=True)
    (root / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    assert cli.main(["-C", str(root), "init", "--no-hooks", "--preset", "solo"]) == 0
    written = yaml.safe_load((root / ".daedalus.yml").read_text(encoding="utf-8"))
    assert written["preset"] == "solo" and "mode" not in written
    cfg = load_config(root)
    assert cfg.preset == "solo" and cfg.mode == "review"


def test_init_rejects_an_unknown_preset(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["-C", str(tmp_path), "init", "--no-hooks", "--preset", "paranoid"])


def test_policy_diff_shows_what_a_preset_changes(tmp_path, capsys):
    root = _repo(tmp_path)
    assert cli.main(["-C", str(root), "policy", "--preset", "strict", "--diff"]) == 0
    out = capsys.readouterr().out
    assert "+  require_provenance_on_push: true" in out
    assert "+    - mozi" in out
    assert "# policy hash" in out


def test_policy_diff_says_so_when_nothing_changes(tmp_path, capsys):
    root = _repo(tmp_path, preset="solo")
    assert cli.main(["-C", str(root), "policy", "--preset", "solo", "--diff"]) == 0
    assert "changes nothing" in capsys.readouterr().out


def test_policy_diff_leaves_user_keys_out_of_the_changes(tmp_path, capsys):
    mine = {"high": {"reviews": ["aristotle"], "approvals": []}}
    root = _repo(tmp_path, policy={"tier_requirements": mine})
    cli.main(["-C", str(root), "policy", "--preset", "strict", "--diff"])
    out = capsys.readouterr().out
    assert "mozi" not in out and "require_provenance_on_push" in out


def test_diff_without_a_preset_is_an_error(tmp_path, capsys):
    assert cli.main(["-C", str(_repo(tmp_path)), "policy", "--diff"]) == 2
    assert "--preset" in capsys.readouterr().err


def test_status_names_reviews_in_plain_words():
    from daedalus.cli._common import display_name

    assert display_name("socrates") == "validation review (Socrates)"
    assert display_name("custom-review") == "custom-review"
