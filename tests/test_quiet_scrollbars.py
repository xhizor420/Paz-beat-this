"""CTk scrollbars must not lay out the whole window every time they move.

CTkScrollbar._draw ends in update_idletasks(), which runs every pending
layout in the application, not just the scrollbar's. The gallery calls
the bar's set() each time its view moves, so a scroll, a resize or
theater re-ran the window's layout from inside a callback, nested in the
layout pass that had moved the view - 18-32ms of a theater toggle.
"""

from __future__ import annotations

import os
import sys

import customtkinter as ctk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                 # noqa: E402


class Canvas:
    def __init__(self):
        self.flushes = 0

    def update_idletasks(self):
        self.flushes += 1


def fake_scrollbar_class():
    class Bar:
        def __init__(self):
            self._canvas = Canvas()
            self.drawn = 0

        def _draw(self, no_color_updates=False):
            self.drawn += 1
            self._canvas.update_idletasks()    # what CTk does
    return Bar


def test_a_redraw_no_longer_flushes_the_window(monkeypatch):
    Bar = fake_scrollbar_class()
    monkeypatch.setattr(ctk, "CTkScrollbar", Bar)
    theme.quiet_scrollbars()
    bar = Bar()
    bar._draw()
    bar._draw()
    assert bar.drawn == 2, "the bar itself must still be drawn"
    assert bar._canvas.flushes == 0


def test_other_canvases_keep_their_flush(monkeypatch):
    Bar = fake_scrollbar_class()
    monkeypatch.setattr(ctk, "CTkScrollbar", Bar)
    theme.quiet_scrollbars()
    Bar()._draw()
    other = Canvas()
    other.update_idletasks()
    assert other.flushes == 1


def test_installing_twice_wraps_once(monkeypatch):
    Bar = fake_scrollbar_class()
    monkeypatch.setattr(ctk, "CTkScrollbar", Bar)
    theme.quiet_scrollbars()
    first = Bar._draw
    theme.quiet_scrollbars()
    assert Bar._draw is first
