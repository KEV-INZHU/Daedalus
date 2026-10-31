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
    for i in range(60):  # each file is either shown in part or named as not shown
        assert f"+++ b/f{i}.py" in out or f"f{i}.py (" in out.rsplit("\n", 1)[-1]


def test_split_diff_keeps_paths_and_order():
    text = "\n".join(c for _, c in [_file("a.py", 1), _file("dir/b c.py", 1)])
    assert [p for p, _ in split_diff(text)] == ["a.py", "dir/b c.py"]
    assert "\n".join(c for _, c in split_diff(text)) == text
