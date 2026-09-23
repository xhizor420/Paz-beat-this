"""Marking a clip as used, and getting from clip to clip.

Picking footage for an edit is a session: look, judge, mark, next -
hundreds of times. So the three things that matter here are that marking
does not take you anywhere (it used to re-read ten thousand rows,
re-filter, re-sort and redraw, which landed you back on page one having
lost the clip you were looking at), that it does not cost a visible
pause, and that the whole loop is reachable from the keyboard.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import library_tab as lt      # noqa: E402
from paz_suite.library_tab import LibraryTab  # noqa: E402


class Rec:
    def __init__(self, name, used=(), colour=""):
        self.name = self.path = name
        self.used_projects = list(used)
        self.used_color = colour
        self.width, self.height = 1920, 1080
        self.fps, self.duration, self.size = 60.0, 10.0, 1000
        self.folder, self.score, self.pid = "wolves", 0, ""
        self.premium, self.premium_path = False, ""
        self.tags, self.named = set(), frozenset()


class Cfg:
    def __init__(self, page_size=4):
        self.last_project = ""
        self.page_size = page_size
        self.saves = 0

    def save(self):
        self.saves += 1

    save_soon = save


class FakeTab:
    _mark_used = LibraryTab._mark_used
    _unmark_used = LibraryTab._unmark_used
    _after_vault_change = LibraryTab._after_vault_change
    key_step_clip = LibraryTab.key_step_clip
    _select_index = LibraryTab._select_index
    key_mark_used = LibraryTab.key_mark_used
    is_typing = staticmethod(lambda event: False)

    def __init__(self, clips=6, page_size=4):
        self.cfg = Cfg(page_size)
        self.records = [Rec(f"{i}.mp4") for i in range(clips)]
        self.filtered = list(self.records)
        self._layout = [{"rec": rec, "x": 0, "y": 0, "tag": f"card{i}"}
                        for i, rec in enumerate(self.records[:page_size])]
        self._project_colors = {"PMV": "#ff4fa3", "Other": "#00ffcc"}
        self.selected = None
        self.page = 0
        self.marked = set()
        self.status = []
        self.repainted = []
        self.details = 0
        self.renders = 0
        self.metas = 0
        self.counts = 0
        self.scans = 0
        self.bumps = []
        self.scrolled = []
        self.picked = 0

    # -- the surroundings, stubbed -----------------------------------------
    def set_status(self, text, colour=None):
        self.status.append(text)

    def _restyle_cards(self):
        self.repainted.append("cards")

    def _restyle_these(self, *recs):
        # A mark repaints the clips that were marked, not the page -
        # see LibraryTab._after_vault_change.
        self.repainted.append("cards")

    def _draw_badges(self, index, rec, slot):
        self.repainted.append(rec.name)

    def _render_meta_line(self, rec):
        self.metas += 1

    def _render_details(self):
        self.details += 1

    def _refresh_quick_counts(self):
        self.counts += 1
        self.scans += 1

    def _bump_quick_count(self, key, delta):
        self.counts += 1
        self.bumps.append((key, delta))

    def render_page(self):
        self.renders += 1
        start = self.page * self.cfg.page_size
        page = self.filtered[start:start + self.cfg.page_size]
        self._layout = [{"rec": rec, "x": 0, "y": 0, "tag": f"card{i}"}
                        for i, rec in enumerate(page)]

    def _select(self, rec):
        self.selected = rec

    def _scroll_card_into_view(self, rec):
        self.scrolled.append(rec.name)

    def targets(self, fallback=None):
        return list(self.marked) or ([fallback] if fallback else [])

    def _mark_menu(self, targets):
        self.picked += 1


@pytest.fixture
def db(monkeypatch):
    """No database, but record what it was told."""
    wrote = []

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(lt, "db_connect", lambda: Conn())
    monkeypatch.setattr(lt, "vault_mark",
                        lambda conn, paths, project: wrote.append(("mark", tuple(paths), project)))
    monkeypatch.setattr(lt, "vault_unmark",
                        lambda conn, path, project: wrote.append(("unmark", path, project)))
    monkeypatch.setattr(lt, "vault_projects_list",
                        lambda conn: [("PMV", "#ff4fa3", 3, 0)])
    return wrote


class Key:
    state = 0


# ── marking keeps your place ────────────────────────────────────────────

def test_marking_does_not_reload_or_move_the_page(db):
    tab = FakeTab()
    tab.page = 1
    tab.selected = tab.records[2]
    tab._mark_used([tab.records[2]], "PMV")
    assert tab.page == 1                  # still where you were
    assert tab.renders == 0               # nothing re-rendered
    assert db == [("mark", ("2.mp4",), "PMV")]


def test_the_clip_knows_it_is_used_without_a_reload(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "PMV")
    assert rec.used_projects == ["PMV"]
    assert rec.used_color == "#ff4fa3"


def test_the_newest_project_is_the_one_the_card_shows(db):
    """_load_library orders marks most-recent-first and takes the colour
    from the first; marking in place has to match."""
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "Other")
    tab._mark_used([rec], "PMV")
    assert rec.used_projects == ["PMV", "Other"]
    assert rec.used_color == "#ff4fa3"


def test_marking_the_same_project_twice_does_not_duplicate_it(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "PMV")
    tab._mark_used([rec], "PMV")
    assert rec.used_projects == ["PMV"]


def test_only_the_cards_that_changed_are_repainted(db):
    tab = FakeTab()
    tab._mark_used([tab.records[1]], "PMV")
    assert "1.mp4" in tab.repainted
    assert "0.mp4" not in tab.repainted


def test_the_panel_updates_its_one_line_not_the_whole_tag_list(db):
    """The tag list did not change, and rebuilding it was most of what
    made marking slow."""
    tab = FakeTab()
    tab.selected = tab.records[0]
    tab._mark_used([tab.records[0]], "PMV")
    assert tab.metas == 1
    assert tab.details == 0


def test_the_counted_chips_are_refreshed(db):
    tab = FakeTab()
    tab._mark_used([tab.records[0]], "PMV")
    assert tab.counts == 1


def test_the_chips_are_adjusted_not_recounted(db):
    """Recounting means walking ten thousand records to learn a number the
    caller already knows, which was most of what a mark cost."""
    tab = FakeTab()
    tab._mark_used([tab.records[0]], "PMV")
    assert tab.scans == 0
    assert tab.bumps == [("unused", -1)]


def test_marking_a_clip_that_was_already_used_does_not_move_the_count(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "Other")
    tab.bumps.clear()
    tab._mark_used([rec], "PMV")
    assert tab.bumps == [("unused", 0)]


def test_marking_several_clips_counts_each_of_them(db):
    tab = FakeTab()
    tab._mark_used(tab.records[:3], "PMV")
    assert tab.bumps == [("unused", -3)]


def test_unmarking_the_last_project_puts_it_back_in_the_count(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "PMV")
    tab.bumps.clear()
    tab._unmark_used(rec, "PMV")
    assert tab.bumps == [("unused", 1)]


def test_unmarking_one_of_two_projects_does_not(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "Other")
    tab._mark_used([rec], "PMV")
    tab.bumps.clear()
    tab._unmark_used(rec, "PMV")
    assert tab.bumps == [("unused", 0)]


def test_the_project_is_remembered_once_not_per_clip(db):
    tab = FakeTab()
    tab._mark_used([tab.records[0]], "PMV")
    tab._mark_used([tab.records[1]], "PMV")
    tab._mark_used([tab.records[2]], "PMV")
    assert tab.cfg.last_project == "PMV"
    assert tab.cfg.saves == 1


def test_unmarking_recovers_the_previous_project_colour(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "Other")
    tab._mark_used([rec], "PMV")
    tab._unmark_used(rec, "PMV")
    assert rec.used_projects == ["Other"]
    assert rec.used_color == "#00ffcc"


def test_unmarking_the_last_project_clears_the_colour(db):
    tab = FakeTab()
    rec = tab.records[0]
    tab._mark_used([rec], "PMV")
    tab._unmark_used(rec, "PMV")
    assert rec.used_projects == []
    assert rec.used_color == ""


# ── V marks into the project you are working in ─────────────────────────

def test_v_marks_into_the_remembered_project(db):
    tab = FakeTab()
    tab.cfg.last_project = "PMV"
    tab.selected = tab.records[0]
    tab.key_mark_used(Key())
    assert tab.records[0].used_projects == ["PMV"]
    assert tab.picked == 0                 # no menu in the way


def test_v_asks_which_project_the_first_time(db):
    tab = FakeTab()
    tab.selected = tab.records[0]
    tab.key_mark_used(Key())
    assert tab.picked == 1
    assert tab.records[0].used_projects == []


def test_shift_v_always_asks(db):
    tab = FakeTab()
    tab.cfg.last_project = "PMV"
    tab.selected = tab.records[0]
    tab.key_mark_used(Key(), pick=True)
    assert tab.picked == 1


def test_v_marks_the_whole_marked_set_when_there_is_one(db):
    tab = FakeTab()
    tab.cfg.last_project = "PMV"
    tab.marked = {tab.records[0], tab.records[3]}
    tab.key_mark_used(Key())
    assert tab.records[0].used_projects == ["PMV"]
    assert tab.records[3].used_projects == ["PMV"]


def test_v_with_nothing_selected_says_so(db):
    tab = FakeTab()
    tab.key_mark_used(Key())
    assert tab.status and "Nothing selected" in tab.status[-1]


# ── Up and Down walk the results ────────────────────────────────────────

def test_down_goes_to_the_next_clip():
    tab = FakeTab()
    tab.selected = tab.records[1]
    tab.key_step_clip(Key(), 1)
    assert tab.selected is tab.records[2]


def test_up_goes_to_the_previous_clip():
    tab = FakeTab()
    tab.selected = tab.records[2]
    tab.key_step_clip(Key(), -1)
    assert tab.selected is tab.records[1]


def test_the_ends_of_the_list_hold():
    tab = FakeTab()
    tab.selected = tab.records[0]
    tab.key_step_clip(Key(), -1)
    assert tab.selected is tab.records[0]
    tab.selected = tab.records[-1]
    tab.key_step_clip(Key(), 1)
    assert tab.selected is tab.records[-1]


def test_nothing_selected_starts_at_the_first_clip():
    tab = FakeTab()
    tab.key_step_clip(Key(), 1)
    assert tab.selected is tab.records[0]


def test_walking_off_the_page_turns_it():
    tab = FakeTab(clips=6, page_size=4)
    tab.selected = tab.records[3]          # last on page 0
    tab.key_step_clip(Key(), 1)
    assert tab.page == 1
    assert tab.selected is tab.records[4]
    assert tab.renders == 1


def test_the_new_selection_is_scrolled_into_view():
    tab = FakeTab()
    tab.selected = tab.records[0]
    tab.key_step_clip(Key(), 1)
    assert tab.scrolled == ["1.mp4"]


def test_a_selection_that_is_no_longer_in_the_results_starts_over():
    tab = FakeTab()
    tab.selected = Rec("gone.mp4")
    tab.key_step_clip(Key(), 1)
    assert tab.selected is tab.records[0]
