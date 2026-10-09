"""The Library's first layout waits for the window to reach its real size.

It is first shown at its minimum size and grows to the size it asked for a
moment later; laying out against the small one and then again against the
real one was the tab visibly re-arranging itself on every launch.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab   # noqa: E402


class Root:
    def __init__(self, w, h, viewable=True):
        self.w, self.h, self.viewable = w, h, viewable

    def winfo_viewable(self):
        return self.viewable

    def winfo_width(self):
        return self.w

    def winfo_height(self):
        return self.h


def tab(root, requested=(1996, 1180), started_ago=0.0):
    t = type("Tab", (), {"WINDOW_SETTLE_S": LibraryTab.WINDOW_SETTLE_S,
                         "_window_ready": LibraryTab._window_ready})()
    t.root = root
    t.app = type("App", (), {"requested_size": requested,
                             "started_at": time.monotonic() - started_ago})()
    return t


def test_not_ready_while_still_at_the_minimum_size():
    assert tab(Root(1180, 700))._window_ready() is False


def test_not_ready_before_it_is_on_screen():
    assert tab(Root(1996, 1180, viewable=False))._window_ready() is False


def test_ready_at_the_size_it_asked_for():
    assert tab(Root(1996, 1180))._window_ready() is True


def test_once_ready_it_stays_ready_whatever_the_size():
    """A window resized by hand afterwards is still laid out."""
    t = tab(Root(1996, 1180))
    assert t._window_ready()
    t.root.w = 1300
    assert t._window_ready() is True


def test_never_waits_for_ever():
    """A window maximised straight away never reaches the asked-for size."""
    assert tab(Root(2560, 1400), started_ago=LibraryTab.WINDOW_SETTLE_S + 1)._window_ready()
