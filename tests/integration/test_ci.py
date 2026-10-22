"""`daedalus ci`: fresh checks, risk and provenance for a branch against its base."""

from __future__ import annotations

import yaml

from conftest import fix, git, make_repo
from daedalus.ci import render_workflow, run_ci


def branch_with(root, *, trailer: bool, edit=fix):
    git(root, "checkout", "-q", "-b", "feature")
    edit(root)
    msg = "fix the app" + ("\n\nDaedalus-Run: abc123def456" if trailer else "")
    git(root, "commit", "-qam", msg)


def test_checks_run_fresh_on_the_head_and_pass(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "r")
    branch_with(root, trailer=True)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rep = run_ci(root, "main")
    assert rep.ok and rep.checks == {"tests": ("PASS", "")}
    assert rep.changed == ["app.txt"] and rep.tier == "low"
    assert rep.commits[0][2] == "abc123def456" and not rep.unattested
    text = summary.read_text()
    assert "Daedalus gate" in text and "1 of 1 commit(s) were accepted" in text


def test_a_failing_check_fails_ci(tmp_path):
    root = make_repo(tmp_path / "r")
    branch_with(root, trailer=True, edit=lambda r: (r / "app.txt").write_text("nope\n"))  # the check wants "ok"
    rep = run_ci(root, "main")
    assert not rep.ok and rep.checks["tests"][0] == "FAIL"


def test_provenance_is_reported_and_can_be_required(tmp_path):
    root = make_repo(tmp_path / "r")
    branch_with(root, trailer=False)
    assert run_ci(root, "main").ok  # reported, not required
    rep = run_ci(root, "main", require_provenance=True)
    assert not rep.ok and len(rep.unattested) == 1 and "no `Daedalus-Run` trailer" in rep.markdown()


def test_risk_floors_apply_to_the_whole_diff(tmp_path):
    root = make_repo(tmp_path / "r")

    def touch_auth(r):
        fix(r)
        (r / "src" / "auth").mkdir()
        (r / "src" / "auth" / "login.py").write_text("x = 1\n")
        git(r, "add", "-A")

    branch_with(root, trailer=True, edit=touch_auth)
    rep = run_ci(root, "main")
    assert rep.tier == "high" and "merge" not in rep.approvals and "socrates" in rep.reviews
    assert "Confirm them before merging" in rep.markdown()  # the reviews are still listed


def test_no_mandatory_check_never_passes(tmp_path):
    root = make_repo(tmp_path / "r", {"checks": {}})
    branch_with(root, trailer=True)
    assert not run_ci(root, "main").ok


def test_workflow_template_is_valid_yaml():
    doc = yaml.safe_load(render_workflow("daedalus", install='pip install -e ".[dev]"', require_provenance=True))
    steps = doc["jobs"]["gate"]["steps"]
    assert doc[True] == {"pull_request": None}  # YAML 1.1 reads the `on:` key as True
    assert steps[0]["with"]["fetch-depth"] == 0
    assert doc["jobs"]["gate"]["permissions"] == {"contents": "read"}  # a read-only token for the PR's checks
    assert any("daedalus ci" in s.get("run", "") and "--require-provenance" in s["run"] for s in steps)
    assert any(s.get("run") == 'pip install -e ".[dev]"' for s in steps)


def test_ci_cli_and_init_writes_the_workflow(tmp_path, capsys, monkeypatch):
    from daedalus import cli

    root = make_repo(tmp_path / "r")
    branch_with(root, trailer=False)
    assert cli.main(["-C", str(root), "ci", "--base", "main"]) == 0
    assert "Daedalus gate" in capsys.readouterr().out
    assert cli.main(["-C", str(root), "ci", "--base", "main", "--require-provenance"]) == 1
    assert cli.main(["-C", str(root), "init", "--force", "--no-hooks", "--ci", "--ci-install", "pip install -e ."]) == 0
    wf = yaml.safe_load((root / ".github" / "workflows" / "daedalus.yml").read_text())
    runs = [s.get("run", "") for s in wf["jobs"]["gate"]["steps"]]
    assert "pip install -e ." in runs and any(r.startswith("daedalus ci") for r in runs)


def test_init_ci_force_refreshes_the_workflow_but_keeps_the_config(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r")
    cfg = root / ".daedalus.yml"
    cfg.write_bytes(b"# hand-tuned\nchecks:\n  tests: {cmd: 'true'}\n")
    before = cfg.read_bytes()
    wf = root / ".github" / "workflows" / "daedalus.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("stale\n")
    assert cli.main(["-C", str(root), "init", "--ci", "--force", "--no-hooks", "--ci-install", "make deps"]) == 0
    assert cfg.read_bytes() == before
    runs = [s.get("run", "") for s in yaml.safe_load(wf.read_text())["jobs"]["gate"]["steps"]]
    assert "make deps" in runs
    # without --ci, --force still regenerates the config
    assert cli.main(["-C", str(root), "init", "--force", "--no-hooks"]) == 0
    assert cfg.read_bytes() != before and "hand-tuned" not in cfg.read_text()


def test_init_ci_writes_the_config_when_missing(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r")
    cfg = root / ".daedalus.yml"
    cfg.unlink(missing_ok=True)
    assert cli.main(["-C", str(root), "init", "--ci", "--force", "--no-hooks"]) == 0
    assert cfg.exists() and (root / ".github" / "workflows" / "daedalus.yml").exists()


def test_flag_criteria_add_to_contract_file_criteria(tmp_path, monkeypatch):
    from daedalus import cli
    from daedalus.orchestration.ariadne import Ariadne

    root = make_repo(tmp_path / "r")
    monkeypatch.setenv("CLAUDECODE", "1")
    contract = tmp_path / "c.yml"
    contract.write_text("objective: o\nacceptance_criteria: [the owner signed off]\n")
    assert cli.main(["-C", str(root), "start", "--contract", str(contract), "-c", "app says ok"]) == 0
    a = Ariadne(root)
    crits = a.state(a.active_run_id()).contract.acceptance_criteria
    assert [(c.description, c.checks) for c in crits] == [("the owner signed off", ()), ("app says ok", ("tests",))]
    a.close()


def test_a_pull_request_cannot_weaken_the_gate_that_judges_it(tmp_path):
    import sys

    root = make_repo(tmp_path / "r")

    def weaken(r):
        (r / "app.txt").write_text("nope\n")  # fails the base's check
        cfg = yaml.safe_load((r / ".daedalus.yml").read_text())
        cfg["checks"] = {"tests": {"command": [sys.executable, "-c", "pass"]}}  # "tests" now always passes
        (r / ".daedalus.yml").write_text(yaml.safe_dump(cfg))

    branch_with(root, trailer=True, edit=weaken)
    rep = run_ci(root, "main")
    assert not rep.ok and rep.checks["tests"][0] == "FAIL"  # the base's check ran
    assert rep.policy_source == "base" and rep.policy_changed == [".daedalus.yml"]
    assert rep.tier == "critical" and "Policy changed in this PR" in rep.markdown()


def test_without_config_on_the_base_the_heads_policy_is_used_and_said_so(tmp_path):
    root = make_repo(tmp_path / "r")
    git(root, "rm", "-q", ".daedalus.yml")
    git(root, "commit", "-qm", "no gate yet")
    git(root, "checkout", "-q", "-b", "adopt")
    git(root, "checkout", "-q", "main~1", "--", ".daedalus.yml")
    fix(root)
    git(root, "commit", "-qam", "adopt the gate")
    rep = run_ci(root, "main")
    assert rep.policy_source == "head" and "base branch has no Daedalus config" in rep.markdown()


def test_an_unknown_base_says_how_to_fetch_it(tmp_path):
    import pytest

    from daedalus.core.errors import DaedalusError

    root = make_repo(tmp_path / "r")
    with pytest.raises(DaedalusError, match="fetch-depth: 0"):
        run_ci(root, "origin/nowhere")


def test_an_unwritable_summary_does_not_fail_the_job(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "r")
    branch_with(root, trailer=True)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing-dir" / "summary.md"))
    assert run_ci(root, "main").ok


def test_install_commands_with_yaml_syntax_are_quoted():
    import pytest

    from daedalus.core.errors import DaedalusError

    for cmd in ("npm ci # fast", "echo a: b", "- weird", '"quoted"'):
        doc = yaml.safe_load(render_workflow("daedalus", install=cmd))
        assert any(s.get("run") == cmd for s in doc["jobs"]["gate"]["steps"])
    with pytest.raises(DaedalusError, match="single line"):
        render_workflow("daedalus", install="a\nb")


def test_init_refuses_workflow_options_without_ci(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "init", "--force", "--no-hooks", "--require-provenance"]) == 2
    assert not (root / ".github").exists()


def test_a_base_policy_file_outside_the_repository_is_refused(tmp_path):
    import pytest

    from daedalus.core.errors import DaedalusError

    root = make_repo(tmp_path / "r")
    cfg = yaml.safe_load((root / ".daedalus.yml").read_text())
    cfg["policy_file"] = "../../etc/policy.yaml"
    (root / ".daedalus.yml").write_text(yaml.safe_dump(cfg))
    git(root, "commit", "-qam", "odd policy file")
    branch_with(root, trailer=True)
    with pytest.raises(DaedalusError, match="inside the repository"):
        run_ci(root, "main")


def test_init_checks_workflow_options_before_writing_anything(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r")
    (root / ".daedalus.yml").unlink()
    assert cli.main(["-C", str(root), "init", "--ci-source", "x"]) == 2
    assert not (root / ".daedalus.yml").exists() and not (root / ".claude").exists()


def test_workflow_passes_the_base_ref_through_env_and_quotes_the_source():
    import pytest

    from daedalus.core.errors import DaedalusError

    doc = yaml.safe_load(render_workflow("git+https://example.com/x@v1; rm -rf /"))
    steps = doc["jobs"]["gate"]["steps"]
    gate = next(s for s in steps if s.get("name") == "Gate")
    assert gate["env"]["BASE_REF"] == "${{ github.base_ref }}" and "${{" not in gate["run"]
    install = next(s for s in steps if s.get("name") == "Install Daedalus")
    assert install["run"] == "pip install 'git+https://example.com/x@v1; rm -rf /'"  # one quoted argument
    with pytest.raises(DaedalusError, match="single line"):
        render_workflow("a\nb")


def test_malformed_base_config_is_a_clear_error(tmp_path):
    import pytest

    from daedalus.core.errors import DaedalusError

    root = make_repo(tmp_path / "r")
    (root / ".daedalus.yml").write_text("checks: [unclosed\n")
    git(root, "commit", "-qam", "broken config on main")
    git(root, "checkout", "-q", "-b", "fix")
    (root / ".daedalus.yml").write_text("enabled: true\n")
    git(root, "commit", "-qam", "repair")
    with pytest.raises(DaedalusError, match="on the base is not valid YAML"):
        run_ci(root, "main")


def test_a_policy_tightened_on_the_base_after_branching_still_applies(tmp_path):
    import sys

    root = make_repo(tmp_path / "r")
    git(root, "checkout", "-q", "-b", "feature")
    fix(root)
    git(root, "commit", "-qam", "fix the app")
    git(root, "checkout", "-q", "main")
    cfg = yaml.safe_load((root / ".daedalus.yml").read_text())
    cfg["checks"]["lint"] = {"command": [sys.executable, "-c", "import sys; sys.exit(1)"]}  # new, failing
    (root / ".daedalus.yml").write_text(yaml.safe_dump(cfg))
    git(root, "commit", "-qam", "main adds a mandatory check")
    git(root, "checkout", "-q", "feature")
    rep = run_ci(root, "main")
    assert "lint" in rep.checks and not rep.ok


def test_commit_subjects_cannot_inject_markup_into_the_report(tmp_path):
    root = make_repo(tmp_path / "r")
    git(root, "checkout", "-q", "-b", "feature")
    fix(root)
    git(root, "commit", "-qam", "[click](http://evil) | fake | row <img src=x>")
    md = run_ci(root, "main").markdown()
    assert "](http" not in md and "<img" not in md and "\\|" in md


def test_init_validates_the_workflow_before_writing_anything(tmp_path):
    from daedalus import cli

    root = make_repo(tmp_path / "r")
    (root / ".daedalus.yml").unlink()
    assert cli.main(["-C", str(root), "init", "--ci", "--ci-install", "a\nb"]) == 2
    assert not (root / ".daedalus.yml").exists() and not (root / ".claude").exists()


def test_a_weakened_test_raises_the_tier_and_the_report_says_so(tmp_path):
    root = make_repo(tmp_path / "r")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_a():\n    assert 1 + 1 == 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add a test")

    def weaken(r):
        fix(r)
        (r / "tests" / "test_app.py").write_text("def test_a():\n    assert True\n")

    branch_with(root, trailer=True, edit=weaken)
    rep = run_ci(root, "main")
    assert rep.tier == "high" and "test_integrity" in rep.risk
    assert rep.test_integrity and "tests/test_app.py" in rep.test_integrity[0]
    text = rep.markdown()
    assert "Tests weakened" in text and "tests/test_app.py" in text


def test_a_new_workspace_config_is_not_a_weakened_test(tmp_path):
    root = make_repo(tmp_path / "r")

    def add_package_json(r):
        fix(r)
        (r / "web").mkdir()
        (r / "web" / "package.json").write_text('{"name": "web", "version": "1.0.0"}\n')

    branch_with(root, trailer=True, edit=add_package_json)
    rep = run_ci(root, "main")
    assert not rep.test_integrity and "Tests weakened" not in rep.markdown()


def test_markdown_out_and_annotations_from_the_cli(tmp_path, capsys):
    from daedalus.cli import main

    root = make_repo(tmp_path / "r")
    branch_with(root, trailer=True, edit=lambda r: (r / "app.txt").write_text("nope\n"))  # the check wants "ok"
    out = tmp_path / "comment.md"
    code = main(["--dir", str(root), "ci", "--base", "main", "--markdown-out", str(out), "--annotations"])
    assert code == 1
    body = out.read_text(encoding="utf-8")
    assert body.splitlines()[0].startswith("Daedalus CI: ❌ failed") and "<!-- daedalus-ci -->" in body
    assert "::error title=Daedalus check failed::check tests FAIL" in capsys.readouterr().out
