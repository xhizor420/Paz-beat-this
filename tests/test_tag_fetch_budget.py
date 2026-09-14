"""The quiet tag fetch must not lock the tab for an evening.

Opening the Library (or finishing a sync) kicks off an ambient e621
fetch for anything untagged. e621 allows about two requests a second, so
a library with ten thousand untagged post IDs is hours of fetching - and
the whole time, the tab is busy, which means Sync and Fetch are both
refused. Ambient work takes a batch and leaves the rest for next time.
Pressing Fetch yourself is the unbudgeted way, and stays that way.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402


class Rec:
    def __init__(self, pid):
        self.pid = pid


class Cfg:
    e621_enabled = True
    library_stale_refresh_budget = 40


class Meta:
    def __init__(self, cached=()):
        self.cached = set(cached)

    def get(self, pid):
        return {"tags": []} if pid in self.cached else None

    def due_for_refresh(self, pids, budget, exclude=()):
        return []


class FakeTab:
    _fetch_tags = LibraryTab._fetch_tags

    def __init__(self, count=10_000, cached=()):
        self.busy = False
        self.cfg = Cfg()
        self.emeta = Meta(cached)
        self.records = [Rec(str(1000 + i)) for i in range(count)]
        self.status = []
        self.fetched = None

    def set_status(self, text, colour=None):
        self.status.append(text)

    def _run_fetch(self, todo, refreshing, note=""):
        self.fetched = (list(todo), refreshing, note)

    def F(self, key, **fmt):
        return key


def test_the_ambient_fetch_takes_a_batch_not_the_whole_library():
    tab = FakeTab(count=10_000)
    tab._fetch_tags()
    todo, _refreshing, note = tab.fetched
    assert len(todo) == Cfg.library_stale_refresh_budget
    assert "9,960 more next time" in note


def test_pressing_fetch_yourself_still_catches_up_everything():
    tab = FakeTab(count=10_000)
    tab._fetch_tags(full=True)
    todo, _refreshing, note = tab.fetched
    assert len(todo) == 10_000
    assert note == ""


def test_a_small_backlog_is_done_in_one_go_with_nothing_left_over():
    tab = FakeTab(count=12)
    tab._fetch_tags()
    todo, _refreshing, note = tab.fetched
    assert len(todo) == 12
    assert note == ""


def test_nothing_untagged_means_no_fetch_at_all():
    tab = FakeTab(count=5, cached=[str(1000 + i) for i in range(5)])
    tab._fetch_tags()
    assert tab.fetched is None
    assert tab.status and "already tagged" in tab.status[-1]


def test_a_busy_tab_is_left_alone():
    tab = FakeTab(count=100)
    tab.busy = True
    tab._fetch_tags()
    assert tab.fetched is None
