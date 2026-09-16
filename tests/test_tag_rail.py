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
    _chip_room = LibraryTab._chip_room
    _plan_chips = LibraryTab._plan_chips
    _plan_header = LibraryTab._plan_header

    def __init__(self, rail_width=400):
        self.tagpanel = Panel(rail_width)
        self._chip_font = Font()
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
