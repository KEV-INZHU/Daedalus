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
