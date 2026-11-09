"""The Vault keeps up with marks made in the Library.

Marking happens in the Library, hundreds of clips at a time, and the Vault
only read the database when it was built or when something was done in the
Vault itself - so a project started from the Library never appeared there,
and the counts were whatever they had been when the tab was first opened.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import library_db as db           # noqa: E402
from paz_suite.library_tab import LibraryTab     # noqa: E402
from paz_suite.vault_tab import VaultTab         # noqa: E402


class Rec:
    def __init__(self, name, used=()):
        self.name = self.path = name
        self.used_projects = list(used)
        self.used_color = ""
        self.tags = set()


class Cfg:
    def __init__(self, last=""):
        self.last_project = last
        self.saves = 0

    def save_soon(self):
        self.saves += 1


class FakeVault:
    def __init__(self):
        self.stale = 0

    def mark_stale(self):
        self.stale += 1


class FakeApp:
    def __init__(self, vault):
        self.vault_tab = vault
        self.asked = []

    def _tab_object(self, name, build=True):
        self.asked.append((name, build))
        return self.vault_tab if name == "Vault" else None


class MarkingTab:
    _mark_used = LibraryTab._mark_used
    _after_vault_change = LibraryTab._after_vault_change

    def __init__(self, app, colours=None):
        self.app = app
        self.cfg = Cfg()
        self.cfg.save = self.cfg.save_soon
        self.records = [Rec(f"{i}.mp4") for i in range(3)]
        self._layout = []
        self.selected = None
        self._project_colors = dict(colours or {})

    def _restyle_these(self, *recs):
        pass

    def _bump_quick_count(self, key, delta):
        pass

    def set_status(self, text, colour=None):
        pass


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "lib.sqlite3"))
    db.db_forget_schema()
    yield
    db.db_forget_schema()


# ── the Library tells the Vault ────────────────────────────────────────

def test_a_mark_in_the_library_tells_the_vault_without_building_it(real_db):
    vault = FakeVault()
    app = FakeApp(vault)
    tab = MarkingTab(app)
    tab._mark_used([tab.records[0]], "Summer PMV")
    assert vault.stale == 1
    assert ("Vault", False) in app.asked
    assert ("Vault", True) not in app.asked


def test_no_vault_yet_is_fine(real_db):
    tab = MarkingTab(FakeApp(None))
    tab._mark_used([tab.records[0]], "Summer PMV")      # nothing to tell


def test_a_library_reload_tells_the_vault():
    import threading
    vault = FakeVault()

    class Loading:
        _adopt_library = LibraryTab._adopt_library

        def __init__(self):
            self.app = FakeApp(vault)
            self._tiles_lock = threading.Lock()
            self._tiles, self._photos = {}, {}
            self._prefetch_token = 0

    Loading()._adopt_library({"records": [], "by_path": {}, "tag_universe": set(),
                              "colors": {}})
    assert vault.stale == 1


# ── a mark is never left on a project that does not exist ──────────────

def test_marking_into_a_missing_project_creates_it(real_db):
    tab = MarkingTab(FakeApp(None))
    tab._mark_used([tab.records[0], tab.records[1]], "Renamed away")
    conn = db.db_connect()
    try:
        names = [name for name, _c, count, _t in db.vault_projects_list(conn)]
        marks = db.vault_marks_by_path(conn)
    finally:
        conn.close()
    assert names == ["Renamed away"]
    # Visible on the next load, which joins marks to their project.
    assert set(marks) == {"0.mp4", "1.mp4"}
    assert tab._project_colors["Renamed away"]


def test_an_existing_project_keeps_its_colour(real_db):
    conn = db.db_connect()
    db.vault_ensure_project(conn, "Kept", "#123456")
    conn.close()
    tab = MarkingTab(FakeApp(None))
    tab._mark_used([tab.records[0]], "Kept")
    assert tab._project_colors == {"Kept": "#123456"}
    assert tab.records[0].used_color == "#123456"


# ── the Vault catches up when it is looked at ──────────────────────────

class Vault:
    mark_stale = VaultTab.mark_stale
    _refresh_if_stale = VaultTab._refresh_if_stale
    on_shown = VaultTab.on_shown
    refresh = VaultTab.refresh
    _clips_of = VaultTab._clips_of
    _load_project_clips = VaultTab._load_project_clips
    _apply_tag_filter = VaultTab._apply_tag_filter

    def __init__(self, records, selected=None):
        library = type("Lib", (), {})()
        library.records = records
        self.app = type("App", (), {})()
        self.app.library = library
        self._visible = False
        self._fit_wanted = False
        self._stale = False
        self._results = {}
        self._selected_project = selected
        self._tag_filter = ()
        self._overview_on = False
        self._project_all, self._project_clips = [], []
        self._focused_index = None
        self.idle = []
        self.rebuilt_lists = 0
        self.details = 0
        self.infos = 0

    def after_idle(self, fn):
        self.idle.append(fn)

    def _refresh_projects(self):
        self._stale = False
        self.rebuilt_lists += 1

    def _show_project_detail(self):
        self.details += 1

    def _update_focused_info(self):
        self.infos += 1


def test_a_hidden_vault_waits_until_it_is_shown():
    vault = Vault([])
    vault.mark_stale()
    vault.mark_stale()
    assert vault.rebuilt_lists == 0 and vault.idle == []
    vault.on_shown()
    assert vault.rebuilt_lists == 1
    vault.on_shown()                           # nothing changed since
    assert vault.rebuilt_lists == 1


def test_a_visible_vault_catches_up_once_the_moment_settles():
    vault = Vault([])
    vault._visible = True
    vault.mark_stale()
    assert vault.rebuilt_lists == 0
    for fn in vault.idle:
        fn()
    assert vault.rebuilt_lists == 1


def test_a_new_clip_in_the_project_keeps_the_focused_one():
    a, b, c = Rec("a.mp4", ["P"]), Rec("b.mp4", ["P"]), Rec("c.mp4")
    vault = Vault([a, b, c], selected="P")
    vault._load_project_clips()
    vault._focused_index = 1                   # b
    c.used_projects.append("P")                # marked from the Library
    vault.mark_stale()
    vault.on_shown()
    assert [r.name for r in vault._project_clips] == ["a.mp4", "b.mp4", "c.mp4"]
    assert vault._project_clips[vault._focused_index] is b


def test_nothing_new_in_the_project_leaves_its_rows_alone():
    a = Rec("a.mp4", ["P"])
    vault = Vault([a, Rec("b.mp4")], selected="P")
    vault._load_project_clips()
    shown = vault.details
    vault.mark_stale()
    vault.on_shown()
    assert vault.details == shown
    assert vault.infos == 1


def test_the_cover_save_has_a_refresh_to_call():
    # Saving a cover called self.refresh(), which did not exist: the cover
    # was saved and then an exception stopped the list being redrawn.
    assert callable(getattr(VaultTab, "refresh", None))


# ── V follows a project that is renamed or cleared ──────────────────────

def _project_tab(monkeypatch, last):
    from paz_suite import vault_tab as vt

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(vt, "db_connect", lambda: Conn())
    monkeypatch.setattr(vt, "vault_rename_project", lambda *a: None)
    monkeypatch.setattr(vt, "vault_clear_project", lambda *a: None)
    monkeypatch.setattr(vt, "ask_yes", lambda *a, **k: True)

    class Tab:
        _rename_project = VaultTab._rename_project
        _clear_project = VaultTab._clear_project

        def __init__(self):
            self.cfg = Cfg(last)
            self._selected_project = None

        def _reload_library(self):
            pass

        def _refresh_projects(self):
            pass

        def set_status(self, *a):
            pass

    return Tab(), vt


def test_renaming_the_v_project_moves_v_with_it(monkeypatch):
    tab, vt = _project_tab(monkeypatch, "Old")

    class Dialog:
        def __init__(self, **kw):
            pass

        def get_input(self):
            return "New"

    monkeypatch.setattr(vt.ctk, "CTkInputDialog", Dialog)
    tab._rename_project("Old")
    assert tab.cfg.last_project == "New"


def test_renaming_another_project_leaves_v_alone(monkeypatch):
    tab, vt = _project_tab(monkeypatch, "Mine")

    class Dialog:
        def __init__(self, **kw):
            pass

        def get_input(self):
            return "New"

    monkeypatch.setattr(vt.ctk, "CTkInputDialog", Dialog)
    tab._rename_project("Other")
    assert tab.cfg.last_project == "Mine" and tab.cfg.saves == 0


def test_clearing_the_v_project_makes_v_ask_again(monkeypatch):
    tab, _vt = _project_tab(monkeypatch, "Gone")
    tab._clear_project("Gone")
    assert tab.cfg.last_project == ""
