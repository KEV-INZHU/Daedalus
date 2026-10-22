"""`daedalus demo` deletes its throwaway repository with `remove_tree`. Git writes objects
read-only: Windows refuses to delete a read-only file, POSIX refuses to delete anything in a
read-only directory. Plain file operations only, so this runs on every OS."""

from __future__ import annotations

import os
import stat

import pytest

from daedalus import demo

# Root ignores POSIX permission bits, so these would pass without testing anything.
needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
)


def test_remove_tree_deletes_read_only_files(tmp_path):
    d = tmp_path / "project" / ".git" / "objects" / "05"
    d.mkdir(parents=True)
    f = d / "2a10e3"
    f.write_text("x")
    f.chmod(stat.S_IREAD)
    demo.remove_tree(tmp_path / "project")
    assert not (tmp_path / "project").exists()


@needs_permissions
def test_remove_tree_deletes_files_in_read_only_directories(tmp_path):
    d = tmp_path / "project" / ".git" / "objects" / "05"
    d.mkdir(parents=True)
    f = d / "2a10e3"
    f.write_text("x")
    f.chmod(stat.S_IREAD)
    d.chmod(stat.S_IREAD | stat.S_IEXEC)
    demo.remove_tree(tmp_path / "project")
    assert not (tmp_path / "project").exists()


@needs_permissions
@pytest.mark.skipif(os.name == "nt", reason="Windows has no unreadable-directory permission bits")
def test_remove_tree_deletes_the_contents_of_unreadable_directories(tmp_path):
    d = tmp_path / "project" / "locked"
    (d / "inner").mkdir(parents=True)
    (d / "inner" / "f").write_text("x")
    d.chmod(0)
    demo.remove_tree(tmp_path / "project")
    assert not (tmp_path / "project").exists()


@needs_permissions
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_remove_tree_never_changes_the_directory_that_holds_it(tmp_path):
    holder = tmp_path / "holder"
    (holder / "project").mkdir(parents=True)
    holder.chmod(0o555)  # removing `project` fails: the retry must not unlock the holder to force it
    try:
        demo.remove_tree(holder / "project")
        assert stat.S_IMODE(holder.stat().st_mode) == 0o555
    finally:
        holder.chmod(0o755)
