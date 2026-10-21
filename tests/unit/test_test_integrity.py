"""The test-tampering detector: each signal, and the changes that must NOT be flagged."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from daedalus.core.policy import PolicyError, default_policy_text, policy_from_dict
from daedalus.verification import test_integrity as ti


def run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    run(tmp_path, "init", "-q", "-b", "main")
    run(tmp_path, "config", "user.email", "t@example.com")
    run(tmp_path, "config", "user.name", "t")
    return tmp_path


def commit(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    run(root, "add", "-A")
    run(root, "commit", "-q", "-m", "base")
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def change(root: Path, files: dict[str, str | None]) -> dict[str, str | None]:
    """Apply a candidate change; returns the manifest-style `files` map (None = deleted)."""
    out: dict[str, str | None] = {}
    for rel, text in files.items():
        p = root / rel
        if text is None:
            p.unlink()
            out[rel] = None
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            out[rel] = hashlib.sha256(text.encode()).hexdigest()
    return out


def scan(root: Path, base: str, files: dict[str, str | None]) -> ti.IntegrityReport:
    return ti.analyze(root, base, change(root, files))


def kinds(report: ti.IntegrityReport) -> set[str]:
    return {s.kind for s in report.signals}


PY_BASE = '''
def test_add():
    assert add(1, 2) == 3


def test_neg():
    assert add(-1, 1) == 0
    assert add(-1, -1) == -2
'''


# ------------------------------------------------------------------------------ classification
def test_default_globs_classify_paths():
    for path in ("test_a.py", "pkg/a_test.py", "tests/x/data.py", "web/a.test.ts", "web/a.spec.js",
                 "pkg/a_test.go", "crate/tests/it.rs", "tests/test_a.py"):
        assert ti.is_test_path(path), path
    for path in ("src/app.py", "src/contest.py", "README.md", "pkg/a.go"):
        assert not ti.is_test_path(path), path


# ------------------------------------------------------------------------------------ Python
def test_assert_true_replacement_is_flagged_with_a_specific_message(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace("assert add(1, 2) == 3", "assert True")})
    assert r.flagged
    assert ti.LOST_ASSERTIONS in kinds(r)
    assert "tests/test_app.py: 'test_add' lost 1 assertion and now asserts `True`" in r.summary()
    assert "assert True" in r.hunks["tests/test_app.py"]


def test_deleted_test_file_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE, "src.py": "x = 1\n"})
    r = scan(repo, base, {"tests/test_app.py": None})
    assert kinds(r) == {ti.REMOVED_FILE}
    assert "test_add" in r.summary()


def test_emptied_test_file_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": ""})
    assert ti.EMPTIED_FILE in kinds(r)


def test_removed_test_function_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": "def test_add():\n    assert add(1, 2) == 3\n"})
    assert ti.REMOVED_TEST in kinds(r)
    assert "'test_neg'" in r.summary()


def test_dropped_assertions_in_a_test_are_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    weaker = PY_BASE.replace("    assert add(-1, -1) == -2\n", "")
    r = scan(repo, base, {"tests/test_app.py": weaker})
    assert ti.LOST_ASSERTIONS in kinds(r)
    assert "'test_neg' lost 1 assertion" in r.summary()


def test_test_with_all_asserts_removed_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace("    assert add(1, 2) == 3", "    add(1, 2)")})
    assert "no longer asserts anything" in r.summary()


@pytest.mark.parametrize("decorator", [
    "@pytest.mark.skip(reason='later')",
    "@pytest.mark.skipif(True, reason='x')",
    "@pytest.mark.xfail",
    "@unittest.skip('x')",
])
def test_python_skip_decorators_are_flagged(repo, decorator):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace("def test_add", decorator + "\ndef test_add")})
    assert ti.SKIP_ADDED in kinds(r)


def test_pytest_skip_call_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace(
        "    assert add(1, 2) == 3", "    pytest.skip('flaky')\n    assert add(1, 2) == 3")})
    assert ti.SKIP_ADDED in kinds(r)


@pytest.mark.parametrize("trivial", ["assert True", "assert 1", "assert not False", "assert 1 == 1"])
def test_trivially_true_asserts_are_flagged(repo, trivial):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace("assert add(1, 2) == 3", trivial)})
    assert ti.LOST_ASSERTIONS in kinds(r)


def test_unittest_asserttrue_true_is_flagged(repo):
    src = "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(add(1, 2), 3)\n"
    base = commit(repo, {"tests/test_app.py": src})
    r = scan(repo, base, {"tests/test_app.py": src.replace("self.assertEqual(add(1, 2), 3)", "self.assertTrue(True)")})
    assert r.flagged
    assert "assertTrue(True)" in r.summary()


def test_new_test_with_trivial_assert_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE + "\n\ndef test_new():\n    assert True\n"})
    assert ti.TRIVIAL_ASSERT in kinds(r)
    assert "new test 'test_new'" in r.summary()


def test_changed_expected_literal_is_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace("add(1, 2) == 3", "add(1, 2) == 4")})
    assert kinds(r) == {ti.EXPECTED_CHANGED}
    assert "now expects 4 instead of 3 for `add(1, 2)`" in r.summary()


def test_syntax_error_in_changed_test_file_fails_closed(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": "def test_add(:\n    pass\n"})
    assert kinds(r) == {ti.UNANALYZABLE}
    assert "could not analyze" in r.summary()


def test_binary_test_file_fails_closed(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    (repo / "tests/test_app.py").write_bytes(b"\xff\xfe\x00bad")
    r = ti.analyze(repo, base, {"tests/test_app.py": "0" * 64})
    assert kinds(r) == {ti.UNANALYZABLE}


def test_unreadable_base_revision_fails_closed(repo):
    commit(repo, {"tests/test_app.py": PY_BASE})
    r = ti.analyze(repo, "0" * 40, {"tests/test_app.py": "0" * 64})
    assert kinds(r) == {ti.UNANALYZABLE}


# ------------------------------------------------------------------------------ no false positives
def test_moving_asserts_between_files_is_not_flagged(repo):
    other = "def test_other():\n    assert other() == 1\n"
    base = commit(repo, {"tests/test_app.py": PY_BASE, "tests/test_other.py": other})
    moved_out = "def test_add():\n    assert add(1, 2) == 3\n"
    moved_in = other + "\n\ndef test_neg():\n    assert add(-1, 1) == 0\n    assert add(-1, -1) == -2\n"
    r = scan(repo, base, {"tests/test_app.py": moved_out, "tests/test_other.py": moved_in})
    assert not r.flagged, r.summary()


def test_adding_new_tests_is_not_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE + "\n\ndef test_more():\n    assert add(2, 2) == 4\n",
                          "tests/test_new.py": "def test_x():\n    assert 1 + 1 == 2\n"})
    assert not r.flagged, r.summary()


def test_renamed_test_file_is_not_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": None, "tests/test_calc.py": PY_BASE})
    assert not r.flagged, r.summary()


def test_strengthening_a_test_is_not_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace(
        "    assert add(1, 2) == 3", "    assert add(1, 2) == 3\n    assert add(0, 0) == 0")})
    assert not r.flagged, r.summary()


def test_changes_to_non_test_files_are_ignored(repo):
    base = commit(repo, {"src/app.py": "def add(a, b):\n    return a - b\n"})
    assert not scan(repo, base, {"src/app.py": "def add(a, b):\n    return a + b\n"}).flagged


def test_deleting_a_helper_in_the_tests_directory_is_not_flagged(repo):
    base = commit(repo, {"tests/helpers.py": "def make():\n    return 1\n", "tests/__init__.py": ""})
    assert not scan(repo, base, {"tests/helpers.py": None}).flagged


def test_custom_globs_decide_what_is_a_test(repo):
    base = commit(repo, {"checks/test_app.py": PY_BASE})
    files = change(repo, {"checks/test_app.py": PY_BASE.replace("assert add(1, 2) == 3", "assert True")})
    assert ti.analyze(repo, base, files).flagged  # test_*.py matches the default globs
    assert not ti.analyze(repo, base, files, ["only/**"]).flagged
    assert ti.analyze(repo, base, files, ["checks/**"]).flagged


# --------------------------------------------------------------------------------------- JS / TS
JS_BASE = '''
describe("math", () => {
  it("adds", () => {
    expect(add(1, 2)).toBe(3);
  });
  it("subtracts", () => {
    expect(sub(3, 1)).toBe(2);
    expect(sub(1, 1)).toBe(0);
  });
});
'''


def test_js_expect_true_replacement_is_flagged(repo):
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    r = scan(repo, base, {"web/math.test.ts": JS_BASE.replace("expect(add(1, 2)).toBe(3)", "expect(true).toBe(true)")})
    assert ti.TRIVIAL_ASSERT in kinds(r)
    assert ti.LOST_ASSERTIONS in kinds(r)


@pytest.mark.parametrize("edit", [
    ('it("adds"', 'it.skip("adds"'),
    ('it("adds"', 'xit("adds"'),
    ('describe("math"', 'describe.skip("math"'),
    ('it("adds"', 'test.todo("adds"'),
])
def test_js_skip_markers_are_flagged(repo, edit):
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    r = scan(repo, base, {"web/math.test.ts": JS_BASE.replace(*edit)})
    assert ti.SKIP_ADDED in kinds(r)


def test_js_only_is_flagged_as_focus(repo):
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    r = scan(repo, base, {"web/math.test.ts": JS_BASE.replace('it("adds"', 'it.only("adds"')})
    assert ti.FOCUS_ADDED in kinds(r)


def test_js_removed_expectations_and_tests_are_flagged(repo):
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    r = scan(repo, base, {"web/math.test.ts": JS_BASE.replace("    expect(sub(1, 1)).toBe(0);\n", "")})
    assert ti.LOST_ASSERTIONS in kinds(r)
    r = scan(repo, base, {"web/math.test.ts": JS_BASE.split('  it("subtracts"')[0] + "});\n"})
    assert ti.REMOVED_TEST in kinds(r)


def test_js_moving_a_test_between_files_is_not_flagged(repo):
    other = 'it("other", () => {\n  expect(1).toBe(1 + 0);\n});\n'
    base = commit(repo, {"web/math.test.ts": JS_BASE, "web/other.test.ts": other})
    kept = JS_BASE.split('  it("subtracts"')[0] + "});\n"
    moved = other + 'it("subtracts", () => {\n  expect(sub(3, 1)).toBe(2);\n  expect(sub(1, 1)).toBe(0);\n});\n'
    assert not scan(repo, base, {"web/math.test.ts": kept, "web/other.test.ts": moved}).flagged


def test_js_commented_out_skip_is_not_flagged(repo):
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    r = scan(repo, base, {"web/math.test.ts": JS_BASE + '// it.skip("later", () => {})\n'})
    assert not r.flagged


# ------------------------------------------------------------------------------------------- Go
GO_BASE = '''package calc

import "testing"

func TestAdd(t *testing.T) {
\tif Add(1, 2) != 3 {
\t\tt.Errorf("want 3")
\t}
}

func TestSub(t *testing.T) {
\tif Sub(3, 1) != 2 {
\t\tt.Fatal("want 2")
\t}
}
'''


def test_go_skip_is_flagged(repo):
    base = commit(repo, {"calc_test.go": GO_BASE})
    r = scan(repo, base, {"calc_test.go": GO_BASE.replace(
        "func TestAdd(t *testing.T) {", 'func TestAdd(t *testing.T) {\n\tt.Skip("later")')})
    assert ti.SKIP_ADDED in kinds(r)


def test_go_test_that_lost_its_assertions_is_flagged(repo):
    base = commit(repo, {"calc_test.go": GO_BASE})
    weaker = GO_BASE.replace('\tif Add(1, 2) != 3 {\n\t\tt.Errorf("want 3")\n\t}\n', "\t_ = Add(1, 2)\n")
    r = scan(repo, base, {"calc_test.go": weaker})
    assert ti.LOST_ASSERTIONS in kinds(r)
    assert "'TestAdd'" in r.summary()


def test_go_removed_test_function_is_flagged(repo):
    base = commit(repo, {"calc_test.go": GO_BASE})
    r = scan(repo, base, {"calc_test.go": GO_BASE.split("func TestSub")[0]})
    assert ti.REMOVED_TEST in kinds(r)
    assert "'TestSub'" in r.summary()


# ----------------------------------------------------------------------------------------- Rust
def test_rust_ignore_is_flagged(repo):
    src = "#[test]\nfn adds() {\n    assert_eq!(add(1, 2), 3);\n}\n"
    base = commit(repo, {"crate/tests/it.rs": src})
    r = scan(repo, base, {"crate/tests/it.rs": src.replace("#[test]", "#[test]\n#[ignore]")})
    assert ti.SKIP_ADDED in kinds(r)


def test_rust_assert_true_is_flagged(repo):
    src = "#[test]\nfn adds() {\n    assert_eq!(add(1, 2), 3);\n}\n"
    base = commit(repo, {"crate/tests/it.rs": src})
    r = scan(repo, base, {"crate/tests/it.rs": src.replace("assert_eq!(add(1, 2), 3)", "assert!(true)")})
    assert {ti.TRIVIAL_ASSERT, ti.LOST_ASSERTIONS} <= kinds(r)
    assert "'adds' lost 1 assertion and no longer asserts anything" in r.summary()


# --------------------------------------------------------------------------------------- policy
def test_default_policy_enables_it_with_a_reviewing_floor():
    import yaml

    p = policy_from_dict(yaml.safe_load(default_policy_text()))
    assert p.test_integrity.enabled
    assert p.test_integrity.tier == "high"
    assert p.requirement(p.test_integrity.tier).independent_review
    assert "**/tests/**" in p.test_integrity.test_globs


def test_policy_defaults_and_validation():
    import yaml

    raw = yaml.safe_load(default_policy_text())
    del raw["test_integrity"]
    assert policy_from_dict(raw).test_integrity.tier == "high"  # lowest tier needing independent review
    raw["test_integrity"] = {"enabled": False}
    assert not policy_from_dict(raw).test_integrity.enabled
    assert policy_from_dict(raw).test_integrity.test_globs  # defaults fill in
    raw["test_integrity"] = {"tier": "nope"}
    with pytest.raises(PolicyError):
        policy_from_dict(raw)
    raw["test_integrity"] = {"enabled": "yes"}
    with pytest.raises(PolicyError):
        policy_from_dict(raw)
    raw["test_integrity"] = {"test_globs": "tests/**"}
    with pytest.raises(PolicyError):
        policy_from_dict(raw)


# ----------------------------------------------------------------- follow-up round: edge cases
def test_moving_a_test_into_a_helper_the_runner_does_not_collect_is_a_removal(repo):  # F1
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": "def test_add():\n    assert add(1, 2) == 3\n",
                          "tests/helpers.py": "def test_neg():\n    assert add(-1, 1) == 0\n"})
    assert ti.REMOVED_TEST in kinds(r)
    assert "'test_neg'" in r.summary()


def test_moving_a_test_into_a_collected_file_is_still_fine(repo):  # F1, F10
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": "", "tests/test_calc.py": PY_BASE})
    assert not r.flagged, r.summary()


def test_emptied_file_counts_only_tests_that_are_gone(repo):  # F10
    base = commit(repo, {"tests/test_app.py": PY_BASE, "tests/test_other.py": "def test_x():\n    assert 1 < 2\n"})
    r = scan(repo, base, {"tests/test_app.py": "", "tests/test_other.py":
                          "def test_x():\n    assert 1 < 2\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"})
    assert ti.EMPTIED_FILE in kinds(r)
    assert "1 test removed" in r.summary()  # test_neg only; test_add moved


@pytest.mark.parametrize("body", [
    "    return\n    assert add(1, 2) == 3\n",
    "    raise RuntimeError('x')\n    assert add(1, 2) == 3\n",
    "    if False:\n        assert add(1, 2) == 3\n",
    "    if 0:\n        assert add(1, 2) == 3\n",
    "    try:\n        assert add(1, 2) == 3\n    except AssertionError:\n        pass\n",
    "    try:\n        assert add(1, 2) == 3\n    except:\n        pass\n",
])
def test_assertions_that_can_never_fail_are_flagged(repo, body):  # F3
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace("    assert add(1, 2) == 3\n", body)})
    assert ti.NEUTRALIZED in kinds(r), r.summary()


def test_a_handler_that_reraises_is_not_flagged(repo):  # F3
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    body = "    try:\n        assert add(1, 2) == 3\n    except AssertionError:\n        log()\n        raise\n"
    assert not scan(repo, base, {"tests/test_app.py": PY_BASE.replace("    assert add(1, 2) == 3\n", body)}).flagged


def test_block_commenting_a_js_test_does_not_hide_it(repo):  # F4
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    commented = JS_BASE.replace('  it("subtracts"', '  /* it("subtracts"').replace("  });\n});", "  }); */\n});")
    r = scan(repo, base, {"web/math.test.ts": commented})
    assert ti.REMOVED_TEST in kinds(r), r.summary()


def test_block_commenting_a_rust_test_does_not_hide_it(repo):  # F4
    src = "#[test]\nfn adds() {\n    assert_eq!(add(1, 2), 3);\n}\n"
    base = commit(repo, {"crate/tests/it.rs": src})
    r = scan(repo, base, {"crate/tests/it.rs": "/*\n" + src + "*/\n"})
    assert ti.REMOVED_TEST in kinds(r) or ti.EMPTIED_FILE in kinds(r)


def test_comment_markers_inside_strings_are_not_comments(repo):  # F4
    js = 'it("url", () => {\n  expect(get("http://x/*")).toBe(1);\n});\n'
    base = commit(repo, {"web/a.test.js": js})
    assert not scan(repo, base, {"web/a.test.js": js + "// done\n"}).flagged


def test_new_assertions_elsewhere_do_not_hide_a_loss(repo):  # F5
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    weaker = PY_BASE.replace("    assert add(-1, -1) == -2\n", "")
    extra = "".join(f"def test_n{i}():\n    assert {i} + 1 == {i + 1}\n\n\n" for i in range(5))
    r = scan(repo, base, {"tests/test_app.py": weaker, "tests/test_new.py": extra})
    assert ti.LOST_ASSERTIONS in kinds(r)
    assert "'test_neg' lost 1 assertion" in r.summary()


@pytest.mark.parametrize("marker", ["pytest.importorskip('numpy')", "raise SkipTest('x')"])
def test_more_python_skips(repo, marker):  # F6
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/test_app.py": PY_BASE.replace(
        "    assert add(1, 2) == 3", f"    {marker}\n    assert add(1, 2) == 3")})
    assert ti.SKIP_ADDED in kinds(r)


@pytest.mark.parametrize("edit", [('it("adds"', 'it.skipIf(true)("adds"'), ('it("adds"', 'it.runIf(false)("adds"'),
                                   ('describe("math"', 'describe.todo("math"')])
def test_more_js_skips(repo, edit):  # F6
    base = commit(repo, {"web/math.test.ts": JS_BASE})
    assert ti.SKIP_ADDED in kinds(scan(repo, base, {"web/math.test.ts": JS_BASE.replace(*edit)}))


def test_new_non_test_fixture_that_does_not_parse_is_ignored(repo):  # F9
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    r = scan(repo, base, {"tests/fixtures/broken.py": "def (:\n", "tests/test_app.py": PY_BASE + "\n# note\n"})
    assert not r.flagged, r.summary()


def test_deleting_a_broken_test_file_is_reported_as_a_deletion(repo):  # F9
    base = commit(repo, {"tests/test_app.py": PY_BASE, "tests/test_broken.py": "def test_x(:\n"})
    r = scan(repo, base, {"tests/test_broken.py": None})
    assert kinds(r) == {ti.REMOVED_FILE}


def test_modifying_a_test_file_into_a_syntax_error_still_fails_closed(repo):  # F9
    base = commit(repo, {"tests/helpers.py": "x = 1\n"})
    assert kinds(scan(repo, base, {"tests/helpers.py": "x = (\n"})) == {ti.UNANALYZABLE}


def test_a_file_changed_after_capture_is_unanalyzable(repo):  # F11
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    files = change(repo, {"tests/test_app.py": PY_BASE + "\n# a\n"})
    (repo / "tests/test_app.py").write_text(PY_BASE.replace("== 3", "== 4"), encoding="utf-8")
    r = ti.analyze(repo, base, files)
    assert kinds(r) == {ti.UNANALYZABLE} and "changed during analysis" in r.summary()


@pytest.mark.parametrize("path, text", [
    ("tests/conftest.py", "import pytest\n"),
    ("pytest.ini", "[pytest]\naddopts = -k nothing\n"),
    ("tox.ini", "[testenv]\ncommands = true\n"),
    ("jest.config.js", "module.exports = { testPathIgnorePatterns: ['/'] }\n"),
    ("vitest.config.ts", "export default { test: { exclude: ['**'] } }\n"),
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-k nothing'\n"),
    ("package.json", '{"jest": {"testMatch": []}}\n'),
])
def test_runner_config_changes_are_signals(repo, path, text):  # F2
    base = commit(repo, {"README.md": "x\n"})
    r = scan(repo, base, {path: text})
    assert ti.RUNNER_CONFIG in kinds(r), r.summary()


def test_unrelated_config_changes_are_not_signals(repo):  # F2
    base = commit(repo, {"pyproject.toml": "[project]\nname = 'a'\n", "package.json": '{"jest": {"a": 1}, "x": 1}\n'})
    r = scan(repo, base, {"pyproject.toml": "[project]\nname = 'b'\n", "package.json": '{"jest": {"a": 1}, "x": 2}\n'})
    assert not r.flagged, r.summary()


def test_agent_controlled_names_cannot_forge_scan_output(repo):  # F13
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    literal = repr("a`b```c" + "z" * 300)
    evil = PY_BASE.replace("add(1, 2) == 3", f"add(1, 2) == {literal}")
    r = scan(repo, base, {"tests/test_app.py": evil})
    section = r.review_section()
    assert r.signals and "```" not in r.summary()
    assert all(len(s.detail) < 400 for s in r.signals)
    # the fence is longer than any backtick run inside the hunk
    hunk = "line\n````\ninside\n````"
    forged = ti.IntegrityReport((ti.Signal(ti.SKIP_ADDED, "t.py", "d"),), hunks={"t.py": hunk})
    assert "`````diff" in forged.review_section()
    assert section


def test_rust_files_with_inline_tests_are_scanned(repo):  # F14
    src = "pub fn add(a: i32, b: i32) -> i32 { a + b }\n#[cfg(test)]\nmod t {\n    #[test]\n    fn adds() {\n        assert_eq!(super::add(1, 2), 3);\n    }\n}\n"
    base = commit(repo, {"src/lib.rs": src})
    r = scan(repo, base, {"src/lib.rs": src.replace("#[test]", "#[test]\n    #[ignore]")})
    assert ti.SKIP_ADDED in kinds(r)


def test_more_default_globs():  # F14
    for path in ("web/__tests__/a.js", "pkg/test/a.py", "web/a.test.mjs", "web/a.test.cjs"):
        assert ti.is_test_path(path), path


def test_runs_pinned_before_the_feature_replay_with_it_disabled():  # F12
    import yaml

    from daedalus.core.run import _pinned_policy

    raw = yaml.safe_load(default_policy_text())
    assert _pinned_policy(raw).test_integrity.enabled
    del raw["test_integrity"]
    assert not _pinned_policy(raw).test_integrity.enabled


# ------------------------------------------------------------ review round 1: config narrowing
def test_unrelated_sections_of_shared_config_files_are_not_signals(repo):
    base = commit(repo, {
        "setup.cfg": "[metadata]\nversion = 1.0\n\n[flake8]\nmax-line-length = 80\n\n[tool:pytest]\naddopts = -q\n",
        "tox.ini": "[tox]\nenvlist = py\n\n[testenv]\ncommands = pytest\n\n[flake8]\nignore = E1\n",
    })
    r = scan(repo, base, {
        "setup.cfg": "[metadata]\nversion = 1.1\n\n[flake8]\nmax-line-length = 100\n\n[tool:pytest]\naddopts = -q\n",
        "tox.ini": "[tox]\nenvlist = py311\n\n[testenv]\ncommands = pytest\n\n[flake8]\nignore = E2\n",
    })
    assert not r.flagged, r.signals


def test_pytest_sections_of_setup_cfg_and_tox_ini_are_still_signals(repo):
    base = commit(repo, {"setup.cfg": "[tool:pytest]\naddopts = -q\n",
                         "tox.ini": "[testenv]\ncommands = pytest\n"})
    r = scan(repo, base, {"setup.cfg": "[tool:pytest]\naddopts = -q --deselect tests/test_a.py\n",
                          "tox.ini": "[testenv]\ncommands = true\n"})
    assert {s.path for s in r.signals} == {"setup.cfg", "tox.ini"} and kinds(r) == {ti.RUNNER_CONFIG}


def test_adding_or_deleting_a_config_with_no_runner_section_is_not_a_signal(repo):
    base = commit(repo, {"README.md": "x\n", "old/package.json": '{"name": "old"}\n',
                         "old/pyproject.toml": "[project]\nname = 'old'\n"})
    r = scan(repo, base, {
        "web/package.json": '{"name": "web", "dependencies": {"left-pad": "1"}}\n',
        "lib/pyproject.toml": "[project]\nname = 'lib'\n",
        "old/package.json": None,
        "old/pyproject.toml": None,
    })
    assert not r.flagged, r.signals


def test_adding_a_config_that_does_configure_the_runner_is_a_signal(repo):
    base = commit(repo, {"README.md": "x\n"})
    r = scan(repo, base, {"lib/pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--co'\n"})
    assert kinds(r) == {ti.RUNNER_CONFIG}


def test_editing_the_npm_test_script_is_a_signal(repo):
    base = commit(repo, {"package.json": '{"name": "a", "scripts": {"test": "vitest run", "build": "tsc"}}\n'})
    unrelated = scan(repo, base, {"package.json": '{"name": "a", "scripts": {"test": "vitest run", "build": "tsc -b"}}\n'})
    assert not unrelated.flagged
    gutted = scan(repo, base, {"package.json": '{"name": "a", "scripts": {"test": "exit 0", "build": "tsc"}}\n'})
    assert kinds(gutted) == {ti.RUNNER_CONFIG}
    pre = scan(repo, base, {"package.json": '{"name": "a", "scripts": {"test": "vitest run", "pretest": "exit 0"}}\n'})
    assert kinds(pre) == {ti.RUNNER_CONFIG}


def test_editing_the_make_test_target_is_a_signal_but_other_targets_are_not(repo):
    base = commit(repo, {"Makefile": "build:\n\tcc a.c\n\ntest:\n\tpytest -q\n"})
    assert not scan(repo, base, {"Makefile": "build:\n\tcc -O2 a.c\n\ntest:\n\tpytest -q\n"}).flagged
    r = scan(repo, base, {"Makefile": "build:\n\tcc a.c\n\ntest:\n\ttrue\n"})
    assert kinds(r) == {ti.RUNNER_CONFIG} and "Makefile" in r.paths


# ----------------------------------------------------------------- Django / unittest layouts
@pytest.mark.parametrize("path", ["app/tests.py", "app/test.py", "app/models_tests.py"])
def test_django_and_unittest_module_names_are_test_files(repo, path):
    assert ti.is_test_path(path)
    assert ti.is_collected(path)
    base = commit(repo, {path: "def test_a():\n    assert f() == 3\n"})
    r = scan(repo, base, {path: "def test_a():\n    assert True\n"})
    assert ti.TRIVIAL_ASSERT in kinds(r) or ti.LOST_ASSERTIONS in kinds(r) or ti.NEUTRALIZED in kinds(r), r.signals


def test_default_policy_globs_match_the_code_defaults():
    import yaml

    raw = yaml.safe_load(default_policy_text())
    assert set(raw["test_integrity"]["test_globs"]) == set(ti.DEFAULT_TEST_GLOBS)


# -------------------------------------------------------- runs record the feature they pinned
def test_a_policy_file_without_the_key_is_enabled_for_new_runs_and_not_for_old_ones():
    import yaml

    from daedalus.core.run import POLICY_FEATURES, _pinned_policy

    raw = yaml.safe_load(default_policy_text())
    del raw["test_integrity"]  # a complete policy_file written before the feature
    assert policy_from_dict(raw).test_integrity.enabled  # what `daedalus ci` / `daedalus policy` report
    assert _pinned_policy(raw, list(POLICY_FEATURES)).test_integrity.enabled  # a new run agrees
    assert not _pinned_policy(raw).test_integrity.enabled  # a run pinned before the feature
    assert not _pinned_policy(raw, []).test_integrity.enabled


# ----------------------------------------------------------- skips in brand-new test files
def test_a_skip_in_a_brand_new_test_file_removes_no_coverage(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    new = 'import pytest\n\n\ndef test_schema():\n    js = pytest.importorskip("jsonschema")\n    assert js\n'
    r = scan(repo, base, {"tests/test_schema.py": new})
    assert ti.SKIP_ADDED not in kinds(r)


def test_a_test_moved_into_a_new_file_and_skipped_is_still_flagged(repo):
    base = commit(repo, {"tests/test_app.py": PY_BASE})
    moved = "import pytest\n\n\n@pytest.mark.skip(reason='later')\ndef test_add():\n    assert add(1, 2) == 3\n"
    rest = PY_BASE.split("def test_neg")[0].replace("def test_add():\n    assert add(1, 2) == 3\n", "")
    r = scan(repo, base, {"tests/test_app.py": rest + "def test_neg" + PY_BASE.split("def test_neg")[1],
                          "tests/test_moved.py": moved})
    assert ti.SKIP_ADDED in kinds(r)


def test_a_focus_marker_in_a_new_js_file_is_still_flagged(repo):
    base = commit(repo, {"tests/app.test.js": "it('adds', () => { expect(add(1, 2)).toBe(3); });\n"})
    r = scan(repo, base, {"tests/other.test.js": "it.only('x', () => { expect(1).toBe(1); });\n"})
    assert ti.FOCUS_ADDED in kinds(r)
