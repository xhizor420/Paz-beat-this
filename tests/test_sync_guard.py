"""The library index must survive a drive that isn't there.

A sync that finds no files looks exactly like a library where every file
was deleted, and the sync used to believe it: every row dropped from the
index and every thumbnail removed from disk, in one press, with no
warning. An unmounted drive, a renamed folder or a changed drive letter
all produce that. These pin the refusal.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402


class FakeTab:
    """Just enough of the tab to exercise the decision, without Tk."""
    SYNC_GUARD_FLOOR = LibraryTab.SYNC_GUARD_FLOOR
    SYNC_GUARD_SHARE = LibraryTab.SYNC_GUARD_SHARE
    _refuse_mass_delete = LibraryTab._refuse_mass_delete

    def __init__(self, dirs):
        self._dirs = dirs

    def library_dirs(self):
        return self._dirs


def library(n):
    return {f"P:/clips/{i}.mp4": (i, i) for i in range(n)}


def test_unreachable_folder_stops_the_sync():
    known = library(10_000)
    tab = FakeTab(dirs=[])                       # drive not mounted
    reason = tab._refuse_mass_delete(list(known), known, {})
    assert reason and "isn't reachable" in reason


def test_reachable_but_empty_folder_stops_the_sync():
    known = library(10_000)
    tab = FakeTab(dirs=["P:/clips"])             # folder there, contents gone
    reason = tab._refuse_mass_delete(list(known), known, {})
    assert reason and "10,000" in reason


def test_a_folder_that_moved_stops_the_sync():
    known = library(10_000)
    on_disk = {f"Q:/clips/{i}.mp4": (i, i) for i in range(10_000)}
    tab = FakeTab(dirs=["P:/clips"])             # same clips, new drive letter
    reason = tab._refuse_mass_delete(list(known), known, on_disk)
    assert reason and "folder moved" in reason


def test_deleting_a_few_clips_is_still_allowed():
    known = library(10_000)
    on_disk = {p: sig for p, sig in list(known.items())[:9_800]}
    gone = [p for p in known if p not in on_disk]
    tab = FakeTab(dirs=["P:/clips"])
    assert tab._refuse_mass_delete(gone, known, on_disk) is None


def test_a_small_library_is_still_protected_from_finding_nothing():
    """An indexed library does not go to zero files on its own, whatever
    its size."""
    known = library(10)
    tab = FakeTab(dirs=["P:/clips"])
    reason = tab._refuse_mass_delete(list(known), known, {})
    assert reason and "no matching files at all" in reason


def test_a_small_library_is_not_second_guessed_proportionally():
    """The share check does need a floor: while folders are still being
    set up, losing most of a handful of clips is expected."""
    known = library(10)
    on_disk = {p: sig for p, sig in list(known.items())[:2]}
    gone = [p for p in known if p not in on_disk]
    tab = FakeTab(dirs=["P:/clips"])
    assert tab._refuse_mass_delete(gone, known, on_disk) is None


def test_an_empty_index_is_not_second_guessed():
    tab = FakeTab(dirs=["P:/clips"])
    assert tab._refuse_mass_delete([], {}, {}) is None
