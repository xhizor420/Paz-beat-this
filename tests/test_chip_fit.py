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


# ── the query chips are pooled, and stay in order ──────────────────────
#
# One chip per word of the search. They were destroyed and rebuilt on
# every search, which is every quarter-second the user pauses while
# typing - 6.9ms of the 19ms a search cost, spent making twelve widgets
# identical to the twelve just thrown away. Pooled, the cost is 0.2ms.
# What pooling can get wrong is order and staleness, so that is what
# these pin.

class QueryChip:
    def __init__(self):
        self.opts = {}

    def configure(self, **kw):
        self.opts.update(kw)

    def cget(self, name):
        return self.opts.get(name)

    def pack(self, **_kw):
        pass

    def pack_forget(self):
        pass


class QueryRow:
    """Stands in for LibraryTab, with just what _render_chips touches."""

    _render_chips = LibraryTab._render_chips

    def __init__(self):
        self.chips = object()
        self._query_chips = []
        self._chips_shown = 0
        self.removed = []

    def _remove_token(self, token):
        self.removed.append(token)


def render(row, query):
    import paz_suite.library_tab as lt
    was_button, was_font = lt.ctk.CTkButton, lt.font
    lt.ctk.CTkButton = lambda *a, **kw: QueryChip()
    lt.font = lambda *a, **kw: None
    try:
        row._render_chips(query)
    finally:
        lt.ctk.CTkButton, lt.font = was_button, was_font
    return [c.cget("text").replace("  ✕", "")
            for c in row._query_chips[:row._chips_shown]]


def test_a_chip_shows_each_word_of_the_query():
    row = QueryRow()
    assert render(row, "wolf male solo") == ["wolf", "male", "solo"]


def test_a_shorter_query_hides_the_chips_it_no_longer_needs():
    row = QueryRow()
    render(row, "a b c d e")
    assert render(row, "a c") == ["a", "c"]
    assert row._chips_shown == 2
    assert len(row._query_chips) == 5, "a hidden chip was thrown away"


def test_a_longer_query_brings_the_hidden_ones_back():
    row = QueryRow()
    render(row, "a b c d")
    render(row, "a")
    assert render(row, "a b c d e") == ["a", "b", "c", "d", "e"]


def test_an_empty_query_shows_nothing():
    row = QueryRow()
    render(row, "wolf male")
    assert render(row, "") == []
    assert row._chips_shown == 0


def test_the_query_chips_stop_at_twelve():
    row = QueryRow()
    words = [f"w{i}" for i in range(20)]
    assert len(render(row, " ".join(words))) == 12


def test_a_reused_chip_drops_the_last_words_negative_styling():
    """A chip that showed '-cub' is red. Reused for 'cub' it must not be."""
    row = QueryRow()
    render(row, "-cub")
    negative = row._query_chips[0].cget("text_color")
    render(row, "cub")
    assert row._query_chips[0].cget("text_color") != negative


def test_the_cross_removes_the_word_the_chip_is_showing_now():
    row = QueryRow()
    render(row, "wolf male solo")
    render(row, "fox male solo")
    row._query_chips[0].cget("command")()
    assert row.removed == ["fox"], "the chip removed the word it used to show"
