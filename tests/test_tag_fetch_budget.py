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
    tagging = False
    cancelled = False

    def cancel_tag_fetch(self):
        self.cancelled = True

    def __init__(self, count=10_000, cached=()):
        self.busy = False
        self.cfg = Cfg()
        self.emeta = Meta(cached)
        self.records = [Rec(str(1000 + i)) for i in range(count)]
        self.status = []
        self.fetched = None

    def set_status(self, text, colour=None):
        self.status.append(text)

    def _run_fetch(self, todo, refreshing, note="", ambient=False):
        self.fetched = (list(todo), refreshing, note)
        self.ambient = ambient

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


# ── ambient work must not stand in the user's way ───────────────────────

def test_the_tab_opening_starts_an_ambient_batch():
    tab = FakeTab(count=100)
    tab._fetch_tags()
    assert tab.ambient is True


def test_pressing_fetch_is_not_ambient():
    tab = FakeTab(count=100)
    tab._fetch_tags(full=True)
    assert tab.ambient is False


def test_ambient_work_does_not_start_on_top_of_itself():
    tab = FakeTab(count=100)
    tab.tagging = True
    tab._fetch_tags()
    assert tab.fetched is None


def test_pressing_fetch_cuts_in_on_ambient_work():
    """e621 allows two requests a second, so a batch runs for a while. The
    button must not have to queue behind one the app started itself."""
    tab = FakeTab(count=100)
    tab.tagging = True
    tab._fetch_tags(full=True)
    assert tab.cancelled
    assert tab.fetched is not None


# ── a finished run must not tidy away the one that replaced it ──────────

class Button:
    def __init__(self):
        self.state = "disabled"

    def configure(self, state=None, **kw):
        if state:
            self.state = state


class Runner:
    _fetch_release = LibraryTab._fetch_release

    def __init__(self):
        self.busy = True
        self._fetch_running = True
        self.more_btn = Button()
        self._fetch_stop = object()

    def ui(self, fn, *a, **kw):
        fn(*a, **kw)


def test_a_finished_run_hands_back_what_it_was_holding():
    tab = Runner()
    assert tab._fetch_release(tab._fetch_stop, ambient=False) is True
    assert tab.busy is False
    assert tab._fetch_running is False
    assert tab.more_btn.state == "normal"


def test_a_cancelled_run_does_not_release_its_replacement():
    """It finishes a moment after the thing that cancelled it started, so
    an unconditional release unlocks the new run's buttons and tells the
    app nothing is fetching while a fetch is underway."""
    tab = Runner()
    stale = tab._fetch_stop
    tab._fetch_stop = object()            # the replacement claims the slot
    assert tab._fetch_release(stale, ambient=True) is False
    assert tab.busy is True
    assert tab._fetch_running is True
    assert tab.more_btn.state == "disabled"


def test_an_ambient_run_never_touches_the_buttons():
    tab = Runner()
    tab.busy = False
    tab._fetch_release(tab._fetch_stop, ambient=True)
    assert tab._fetch_running is False
    assert tab.more_btn.state == "disabled"   # left exactly as it found it
