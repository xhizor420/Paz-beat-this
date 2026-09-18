"""The tag rail: how chips wrap, and how they are reused.

Two things were wrong here and each one alone was enough to ruin the
layout. The wrap compared a width measured in real screen pixels against
a room constant written in design pixels, so at 150% scaling a row that
could hold three chips was told it could hold one. And the measured width
was handed straight to CustomTkinter, which multiplies what it is given
by the widget scaling, so every chip rendered half again as wide as its
own text. Between them, a design of wrapped chips rendered as a single
column of full-width rows - the exact thing it exists to avoid.

Rebuilding it was also the largest stall left in the app: a hundred and
seventy CTkButtons at three and a half milliseconds each. The chips are
pooled now, which is why the parking rules below matter - a reused chip
that is not hidden still reads as a tag of the current search.
"""

from __future__ import annotations

import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                 # noqa: E402
from paz_suite.library_tab import LibraryTab                # noqa: E402


class Font:
    """A font whose measurements are predictable: 10px per character,
    scaled the way a real one would be."""

    def measure(self, text: str) -> int:
        return int(len(text) * 10 * theme.T.SCALE)


class Panel:
    def __init__(self, width):
        self._width = width

    def winfo_width(self):
        return self._width


class FakeTab:
    CHIP_PAD = LibraryTab.CHIP_PAD
    CHIP_ROOM = LibraryTab.CHIP_ROOM
    WIDTH_CACHE = LibraryTab.WIDTH_CACHE
    _chip_room = LibraryTab._chip_room
    _plan_chips = LibraryTab._plan_chips
    _plan_header = LibraryTab._plan_header
    _text_w = LibraryTab._text_w

    def __init__(self, rail_width=400):
        self.tagpanel = Panel(rail_width)
        self._chip_font = Font()
        # Widths go through the remembered-measurement path, the way the
        # real rail does - a wrong cache key would look like a wrong wrap.
        self._widths = {}
        self._measure_fonts = {"chip": self._chip_font}
        self._rows_planned = 0
        self._heads_planned = 0
        self._slots_planned = {}


def chips(names):
    return [(n, 1, n, "#fff", None) for n in names]


def plan_for(tab, names):
    plan: list = []
    tab._plan_chips(chips(names), 0, True, plan)
    return plan


def rows_used(plan):
    return sum(1 for item in plan if item[0] == "row")


@pytest.fixture(autouse=True)
def scale_100():
    before = theme.T.SCALE
    theme.T.SCALE = 1.0
    yield
    theme.T.SCALE = before


# ── the room is measured, not assumed ───────────────────────────────────

def test_the_room_comes_from_the_rail_not_a_constant():
    """The constant said 236 while the rail is over 400 wide."""
    tab = FakeTab(rail_width=420)
    assert tab._chip_room() > tab.CHIP_ROOM


def test_a_rail_not_on_screen_yet_falls_back_to_the_constant():
    tab = FakeTab(rail_width=0)
    assert tab._chip_room() == pytest.approx(theme.px(tab.CHIP_ROOM) - theme.px(26),
                                             abs=2)


def test_the_room_leaves_space_for_the_scrollbar():
    tab = FakeTab(rail_width=400)
    assert tab._chip_room() < 400


def test_a_silly_narrow_rail_still_gets_a_usable_row():
    tab = FakeTab(rail_width=40)
    assert tab._chip_room() >= theme.px(110)


# ── wrapping ────────────────────────────────────────────────────────────

def test_several_short_chips_share_a_row():
    tab = FakeTab(rail_width=400)
    plan = plan_for(tab, ["ab", "cd", "ef"])
    assert rows_used(plan) == 1


def test_a_row_wraps_when_the_next_chip_will_not_fit():
    tab = FakeTab(rail_width=400)
    plan = plan_for(tab, ["a" * 12, "b" * 12, "c" * 12])
    assert rows_used(plan) > 1


def test_every_chip_is_placed_exactly_once():
    tab = FakeTab(rail_width=400)
    names = [f"tag{i}" for i in range(20)]
    plan = plan_for(tab, names)
    placed = [item[7] for item in plan if item[0] == "chip"]
    assert placed == names


def test_slots_restart_at_zero_on_each_new_row():
    tab = FakeTab(rail_width=300)
    plan = plan_for(tab, [f"tag{i}" for i in range(12)])
    seen: dict = {}
    for item in plan:
        if item[0] != "chip":
            continue
        line, slot = item[1], item[2]
        assert slot == seen.get(line, 0), "a slot was skipped or repeated"
        seen[line] = slot + 1
    assert len(seen) > 1, "this case is meant to wrap"


def test_the_planned_slot_counts_match_what_was_planned():
    """_park_rail hides chips past these counts, so a wrong one either
    leaves a stale tag showing or blanks a real one."""
    tab = FakeTab(rail_width=300)
    plan = plan_for(tab, [f"tag{i}" for i in range(15)])
    actual: dict = {}
    for item in plan:
        if item[0] == "chip":
            actual[item[1]] = actual.get(item[1], 0) + 1
    assert tab._slots_planned == actual


def test_the_rows_planned_count_matches_the_rows_in_the_plan():
    tab = FakeTab(rail_width=300)
    plan = plan_for(tab, [f"tag{i}" for i in range(15)])
    assert tab._rows_planned == rows_used(plan)


def test_a_wider_rail_fits_more_per_row():
    narrow = FakeTab(rail_width=260)
    wide = FakeTab(rail_width=900)
    names = [f"tag{i}" for i in range(18)]
    assert rows_used(plan_for(wide, names)) < rows_used(plan_for(narrow, names))


def test_nothing_to_show_plans_nothing():
    tab = FakeTab()
    plan = plan_for(tab, [])
    assert plan == []
    assert tab._rows_planned == 0


def test_a_swatch_is_paid_for_in_the_wrap():
    """Project chips carry a colour swatch, which takes room."""
    tab = FakeTab(rail_width=400)
    plain: list = []
    tab._plan_chips(chips(["aaaa"] * 6), 0, True, plain)
    tab2 = FakeTab(rail_width=400)
    swatched: list = []
    tab2._plan_chips([("aaaa", 1, "aaaa", "#fff", "#f0f")] * 6, 0, False, swatched)
    assert rows_used(swatched) >= rows_used(plain)


# ── scaling ─────────────────────────────────────────────────────────────

def test_the_wrap_holds_up_at_150_percent():
    """Both sides of the comparison have to scale together. When only the
    font did, one chip filled a row that could hold three."""
    names = [f"tag{i}" for i in range(18)]
    theme.T.SCALE = 1.0
    at_100 = rows_used(plan_for(FakeTab(rail_width=400), names))
    theme.T.SCALE = 1.5
    at_150 = rows_used(plan_for(FakeTab(rail_width=600), names))
    # A rail 1.5x wider holding 1.5x bigger chips wraps the same way.
    assert at_150 == at_100


def test_the_grid_rows_advance_past_the_chip_rows():
    """The rail is one grid: headers and chip rows share its row numbers,
    so a group has to leave the next one somewhere to go."""
    tab = FakeTab(rail_width=300)
    plan: list = []
    row = tab._plan_header("TAGS", "tags", True, 3, 0, plan)
    assert row == 1
    row = tab._plan_chips(chips([f"tag{i}" for i in range(9)]), row, True, plan)
    assert row > 1
    grid_rows = [item[2] for item in plan if item[0] == "row"]
    assert grid_rows == sorted(set(grid_rows)), "two rows claimed one grid row"


# ── the counting happens off the UI thread ─────────────────────────────

def test_counting_needs_no_window():
    """Five sixths of what rebuilding the rail costs was counting every
    tag in the result set, which touches no widget. So it has to be a
    function of the records alone."""
    class Rec:
        def __init__(self, artists=(), tags=(), named=(), projects=()):
            self.artists = list(artists)
            self.characters = []
            self.species = []
            self.copyrights = []
            self.lore = []
            self.tags = set(tags)
            self.named = frozenset(named)
            self.used_projects = list(projects)

    counted = LibraryTab._count_tags([
        Rec(artists=["kenket"], tags={"wolf", "male", "kenket"},
            named={"kenket"}, projects=["PMV"]),
        Rec(artists=["kenket"], tags={"wolf", "fox"}),
    ])
    assert counted["artists"]["kenket"] == 2
    assert counted["other"]["wolf"] == 2
    assert counted["other"]["fox"] == 1
    assert "kenket" not in counted["other"], "a named tag was counted twice"
    assert counted["projects"]["PMV"] == 1


def test_counting_an_empty_result_is_empty():
    counted = LibraryTab._count_tags([])
    assert all(not c for c in counted.values())
    assert set(counted) == {"artists", "characters", "species", "series",
                            "lore", "other", "projects"}


# ── the chips are bound once, not per render ───────────────────────────

@contextlib.contextmanager
def paper_chips(make):
    """Stand in for the two things a chip needs a live window for: the
    CTkButton itself, and the CTkFont handed to it."""
    import paz_suite.library_tab as lt
    was_button, was_font = lt.ctk.CTkButton, lt.font
    lt.ctk.CTkButton = lambda *a, **kw: make()
    lt.font = lambda *a, **kw: None
    try:
        yield
    finally:
        lt.ctk.CTkButton, lt.font = was_button, was_font


def test_a_chip_is_bound_once_and_carries_its_tag_on_itself():
    """CTkButton.bind registers a Tcl command on each of the button's two
    inner widgets, and neither unbind nor rebinding gives them back - so
    rebinding a pooled chip on every search abandoned two commands per
    chip, for as long as the app stayed open. The tag lives on the widget
    instead, and the handler reads it when it fires."""
    class Chip:
        def __init__(self):
            self.binds = 0
            self.configures = 0
            self.mapped = False
            self.opts = {}

        def bind(self, *_a, **_kw):
            self.binds += 1

        def configure(self, **kw):
            self.configures += 1
            self.opts.update(kw)

        def cget(self, name):
            return self.opts.get(name)

        def winfo_ismapped(self):
            return self.mapped

        def pack(self, **_kw):
            self.mapped = True

    made = []

    class Tab:
        _rail_chip = LibraryTab._rail_chip

        def __init__(self):
            self._rail_slots = [[]]
            self._rail_rows = [object()]

        def add_token(self, token):
            made.append(token)

    tab = Tab()
    with paper_chips(Chip):
        tab._rail_chip(0, 0, "wolf  12", 80, "#fff", None, "wolf", "wolf", True)
        chip = tab._rail_slots[0][0]
        first_binds = chip.binds
        assert first_binds == 1
        assert chip.paz_token == "wolf"
        # The click goes through the command, which reads the tag off the
        # widget - so it has to be the tag the chip is showing now.
        chip.opts["command"]()
        assert made == ["wolf"]

        # A later search puts a different tag in the same slot.
        tab._rail_chip(0, 0, "fox  3", 70, "#fff", None, "fox", "fox", True)
        assert chip.binds == first_binds, "rebound a chip it had already bound"
        assert chip.paz_token == "fox", "the chip still carries the old tag"
        made.clear()
        chip.opts["command"]()
        assert made == ["fox"], "the chip acted on the tag it used to show"


def test_a_project_chip_offers_no_tag_menu():
    """The right-click menu is for tags, not for projects - and with one
    binding for the life of the chip, that has to be a check inside the
    handler rather than a binding that is or is not there."""
    offered = []

    class Chip:
        def __init__(self):
            self.opts = {}
            self.handler = None

        def bind(self, _sequence, func, *_a, **_kw):
            self.handler = func

        def configure(self, **kw):
            self.opts.update(kw)

        def cget(self, name):
            return self.opts.get(name)

        def winfo_ismapped(self):
            return True

        def pack(self, **_kw):
            pass

    class Tab:
        _rail_chip = LibraryTab._rail_chip

        def __init__(self):
            self._rail_slots = [[]]
            self._rail_rows = [object()]

        def add_token(self, token):
            pass

        def _tag_menu(self, _event, token, name):
            offered.append((token, name))

    tab = Tab()
    with paper_chips(Chip):
        tab._rail_chip(0, 0, "PMV  4", 80, "#fff", "#f0f", 'used:"PMV"',
                       "PMV", False)
        chip = tab._rail_slots[0][0]
        chip.handler(object())
        assert offered == [], "a project chip offered a tag menu"

        # The same chip, reused for a tag, does offer one.
        tab._rail_chip(0, 0, "wolf  4", 80, "#fff", None, "wolf", "wolf", True)
        chip.handler(object())
        assert offered == [("wolf", "wolf")]


def test_the_inspectors_tag_buttons_are_bound_once_too():
    """The same pool, the same leak: forty tag buttons rebound on every
    click of a clip. Forty clips is sixteen hundred Tcl commands Tk will
    hold until the app closes."""
    clicked, offered = [], []

    class Button:
        def __init__(self):
            self.binds = 0
            self.opts = {}
            self.handler = None

        def bind(self, _sequence, func, *_a, **_kw):
            self.binds += 1
            self.handler = func

        def configure(self, **kw):
            self.opts.update(kw)

        def cget(self, name):
            return self.opts.get(name)

        def grid(self, **_kw):
            pass

    class Tab:
        _tag_button = LibraryTab._tag_button

        def __init__(self):
            self._tag_pool = []
            self._tags_used = 0
            self.detail_tags = object()

        def add_token(self, token):
            clicked.append(token)

        def _tag_menu(self, _event, token, label):
            offered.append((token, label))

    tab = Tab()
    with paper_chips(Button):
        tab._tag_button("artist:kenket", "kenket", "#fff", 0)
        button = tab._tag_pool[0]
        assert button.binds == 1

        # A second clip reuses the pool from the top.
        tab._tags_used = 0
        tab._tag_button("artist:wolfy", "wolfy", "#fff", 0)
        assert button.binds == 1, "rebound a button it had already bound"
        button.opts["command"]()
        button.handler(object())
        assert clicked == ["artist:wolfy"]
        assert offered == [("artist:wolfy", "wolfy")]
