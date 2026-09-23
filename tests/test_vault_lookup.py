"""The Vault: pasting a list of posts, picking a project, its strip.

The lookup answers "which of these do I have" - so a line that finds
nothing has to be reported, whatever kind of line it was. A name that
matched no clip used to vanish from the count entirely.
"""

from __future__ import annotations

import collections
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk                                        # noqa: E402

from paz_suite import vault_tab                             # noqa: E402
from paz_suite.library_db import Rec                        # noqa: E402
from paz_suite.vault_tab import VaultTab                    # noqa: E402


def rec(pid, name):
    return Rec(path=f"/lib/{name}.mp4", name=f"{name}.mp4", folder="", pid=pid,
               size=1, mtime=1, duration=10.0, width=1920, height=1080, fps=30.0)


class Box:
    def __init__(self, text):
        self.text = text

    def get(self, *_a):
        return self.text


class Tree:
    def __init__(self):
        self.rows = []

    def get_children(self):
        return []

    def delete(self, *_a):
        pass

    def insert(self, _parent, _where, iid=None, values=()):
        self.rows.append(values)


class Tab:
    _run_lookup = VaultTab._run_lookup
    F = VaultTab.F

    def __init__(self, text, records):
        self.paste_box = Box(text)
        self.tree = Tree()
        self.app = type("App", (), {"library": type("L", (), {"records": records})()})()
        self.status = None

    def set_status(self, text, colour=None):
        self.status = text


LIBRARY = [rec("4000001", "4000001"), rec("4000002", "wolf_pack_edit"),
           rec("", "fox_trot"), rec("4000003", "4000003")]


def test_post_ids_and_names_are_found():
    tab = Tab("4000001.webm\nwolf_pack\n4000003", LIBRARY)
    tab._run_lookup()
    assert [row[0] for row in tab.tree.rows] == ["4000001.mp4", "wolf_pack_edit.mp4",
                                                 "4000003.mp4"]
    assert tab._unmatched == []


def test_a_name_that_matches_nothing_is_reported_not_dropped():
    tab = Tab("4000001\nno_such_clip\n9999999", LIBRARY)
    tab._run_lookup()
    assert tab._unmatched == ["no_such_clip", "9999999"]
    assert "2 not found" in tab.status


def test_a_name_matching_several_clips_finds_each_once():
    tab = Tab("o\nfox", LIBRARY)
    tab._run_lookup()
    names = [row[0] for row in tab.tree.rows]
    assert len(names) == len(set(names))
    assert "fox_trot.mp4" in names


# ── picking a project repaints two rows, not all of them ──────────────

class Widget:
    def __init__(self):
        self.opts = {}

    def configure(self, **kw):
        self.opts.update(kw)


class Swatch(tk.Label):
    """A cover swatch - only its isinstance matters here."""

    def __init__(self):
        self.opts = {}

    def configure(self, **kw):
        self.opts.update(kw)


def test_selecting_a_project_restyles_only_the_two_rows_involved():
    refreshed = []

    class ProjectTab:
        _select_project = VaultTab._select_project
        _restyle_project_row = VaultTab._restyle_project_row

        def __init__(self):
            self._selected_project = "A"
            self._project_widgets = {n: (Widget(), Swatch(), Widget()) for n in "ABC"}

        def _refresh_projects(self):
            refreshed.append(1)

        def _load_project_clips(self):
            pass

    tab = ProjectTab()
    tab._select_project("B")
    assert refreshed == [], "the whole list was rebuilt to move the selection"
    a_line, a_swatch, a_label = tab._project_widgets["A"]
    b_line, b_swatch, b_label = tab._project_widgets["B"]
    c_line, _c_swatch, _c_label = tab._project_widgets["C"]
    assert a_line.opts["fg_color"] == "transparent"
    assert b_line.opts["fg_color"] == vault_tab.T.ELEVATED
    assert b_label.opts["text_color"] == vault_tab.T.TEXT
    assert a_swatch.opts["bg"] == vault_tab.T.BG
    assert c_line.opts == {}, "a row not involved was touched"

    tab._select_project("brand new")          # not in the list yet
    assert refreshed == [1]


# ── the strip is composed on the worker ────────────────────────────────

def test_strip_thumbnails_are_composed_off_the_ui_thread_and_kept(monkeypatch, tmp_path):
    from PIL import Image
    monkeypatch.setattr(vault_tab, "THUMB_DIR", str(tmp_path))
    clips = [rec("1", "a"), rec("2", "b")]
    for clip in clips:
        Image.new("RGB", (320, 180), (200, 10, 10)).save(
            tmp_path / vault_tab.thumb_key(clip.path), "JPEG")
    posted = []
    monkeypatch.setattr(vault_tab.uithread, "post",
                        lambda fn, *args: posted.append((threading.current_thread(), args)))
    composed = []
    real_fit = vault_tab.fit_frame
    monkeypatch.setattr(vault_tab, "fit_frame",
                        lambda *a, **kw: composed.append(1) or real_fit(*a, **kw))

    class StripTab:
        _load_strip_thumbs = VaultTab._load_strip_thumbs
        STRIP_CACHE = 10
        STRIP_W, STRIP_H = 96, 54

        def __init__(self):
            self._strip_token = 1
            self._strip_cache = collections.OrderedDict()
            self._strip_lock = threading.Lock()

        def _place_strip_thumb(self, *_a):
            pass

    tab = StripTab()
    tab._load_strip_thumbs(clips, 1)
    assert len(composed) == 2
    for _thread, (index, image, token) in posted:
        assert image.size == (96, 54), "the UI thread was handed an unscaled picture"
    posted.clear()
    tab._load_strip_thumbs(clips, 1)           # the same project again
    assert len(composed) == 2, "a thumbnail was composed twice"
    assert len(posted) == 2
