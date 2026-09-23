"""The tag rail: how chips wrap, and how the pointer finds them.

Two things were wrong here once and each one alone was enough to ruin the
layout. The wrap compared a width measured in real screen pixels against
a room constant written in design pixels, so at 150% scaling a row that
could hold three chips was told it could hold one. And the measured width
was handed to CustomTkinter, which multiplies what it is given by the
widget scaling, so every chip rendered half again as wide as its own
text. Between them, a design of wrapped chips rendered as a single column
of full-width rows - the exact thing it exists to avoid.

The rail is one canvas now (see paz_suite/tag_rail.py), so the geometry
is a function of the chips, the width and a font - tested here without a
window - and clicks are found by hit-testing that geometry.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                 # noqa: E402
from paz_suite import tag_rail                              # noqa: E402
from paz_suite.tag_rail import Chip, Section, hit, lay_out  # noqa: E402
from paz_suite.library_tab import LibraryTab                # noqa: E402


def measure(text: str) -> int:
    """A font whose measurements are predictable: 10px per character,
    scaled the way a real one would be."""
    return int(len(text) * 10 * theme.T.SCALE)


def chips(names, swatch=None):
    return [Chip(n, 1, n, "#fff", swatch) for n in names]


def section(names, open_=True, swatch=None, title="TAGS"):
    return Section(title, title.lower(), open_, chips(names, swatch), len(names))


def chip_boxes(sections, width=400, hidden=0):
    boxes, _height = lay_out(sections, width, measure, hidden)
    return [b for b in boxes if b.kind == "chip"]


def lines(boxes):
    return len({b.y0 for b in boxes})


@pytest.fixture(autouse=True)
def scale_100():
    before = theme.T.SCALE
    theme.T.SCALE = 1.0
    yield
    theme.T.SCALE = before


# ── the room is measured, not assumed ───────────────────────────────────

def test_the_room_comes_from_the_rail_not_a_constant():
    """The constant said 236 while the rail is over 400 wide."""
    assert tag_rail.room_for(420) > tag_rail.FALLBACK_ROOM


def test_a_rail_not_on_screen_yet_falls_back_to_the_constant():
    assert tag_rail.room_for(1) == pytest.approx(
        theme.px(tag_rail.FALLBACK_ROOM) - theme.px(tag_rail.LEFT)
        - theme.px(tag_rail.RIGHT), abs=2)


def test_a_silly_narrow_rail_still_gets_a_usable_line():
    assert tag_rail.room_for(90) >= theme.px(tag_rail.MIN_ROOM)


def test_no_chip_runs_past_the_edge():
    for box in chip_boxes([section([f"tag{i}" for i in range(40)])], width=300):
        assert box.x1 <= 300 - theme.px(tag_rail.RIGHT)


# ── wrapping ────────────────────────────────────────────────────────────

def test_several_short_chips_share_a_line():
    assert lines(chip_boxes([section(["ab", "cd", "ef"])])) == 1


def test_a_line_wraps_when_the_next_chip_will_not_fit():
    assert lines(chip_boxes([section(["a" * 12, "b" * 12, "c" * 12])])) > 1


def test_every_chip_is_placed_exactly_once_and_in_order():
    names = [f"tag{i}" for i in range(20)]
    placed = [b.chip.name for b in chip_boxes([section(names)])]
    assert placed == names


def test_chips_never_overlap():
    boxes = chip_boxes([section([f"tag{i}" for i in range(30)])], width=300)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            apart = a.x1 <= b.x0 or b.x1 <= a.x0 or a.y1 <= b.y0 or b.y1 <= a.y0
            assert apart, f"{a.chip.name} overlaps {b.chip.name}"


def test_a_chip_too_wide_for_any_line_still_gets_one_of_its_own():
    boxes = chip_boxes([section(["short", "x" * 80, "after"])], width=300)
    assert [b.chip.name for b in boxes] == ["short", "x" * 80, "after"]
    assert lines(boxes) == 3


def test_a_chip_is_as_wide_as_its_text_needs():
    """Not scaled a second time: the width is what the font says plus
    the padding, in the same pixels."""
    box = chip_boxes([section(["wolf"])])[0]
    assert box.x1 - box.x0 == measure("wolf  1") + theme.px(tag_rail.CHIP_PAD)


def test_a_wider_rail_fits_more_per_line():
    names = [f"tag{i}" for i in range(18)]
    assert (lines(chip_boxes([section(names)], width=900))
            < lines(chip_boxes([section(names)], width=260)))


def test_nothing_to_show_draws_nothing():
    boxes, _height = lay_out([], 400, measure)
    assert boxes == []


def test_a_swatch_is_paid_for_in_the_wrap():
    """Project chips carry a colour dot, which takes room."""
    plain = chip_boxes([section(["aaaa"] * 6)])
    dotted = chip_boxes([section(["aaaa"] * 6, swatch="#f0f")])
    assert dotted[0].x1 - dotted[0].x0 > plain[0].x1 - plain[0].x0
    assert lines(dotted) >= lines(plain)


# ── sections ────────────────────────────────────────────────────────────

def test_a_closed_section_is_its_heading_alone():
    boxes, _ = lay_out([section(["a", "b"], open_=False)], 400, measure)
    assert [b.kind for b in boxes] == ["head"]


def test_sections_stack_without_overlapping():
    boxes, height = lay_out([section([f"a{i}" for i in range(12)], title="ARTISTS"),
                             section([f"t{i}" for i in range(12)], title="TAGS")],
                            300, measure, hidden=2)
    tops = [b.y0 for b in boxes]
    assert tops == sorted(tops), "something was drawn above what came before it"
    heads = [b for b in boxes if b.kind == "head"]
    first_chips = [b for b in boxes if b.kind == "chip" and b.section is heads[0].section]
    assert heads[1].y0 > max(b.y1 for b in first_chips)
    assert boxes[-1].kind == "hidden"
    assert height >= boxes[-1].y1


def test_the_heading_reads_open_or_closed():
    assert section(["a"]).heading.startswith("▾")
    assert section(["a"], open_=False).heading.startswith("▸")
    assert section(["a", "b"]).heading.endswith("2")


# ── scaling ─────────────────────────────────────────────────────────────

def test_the_wrap_holds_up_at_150_percent():
    """Both sides of the comparison have to scale together. When only the
    font did, one chip filled a line that could hold three."""
    names = [f"tag{i}" for i in range(18)]
    theme.T.SCALE = 1.0
    at_100 = lines(chip_boxes([section(names)], width=400))
    theme.T.SCALE = 1.5
    at_150 = lines(chip_boxes([section(names)], width=600))
    # A rail 1.5x wider holding 1.5x bigger chips wraps the same way.
    assert at_150 == at_100


# ── the pointer ─────────────────────────────────────────────────────────

def test_a_point_on_a_chip_finds_that_chip():
    boxes, _ = lay_out([section(["wolf", "fox", "deer"])], 400, measure)
    for index, box in enumerate(boxes):
        assert hit(boxes, (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2) == index


def test_the_gap_between_chips_finds_nothing():
    boxes = chip_boxes([section(["wolf", "fox"])])
    between = (boxes[0].x1 + boxes[1].x0) / 2
    all_boxes, _ = lay_out([section(["wolf", "fox"])], 400, measure)
    assert hit(all_boxes, between, boxes[0].y0 + 2) == -1


def test_the_edges_belong_to_exactly_one_box():
    boxes, _ = lay_out([section([f"t{i}" for i in range(30)])], 300, measure)
    for box in boxes:
        assert hit(boxes, box.x0, box.y0) == boxes.index(box)
        assert hit(boxes, box.x1, box.y0) != boxes.index(box)


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




# ── the canvas rail's clicks ───────────────────────────────────────────

class Event:
    def __init__(self, x, y):
        self.x, self.y = x, y
        self.x_root, self.y_root = x, y


class PaperCanvas:
    def canvasx(self, x):
        return x

    def canvasy(self, y):
        return y

    def itemconfigure(self, *_a, **_kw):
        pass

    def configure(self, **_kw):
        pass


def paper_rail(sections, hidden=0):
    """A TagRail with its geometry laid out and no window behind it."""
    calls = []
    rail = tag_rail.TagRail.__new__(tag_rail.TagRail)
    rail.canvas = PaperCanvas()
    rail.on_chip = lambda token: calls.append(("chip", token))
    rail.on_menu = lambda e, token, name: calls.append(("menu", token, name))
    rail.on_toggle = lambda key: calls.append(("toggle", key))
    rail.on_manage = lambda: calls.append(("manage",))
    rail.hidden = hidden
    rail.boxes, rail._height = lay_out(sections, 400, measure, hidden)
    rail._plates = [0] * len(rail.boxes)
    rail._hover = rail._pressed = -1
    return rail, calls


def centre(box):
    return Event((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)


def test_a_click_on_a_chip_searches_for_its_tag():
    rail, calls = paper_rail([Section("ARTISTS", "artists", True,
                                      [Chip("kenket", 3, "artist:kenket", "#fff")], 1)])
    chip = [b for b in rail.boxes if b.kind == "chip"][0]
    rail._on_press(centre(chip))
    rail._on_release(centre(chip))
    assert calls == [("chip", "artist:kenket")]


def test_pressing_a_chip_and_sliding_off_it_does_nothing():
    rail, calls = paper_rail([section(["wolf", "fox"])])
    wolf, fox = [b for b in rail.boxes if b.kind == "chip"]
    rail._on_press(centre(wolf))
    rail._on_release(centre(fox))
    assert calls == []


def test_a_click_on_a_heading_folds_its_group():
    rail, calls = paper_rail([section(["wolf"], title="SPECIES")])
    head = rail.boxes[0]
    rail._on_press(centre(head))
    rail._on_release(centre(head))
    assert calls == [("toggle", "species")]


def test_the_hidden_line_opens_the_manager():
    rail, calls = paper_rail([section(["wolf"])], hidden=3)
    line = rail.boxes[-1]
    assert line.kind == "hidden"
    rail._on_press(centre(line))
    rail._on_release(centre(line))
    assert calls == [("manage",)]


def test_right_click_offers_the_tag_menu_for_tags_only():
    """The menu is for tags - hiding and excluding a project makes no
    sense."""
    tags = Section("TAGS", "tags", True, [Chip("wolf", 2, "wolf", "#fff")], 1)
    projects = Section("PROJECTS", "projects", True,
                       [Chip("PMV", 4, 'used:"PMV"', "#fff", "#f0f", menu=False)], 1)
    rail, calls = paper_rail([tags, projects])
    wolf, pmv = [b for b in rail.boxes if b.kind == "chip"]
    rail._on_right(centre(pmv))
    assert calls == []
    rail._on_right(centre(wolf))
    assert calls == [("menu", "wolf", "wolf")]


def test_hovering_tracks_one_box_at_a_time():
    painted = []
    rail, _ = paper_rail([section(["wolf", "fox"])])
    rail._paint = lambda index, on: painted.append((index, on))
    wolf, fox = [i for i, b in enumerate(rail.boxes) if b.kind == "chip"]
    rail._on_motion(centre(rail.boxes[wolf]))
    rail._on_motion(centre(rail.boxes[fox]))
    rail._on_motion(Event(-50, -50))
    assert painted == [(-1, False), (wolf, True), (wolf, False), (fox, True),
                       (fox, False), (-1, True)]
    assert rail._hover == -1


# ── the inspector's tag list ──────────────────────────────────────────

from paz_suite.tag_rail import fit_text, lay_out_list    # noqa: E402


def rows(names, columns=1, open_=True, title="TAGS"):
    return Section(title, title.lower(), open_,
                   [Chip(n, None, n, "#fff") for n in names], len(names),
                   columns=columns)


def test_a_row_is_just_the_name():
    assert Chip("wolf", None, "wolf", "#fff").label == "wolf"
    assert Chip("wolf", 3, "wolf", "#fff").label == "wolf  3"


def test_rows_fill_the_width_one_to_a_line():
    boxes, _ = lay_out_list([rows(["a", "b", "c"])], 400)
    r = [b for b in boxes if b.kind == "row"]
    assert len(r) == 3
    assert len({(b.x0, b.x1) for b in r}) == 1, "rows of one column differ in width"
    assert r[0].x0 == theme.px(6) and r[0].x1 == 400 - theme.px(6)
    assert [b.y0 for b in r] == sorted({b.y0 for b in r})


def test_two_columns_split_the_width_and_keep_order():
    boxes, _ = lay_out_list([rows([f"t{i}" for i in range(9)], columns=2)], 400)
    r = [b for b in boxes if b.kind == "row"]
    assert [b.chip.name for b in r] == [f"t{i}" for i in range(9)]
    left, right = r[0], r[1]
    assert left.y0 == right.y0 and left.x1 < right.x0
    assert abs((left.x1 - left.x0) - (right.x1 - right.x0)) <= 1
    assert len({b.y0 for b in r}) == 5


def test_rows_never_overlap_and_headings_come_first():
    boxes, height = lay_out_list([rows(["kenket"], title="ARTISTS"),
                                  rows([f"t{i}" for i in range(20)], columns=2)], 500)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a.x1 <= b.x0 or b.x1 <= a.x0 or a.y1 <= b.y0 or b.y1 <= a.y0
    assert boxes[0].kind == "head" and boxes[2].kind == "head"
    assert height >= max(b.y1 for b in boxes)


def test_a_closed_group_is_its_heading_alone():
    boxes, _ = lay_out_list([rows(["a", "b"], open_=False)], 400)
    assert [b.kind for b in boxes] == ["head"]


def test_a_huge_group_is_capped():
    boxes, _ = lay_out_list([rows([f"t{i}" for i in range(500)], columns=2)], 400)
    assert sum(1 for b in boxes if b.kind == "row") == 160


def test_a_long_name_is_cut_to_fit_and_a_short_one_is_not():
    assert fit_text("wolf", 100, measure) == "wolf"
    cut = fit_text("a_very_long_tag_name_indeed", 100, measure)
    assert cut.endswith("…") and measure(cut) <= 100
    assert fit_text("anything", 0, measure) == ""


def test_a_row_click_searches_and_its_right_click_offers_the_menu():
    rail, calls = paper_rail([])
    rail.boxes, rail._height = lay_out_list([rows(["kenket"], title="ARTISTS")], 400)
    rail._plates = [0] * len(rail.boxes)
    row = rail.boxes[1]
    rail._on_press(centre(row))
    rail._on_release(centre(row))
    rail._on_right(centre(row))
    assert calls == [("chip", "kenket"), ("menu", "kenket", "kenket")]
