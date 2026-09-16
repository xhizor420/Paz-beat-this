"""The handle between the gallery and the inspector.

Two ways it can misbehave, both of which it did: taking hold of it moves
the layout before you have dragged anywhere (the width was read from the
pointer's absolute position, so grabbing the left edge of the handle and
grabbing the right edge meant two different widths), and a plain click -
or the first press of a double-click, which is how you reset it - counts
as a resize.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402


class Cfg:
    def __init__(self, width=0, theater=False):
        self.panel_width_px = width
        self.theater = theater
        self.saves = 0

    def save(self):
        self.saves += 1


class Panel:
    def __init__(self, width):
        self._w = width

    def winfo_width(self):
        return self._w


class Root:
    def winfo_width(self):
        return 2000


class Event:
    def __init__(self, x_root):
        self.x_root = x_root


class FakeTab:
    PANEL_MIN = LibraryTab.PANEL_MIN
    PANEL_MAX = LibraryTab.PANEL_MAX
    GRIP_SLOP = LibraryTab.GRIP_SLOP
    _grip_press = LibraryTab._grip_press
    _grip_drag = LibraryTab._grip_drag
    _grip_release = LibraryTab._grip_release
    _grip_limits = LibraryTab._grip_limits

    def __init__(self, panel=600, **cfg):
        self.cfg = Cfg(**cfg)
        self.detail_panel = Panel(panel)
        self.root = Root()
        self._grip_from = None
        self._grip_moved = False
        self.fits = 0
        self.reserves = []

    def _fit_panel(self):
        self.fits += 1
        self.detail_panel._w = self.cfg.panel_width_px

    def _reserve_for_width(self, want):
        # The real one trades tag-list height for picture height; here we
        # only care that the drag asks for it.
        self.reserves.append(want)
        return 120


def test_taking_hold_of_the_handle_moves_nothing():
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    assert tab.cfg.panel_width_px == 0
    assert tab.fits == 0


def test_a_click_that_never_moves_is_not_a_resize():
    """The first press of a double-click is this. Double-click is how the
    width is put back to automatic, so it must not resize on the way."""
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(1401))
    tab._grip_release()
    assert tab.cfg.panel_width_px == 0
    assert tab.cfg.saves == 0


def test_the_width_follows_the_travel_not_the_pointer():
    """Wherever in the handle you took hold is where it stays."""
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(1300))          # 100px left
    assert tab.cfg.panel_width_px == 700


def test_dragging_right_gives_the_room_back():
    tab = FakeTab(panel=900)
    tab._grip_press(Event(1000))
    tab._grip_drag(Event(1150))
    assert tab.cfg.panel_width_px == 750


def test_the_drag_cannot_swallow_the_window_or_vanish():
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(0))             # yank it all the way left
    assert tab.cfg.panel_width_px == min(int(2000 * 0.60), LibraryTab.PANEL_MAX)
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(9000))          # and all the way right
    assert tab.cfg.panel_width_px == LibraryTab.PANEL_MIN


def test_a_real_drag_is_remembered():
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(1300))
    tab._grip_release()
    assert tab.cfg.saves == 1


def test_the_handle_does_nothing_in_theater():
    """Theater sets its own width; the handle is hidden, and a stray event
    must not reintroduce a width it would then ignore."""
    tab = FakeTab(panel=600, theater=True)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(1200))
    assert tab.cfg.panel_width_px == 0


def test_dragging_past_the_slop_stays_smooth_from_there():
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    for x in (1399, 1396, 1390, 1380, 1360):
        tab._grip_drag(Event(x))
    assert tab.cfg.panel_width_px == 640          # 1400 - 1360
    assert tab.fits == 4                          # the 1px move changed nothing


# ── theater must always have a door ─────────────────────────────────────

class EscapeTab:
    key_escape = LibraryTab.key_escape
    is_typing = staticmethod(lambda event: False)

    def __init__(self, theater=True, marked=(), selected=None):
        self.cfg = Cfg(theater=theater)
        self.marked = set(marked)
        self.selected = selected
        self.toggled = 0
        self.status = []
        self.player = type("P", (), {"playing": False, "pause": lambda self: None})()

    def toggle_theater(self):
        self.toggled += 1
        self.cfg.theater = not self.cfg.theater

    def set_status(self, text, colour=None):
        self.status.append(text)

    def clear_marks(self):
        self.marked.clear()

    def _restyle_cards(self):
        pass

    def _render_details(self):
        pass


class KeyEvent:
    state = 0


def test_escape_leaves_theater():
    """The button that turns it off lives in the panel theater has just
    filled the window with. If the layout ever puts that button out of
    reach, this is the way out."""
    tab = EscapeTab(theater=True)
    tab.key_escape(KeyEvent())
    assert tab.toggled == 1
    assert tab.cfg.theater is False


def test_escape_leaves_theater_before_anything_else():
    """Marks and a selection are cheap to redo; being stuck is not."""
    tab = EscapeTab(theater=True, marked={"a.mp4"}, selected=object())
    tab.key_escape(KeyEvent())
    assert tab.toggled == 1
    assert tab.marked == {"a.mp4"}          # untouched
    assert tab.selected is not None


def test_escape_still_clears_marks_when_theater_is_off():
    tab = EscapeTab(theater=False, marked={"a.mp4"})
    tab.key_escape(KeyEvent())
    assert tab.toggled == 0
    assert not tab.marked


def test_dragging_wider_pays_for_the_picture_out_of_the_tag_list():
    """A wider column is a taller picture, and that height has to come
    from somewhere. Without this the drag stopped dead the moment the
    height ran out, which reads as a broken handle."""
    tab = FakeTab(panel=600)
    tab._grip_press(Event(1400))
    tab._grip_drag(Event(1300))
    assert tab.reserves == [700]
    assert tab.cfg.tags_height_px == 120
