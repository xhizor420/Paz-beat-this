"""The filter chips must not be sliced off by the edge of the row.

They carry the library's counts, so their width depends on how big the
library is, how far the display is scaled, and how much of the row the
inspector has taken - and in theater the row is half what it was. Left
unmeasured the last chips ran under the panel, leaving "4K / 36" with
the rest of the number somewhere off-screen.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402
from paz_suite import theme                       # noqa: E402


class Chip:
    def __init__(self, width, text=""):
        self._w = width
        self._mapped = False
        self._text = text

    def winfo_reqwidth(self):
        return self._w

    def winfo_ismapped(self):
        return self._mapped

    def pack(self, **kw):
        self._mapped = True

    def pack_forget(self):
        self._mapped = False

    def cget(self, _key):
        return self._text


class Row:
    """A frame whose width is the sum of what is packed in it - which is
    exactly the thing the fit must not measure itself against."""

    def __init__(self, chips):
        self._chips = chips

    def winfo_width(self):
        return sum(c.winfo_reqwidth() for c in self._chips if c.winfo_ismapped())

    def winfo_reqwidth(self):
        return self.winfo_width()

    def winfo_ismapped(self):
        return True


class Fixed:
    def __init__(self, width, mapped=True):
        self._w, self._mapped = width, mapped

    def winfo_width(self):
        return self._w

    def winfo_reqwidth(self):
        return self._w

    def winfo_ismapped(self):
        return self._mapped


class FakeTab:
    _fit_quick_chips = LibraryTab._fit_quick_chips

    def __init__(self, row_width=1000, widths=(240, 230, 190, 170, 180)):
        chips = [Chip(w, f"chip{i}") for i, w in enumerate(widths)]
        for chip in chips:
            chip.pack()
        self.quick_chips = {f"k{i}": (chip, f"chip{i}", f"is:{i}")
                            for i, chip in enumerate(chips)}
        self._quick_live = list(self.quick_chips)
        self._quick_hidden = set()
        self._quick_more = Chip(40)
        self._quick_row = Row(chips)
        self._info_row = Fixed(row_width)
        self._info_pager = Fixed(120)
        self._info_stacked = True
        self._saved_row = Fixed(0, mapped=False)

    def shown(self):
        return [key for key, (chip, _l, _t) in self.quick_chips.items()
                if chip.winfo_ismapped()]


def test_everything_fits_when_there_is_room():
    tab = FakeTab(row_width=2000)
    tab._fit_quick_chips()
    assert len(tab.shown()) == 5
    assert not tab._quick_hidden
    assert not tab._quick_more.winfo_ismapped()


def test_what_does_not_fit_goes_behind_the_overflow():
    tab = FakeTab(row_width=700)
    tab._fit_quick_chips()
    assert tab.shown() == ["k0", "k1"]
    assert tab._quick_hidden == {"k2", "k3", "k4"}
    assert tab._quick_more.winfo_ismapped()


def test_the_row_keeps_its_order():
    """A row that drops its middle chip and keeps the next one reads as a
    glitch, not as a row that ran out of space."""
    tab = FakeTab(row_width=700, widths=(240, 400, 60, 60, 60))
    tab._fit_quick_chips()
    assert tab.shown() == ["k0"]


def test_fitting_twice_changes_nothing():
    """The regression: the fit used to measure the chips' own frame, whose
    width IS the chips - so hiding one shrank the number the next pass
    read, and the row emptied itself a chip at a time."""
    tab = FakeTab(row_width=700)
    tab._fit_quick_chips()
    first = tab.shown()
    for _ in range(4):
        tab._fit_quick_chips()
    assert tab.shown() == first


def test_a_chip_that_fits_again_comes_back():
    tab = FakeTab(row_width=700)
    tab._fit_quick_chips()
    tab._info_row = Fixed(2000)
    tab._fit_quick_chips()
    assert len(tab.shown()) == 5
    assert not tab._quick_more.winfo_ismapped()


def test_the_pager_only_takes_room_while_it_shares_the_row():
    tab = FakeTab(row_width=700)
    tab._info_stacked = False          # pager beside the chips
    tab._fit_quick_chips()
    beside = len(tab.shown())
    tab = FakeTab(row_width=700)
    tab._info_stacked = True           # chips on their own row
    tab._fit_quick_chips()
    assert len(tab.shown()) >= beside


# ── scaling ─────────────────────────────────────────────────────────────

def test_real_pixels_survive_the_round_trip_to_ctk_units():
    """CTk scales what it is given, so a size measured on screen has to be
    divided back down before it goes into a CTk widget - otherwise it is
    scaled twice. That is what made the inspector column half as wide
    again as the picture inside it."""
    before = theme.T.SCALE
    try:
        for scale in (1.0, 1.25, 1.5, 2.0):
            theme.T.SCALE = scale
            for real in (200, 431, 1257, 1797):
                assert abs(theme.unscaled(real) * scale - real) <= scale
            assert theme.unscaled(theme.px(430)) == 430
    finally:
        theme.T.SCALE = before


def test_unscaled_never_returns_a_useless_zero():
    before = theme.T.SCALE
    try:
        theme.T.SCALE = 2.5
        assert theme.unscaled(1) >= 1
        assert theme.unscaled(0) >= 1
    finally:
        theme.T.SCALE = before
