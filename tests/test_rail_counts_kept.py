"""The rail's tag counts are kept until the results or their tags change.

Counting every tag across ten thousand clips is 45ms of a worker holding
the interpreter lock, and it was paid for things that change what the
rail shows but not what it counts: folding a group, hiding a tag, and
opening the sidebar again over the same search. Kept counts must never
outlive a change to the results, though - a stale count reads as a fact
about the current search.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import library_tab                           # noqa: E402
from paz_suite.library_tab import LibraryTab                # noqa: E402


class Cfg:
    def __init__(self, sidebar_open=True):
        self.sidebar_open = sidebar_open

    def save(self):
        pass

    save_soon = save


class Hideable:
    def grid(self): pass
    def grid_remove(self): pass
    def configure(self, **_kw): pass


class FakeTab:
    queue_tagpanel = LibraryTab.queue_tagpanel
    _render_tagpanel = LibraryTab._render_tagpanel
    toggle_sidebar = LibraryTab.toggle_sidebar
    _after_vault_change = LibraryTab._after_vault_change
    TAGPANEL_DELAY_MS = LibraryTab.TAGPANEL_DELAY_MS
    SIDEBAR_W = 300

    def __init__(self, sidebar_open=True, visible=True):
        self.cfg = Cfg(sidebar_open)
        self._visible = visible
        self._rail_dirty = False
        self._rail_gen = 0
        self._rail_counts = None
        self._tagpanel_after = None
        self.filtered = ["clip"]
        self.counted_with = []      # what the rail was drawn from
        self.counts_started = 0
        self.side = self.tagpanel = self.side_title = Hideable()
        self.side_hint = self.side_toggle = Hideable()
        self._layout = []
        self.selected = None
        self.renders = 0

    def visible(self):
        return self._visible

    def after(self, _ms, fn):
        fn()
        return "job"

    def after_cancel(self, _job):
        pass

    def _cancel_rail_chunks(self):
        pass

    def _render_soon(self, _ms):
        self.renders += 1

    def _restyle_these(self, *_recs):
        pass

    def _bump_quick_count(self, *_a):
        pass

    def _tagpanel_counted(self, gen, counted):
        if gen != self._rail_gen:
            return
        self._rail_counts = counted
        self.counted_with.append(counted)

    def ui(self, fn, *args):
        fn(*args)


def counting_inline(monkeypatch, tab):
    """Run the counting thread on the spot, and count how often it runs."""
    def count(records):
        tab.counts_started += 1
        return {"n": tab.counts_started}
    monkeypatch.setattr(tab, "_count_tags", count, raising=False)

    class Now:
        def __init__(self, target, daemon=True):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(library_tab.threading, "Thread", Now)


def test_a_new_search_counts(monkeypatch):
    tab = FakeTab()
    counting_inline(monkeypatch, tab)
    tab.queue_tagpanel()
    assert tab.counts_started == 1


def test_folding_a_group_reuses_the_counts(monkeypatch):
    tab = FakeTab()
    counting_inline(monkeypatch, tab)
    tab.queue_tagpanel()
    tab._render_tagpanel()          # what a group toggle or a hide does
    tab._render_tagpanel()
    assert tab.counts_started == 1
    assert len(tab.counted_with) == 3, "the rail must still be redrawn"


def test_a_new_search_after_that_counts_again(monkeypatch):
    tab = FakeTab()
    counting_inline(monkeypatch, tab)
    tab.queue_tagpanel()
    tab._render_tagpanel()
    tab.queue_tagpanel()
    assert tab.counts_started == 2
    assert tab.counted_with[-1] == {"n": 2}


def test_reopening_the_sidebar_over_the_same_search_does_nothing(monkeypatch):
    tab = FakeTab()
    counting_inline(monkeypatch, tab)
    tab.queue_tagpanel()
    tab.toggle_sidebar(force=False)
    tab.toggle_sidebar(force=True)
    assert tab.counts_started == 1
    assert len(tab.counted_with) == 1


def test_a_search_while_shut_is_counted_when_it_opens(monkeypatch):
    tab = FakeTab()
    counting_inline(monkeypatch, tab)
    tab.queue_tagpanel()
    tab.toggle_sidebar(force=False)
    tab.queue_tagpanel()            # searched while it was shut
    assert tab.counts_started == 1
    tab.toggle_sidebar(force=True)
    assert tab.counts_started == 2
    assert tab.counted_with[-1] == {"n": 2}


def test_a_count_still_running_when_the_results_change_is_thrown_away(monkeypatch):
    tab = FakeTab()
    out = []

    class Later:
        def __init__(self, target, daemon=True):
            self.target = target

        def start(self):
            out.append(self.target)
    monkeypatch.setattr(library_tab.threading, "Thread", Later)
    monkeypatch.setattr(tab, "_count_tags", lambda records: {"of": list(records)},
                        raising=False)
    tab.queue_tagpanel()
    tab.filtered = ["other clip"]
    tab.queue_tagpanel()                # the results changed mid-count
    out[0]()                            # the first count lands late
    assert tab._rail_counts is None, "a stale count was kept"
    out[1]()
    assert tab._rail_counts == {"of": ["other clip"]}


def test_a_mark_forgets_the_counts_without_redrawing(monkeypatch):
    """PROJECTS is counted too, and a mark changes it."""
    tab = FakeTab()
    counting_inline(monkeypatch, tab)
    tab.queue_tagpanel()
    tab._after_vault_change([])
    assert tab._rail_counts is None
    assert len(tab.counted_with) == 1
    tab._render_tagpanel()
    assert tab.counts_started == 2
