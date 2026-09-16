"""How wide the Library's inspector column gets, and why.

Three rules, in order: a width dragged by hand wins outright; otherwise
the column grows until the picture, its controls and a usable tag list
fill the height it has (a 16:9 picture in a fixed-width column is only
as tall as that width allows, which is what left a band of empty panel
under the player); and nothing may take more of the window than the
gallery can spare.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402


class Cfg:
    def __init__(self, theater=False, panel_width_px=0):
        self.theater = theater
        self.panel_width_px = panel_width_px


class Root:
    def __init__(self, width=1760):
        self._w = width

    def update_idletasks(self):
        pass

    def winfo_width(self):
        return self._w


class FakeTab:
    PANEL_MIN = LibraryTab.PANEL_MIN
    PANEL_MAX = LibraryTab.PANEL_MAX
    PANEL_SHARE_MAX = LibraryTab.PANEL_SHARE_MAX
    THEATER_SHARE_MAX = LibraryTab.THEATER_SHARE_MAX
    GALLERY_MIN_W = LibraryTab.GALLERY_MIN_W
    SIDEBAR_W = LibraryTab.SIDEBAR_W
    GRIP_W = LibraryTab.GRIP_W
    THEATER_BONUS = LibraryTab.THEATER_BONUS
    panel_cap = LibraryTab.panel_cap
    panel_width = LibraryTab.panel_width

    def __init__(self, window=1760, wants=0, **cfg):
        self.root = Root(window)
        self.cfg = Cfg(**cfg)
        self._wants = wants

    def _width_that_uses_the_height(self):
        return self._wants


def test_a_width_dragged_by_hand_wins():
    tab = FakeTab(panel_width_px=900)
    assert tab.panel_width() == 900


def test_a_dragged_width_still_cannot_swallow_the_gallery():
    tab = FakeTab(window=1000, panel_width_px=5000)
    assert tab.panel_width() == 600          # 60% of the window


def test_a_dragged_width_still_cannot_be_a_slit():
    tab = FakeTab(panel_width_px=40)
    assert tab.panel_width() == LibraryTab.PANEL_MIN


def test_the_column_grows_to_use_the_height_below_the_picture():
    """Without this the picture was a third of the height of its own box."""
    tab = FakeTab(window=1760, wants=700)
    assert tab.panel_width() == 700


def test_growing_for_height_stops_at_the_gallerys_share():
    tab = FakeTab(window=1760, wants=5000)
    assert tab.panel_width() == int(1760 * LibraryTab.PANEL_SHARE_MAX)


def test_a_column_that_needs_no_extra_height_keeps_the_worked_out_width():
    tab = FakeTab(window=1760, wants=0)
    assert tab.panel_width() == int(1760 * 0.28)


def test_theater_ignores_a_dragged_width():
    """Theater has one job - be the big one - and its own width for it."""
    tab = FakeTab(window=1760, panel_width_px=520, theater=True)
    assert tab.panel_width() == int(1760 * 0.60)


def test_theater_leaves_the_gallery_and_its_own_way_out_on_screen():
    """It swallowed the window whole, taking the Theater button with it."""
    tab = FakeTab(window=1760, wants=9999, theater=True)
    assert tab.panel_width() == int(1760 * LibraryTab.THEATER_SHARE_MAX)


def test_theater_grows_to_use_the_height_as_well():
    tab = FakeTab(window=1760, wants=1200, theater=True)
    assert tab.panel_width() == 1200


def test_theater_is_always_wider_than_the_normal_width():
    for window in (1024, 1280, 1760, 2560, 3840):
        normal = FakeTab(window=window).panel_width()
        theater = FakeTab(window=window, theater=True).panel_width()
        assert theater > normal, window


# ── the layout may never be wider than the window ───────────────────────

def test_the_gallery_always_keeps_a_strip_of_the_window():
    """Tk answers "this column wants more than there is" by running off
    the right-hand edge - and what went over the edge was the inspector's
    own Theater button, which is the way out of theater."""
    for window in (1180, 1400, 2000, 2560, 3840):
        for theater in (False, True):
            for dragged in (0, 5000):
                tab = FakeTab(window=window, wants=9999, theater=theater,
                              panel_width_px=dragged)
                panel = tab.panel_width()
                room_left = window - panel
                assert room_left >= LibraryTab.GALLERY_MIN_W - 1, (
                    window, theater, dragged, panel, room_left)
