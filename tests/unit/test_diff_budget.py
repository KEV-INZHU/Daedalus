"""A reviewer's diff over the size limit keeps a share of every file and names what it cut."""

from daedalus.repository.candidate import fit_diff, split_diff


def _file(path: str, lines: int) -> tuple[str, str]:
    hunks = "".join(f"@@ -{i},1 +{i},1 @@\n-old {i}\n+new {i} {'x' * 40}\n" for i in range(lines))
    return path, f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunks}".rstrip("\n")


def test_a_diff_within_the_limit_is_unchanged():
    chunks = [_file("a.py", 3), _file("b.py", 2)]
    assert fit_diff(chunks, 10_000) == "\n".join(c for _, c in chunks)


def test_the_last_file_is_not_dropped_when_an_early_one_is_huge():
    chunks = [_file("src/big.py", 2000), _file("src/small.py", 3), _file("tests/test_big.py", 2000)]
    out = fit_diff(chunks, 20_000)
    assert len(out) <= 20_000
    assert chunks[1][1] in out  # the small file is whole
    assert "+++ b/tests/test_big.py" in out  # the test file at the end still gets its share
    assert "tests/test_big.py: shown" in out and "src/big.py: shown" in out
    assert "Cut short (read these in the repository before judging them): src/big.py, tests/test_big.py" in out


def test_a_cut_lands_on_a_hunk_boundary():
    path, body = _file("a.py", 500)
    out = fit_diff([(path, body), _file("b.py", 500)], 8_000)
    shown = out.split("\n... a.py: shown", 1)[0]
    assert shown.endswith("x" * 40)  # a whole hunk, not half a line


def test_files_with_no_useful_room_are_listed_not_silently_lost():
    chunks = [_file(f"f{i}.py", 300) for i in range(60)]
    out = fit_diff(chunks, 10_000)
    assert "Not shown at all (read these in the repository): " in out
    assert len(out) <= 10_000
    last = out.rsplit("\n", 1)[-1]
    shown = sum(f"+++ b/f{i}.py" in out for i in range(60))
    named = sum(f"f{i}.py (" in last for i in range(60))
    more = int(last.rsplit("and ", 1)[1].split()[0]) if " more" in last else 0
    assert shown + named + more == 60  # every file is shown in part, named, or counted


def test_the_output_never_exceeds_the_limit_however_long_the_paths():
    import random

    rnd = random.Random(7)
    for limit in (2_000, 5_000, 20_000):
        chunks = [_file(f"{'deep/' * rnd.randint(1, 30)}f{i}.py", rnd.randint(1, 400)) for i in range(rnd.randint(2, 80))]
        assert len(fit_diff(chunks, limit)) <= limit


def test_split_diff_keeps_paths_and_order():
    text = "\n".join(c for _, c in [_file("a.py", 1), _file("dir/b c.py", 1)])
    assert [p for p, _ in split_diff(text)] == ["a.py", "dir/b c.py"]
    assert "\n".join(c for _, c in split_diff(text)) == text


def test_split_diff_reads_the_path_from_the_file_lines():
    text = ("diff --git a/x b/y b/x b/y b/z\n--- a/x b/y\n+++ b/x b/y b/z\n@@ -1 +1 @@\n-a\n+b")
    assert split_diff(text)[0][0] == "x b/y b/z"


def test_paths_with_spaces_or_quotes_are_read_cleanly():
    tabbed = "diff --git a/my file.py b/my file.py\n--- a/my file.py\t\n+++ b/my file.py\t\n@@ -1 +1 @@\n-a\n+b"
    assert split_diff(tabbed)[0][0] == "my file.py"
    quoted = 'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b'
    assert split_diff(quoted)[0][0] == "café.py"
    new = "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+--- x"
    assert split_diff(new)[0][0] == "n.py"


def test_deleted_binary_and_mode_only_files_keep_their_paths():
    deleted = "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x"
    assert split_diff(deleted)[0][0] == "old.py"
    binary = 'diff --git "a/caf\\303\\251.png" "b/caf\\303\\251.png"\nBinary files differ'
    assert split_diff(binary)[0][0] == "café.png"
    mode = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755"
    assert split_diff(mode)[0][0] == "run.sh"


def test_kept_files_are_counted_with_their_joiners_and_notes_alone_never_overflow():
    chunks = [_file(f"f{i}.py", 1) for i in range(30)] + [_file("big.py", 400)]
    out = fit_diff(chunks, 6_000)
    assert len(out) <= 6_000 and "cut short here" not in out  # the budget held without the last-resort cut
    tiny = fit_diff([_file("a" * 300 + ".py", 50), _file("b" * 300 + ".py", 50)], 300)
    assert len(tiny) <= 300 and "cut short here" not in tiny


def test_many_small_kept_files_fit_without_the_last_resort_cut():
    chunks = [("t.py", "x") for _ in range(1000)] + [_file("big.py", 2000)]
    out = fit_diff(chunks, 20_000)
    assert len(out) <= 20_000 and "cut short here" not in out


def test_a_very_long_path_is_listed_not_sliced_backwards():
    long = "d/" * 300 + "f.py"
    out = fit_diff([_file(long, 400), _file("b.py", 400)], 3_000)
    assert len(out) <= 3_000
    assert out.count("+new 1 ") < 10  # the long-named file was not kept nearly whole


def test_the_users_diff_settings_do_not_change_the_paths(tmp_path):
    from conftest import git, make_repo
    from daedalus.repository.candidate import capture, diff_text

    root = make_repo(tmp_path / "r")
    git(root, "config", "diff.noprefix", "true")
    git(root, "config", "color.diff", "always")
    (root / "src" / "lib.py").write_text("x = 2\n")
    text = diff_text(root, capture(root, _base(root)))
    assert "+++ b/src/lib.py" in text and "\x1b[" not in text


def _base(root):
    import subprocess

    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
