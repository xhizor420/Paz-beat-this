"""Resolve's proxy folders must never be walked into.

Resolve keeps editing proxies beside the masters - a "Proxies" or "mov"
folder inside each category. Those are not library clips. Indexing one
shows every clip twice, and counting one as a 4K upscale marks a file
done that never was.

files.py has said since it was written that "every directory walk in this
suite skips them", and provides prune_dirs to do it. Nothing called it.
The one recursive scan in the app walked every proxy tree there was,
stat-ing each file in it over a library of several terabytes on every
sync, and the per-file check it relied on instead could only see the
file's own parent - so a proxy one folder deeper was indexed as a clip.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.files import in_ignored_path, prune_dirs       # noqa: E402


def build(root):
    """A category folder shaped like a real one."""
    for folder in ("", "Proxies", "Proxies/2024", "mov",
                   "Wolves", "Wolves/Proxies"):
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    made = []
    for folder in ("", "Proxies", "Proxies/2024", "mov",
                   "Wolves", "Wolves/Proxies"):
        path = os.path.join(root, folder, "clip.mp4")
        open(path, "w").close()
        made.append(path)
    return made


def scan(root, prune: bool):
    """The shape of the sync's walk, with and without the pruning."""
    found = []
    for base, dirs, names in os.walk(root):
        if prune:
            prune_dirs(dirs)
        for name in names:
            path = os.path.join(base, name)
            if not in_ignored_path(path, root):
                found.append(os.path.relpath(path, root))
    return sorted(found)


def test_the_walk_keeps_only_the_real_clips(tmp_path):
    build(str(tmp_path))
    assert scan(str(tmp_path), prune=True) == [
        os.path.join("Wolves", "clip.mp4"),
        "clip.mp4",
    ]


def test_a_proxy_two_folders_down_was_the_one_that_got_through(tmp_path):
    """The failure the root argument fixes: without it in_ignored_path
    can only look at the file's own parent, and "2024" is not a proxy
    folder name."""
    build(str(tmp_path))
    deep = os.path.join(str(tmp_path), "Proxies", "2024", "clip.mp4")
    assert in_ignored_path(deep, str(tmp_path)) is True
    assert in_ignored_path(deep) is False, (
        "if this ever passes, the rootless call is safe and the root "
        "argument is no longer load-bearing")


def test_pruning_stops_the_walk_entering_a_proxy_tree(tmp_path):
    """Not just filtering afterwards - the point is the files inside are
    never looked at. On a several-terabyte library that is the sync's
    running time."""
    build(str(tmp_path))
    visited = []
    for base, dirs, _names in os.walk(str(tmp_path)):
        prune_dirs(dirs)
        visited.append(os.path.relpath(base, str(tmp_path)))
    assert sorted(visited) == [".", "Wolves"]


def test_prune_dirs_edits_the_list_in_place(tmp_path):
    """os.walk only honours changes made to the list it handed over."""
    dirs = ["Wolves", "Proxies", "mov", "Foxes"]
    same = dirs
    prune_dirs(dirs)
    assert same is dirs
    assert dirs == ["Wolves", "Foxes"]


def test_a_category_folder_may_be_named_like_a_proxy_folder(tmp_path):
    """A folder the user picked as a category is a category, whatever it
    is called - the check starts below the root, not at it."""
    root = tmp_path / "mov"
    root.mkdir()
    (root / "clip.mp4").write_text("")
    assert in_ignored_path(str(root / "clip.mp4"), str(root)) is False


def test_nothing_is_dropped_from_an_ordinary_tree(tmp_path):
    for folder in ("Wolves", "Foxes", "Wolves/2024"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / folder / "clip.mp4").write_text("")
    assert len(scan(str(tmp_path), prune=True)) == 3
