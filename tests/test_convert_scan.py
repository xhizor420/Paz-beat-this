"""The Convert tab's scan and queue table.

The scan used to run on the UI thread - a listing per category and three
filesystem calls per file - so it moved to a worker and to two listings
per category. It has to find exactly what the old one found. The old
loop is kept here verbatim and both are run over the same folders.

The table's filter used to re-find each returning row's place by walking
the whole list, so showing five thousand rows again took most of a
second. The rows must still come back in exactly their order.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import convert_tab                          # noqa: E402
from paz_suite.convert_tab import scan_sources             # noqa: E402
from paz_suite.convert_widgets import QueueTable           # noqa: E402


def old_scan(folders, extensions, overwrite, source_root, output_root):
    """The loop _scan ran on the UI thread, minus the widgets."""
    rows, skipped, missing = [], 0, []
    for folder in folders:
        source_dir = os.path.join(source_root, folder)
        target_dir = os.path.join(output_root, folder)
        if not os.path.isdir(source_dir):
            missing.append(source_dir)
            continue
        try:
            names = sorted(os.listdir(source_dir))
        except OSError:
            missing.append(source_dir)
            continue
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() not in extensions:
                continue
            source = os.path.join(source_dir, name)
            if not os.path.isfile(source):
                continue
            target = os.path.join(target_dir, stem + ".mp4")
            exists = os.path.exists(target) and os.path.getsize(target) > 0
            if exists and not overwrite:
                skipped += 1
                continue
            rows.append((folder, name, source, target))
    return rows, skipped, missing


def make_tree(root):
    src, out = root / "src", root / "out"
    (src / "wolves").mkdir(parents=True)
    (src / "foxes").mkdir(parents=True)
    (out / "wolves").mkdir(parents=True)
    for name in ("1001.webm", "1002.webm", "1003.MKV", "1004.webm", "notes.txt",
                 "b_clip.mov", "a_clip.mov", "1005.webm"):
        (src / "wolves" / name).write_bytes(b"x")
    (src / "wolves" / "folder.webm").mkdir()            # a directory, not a file
    (out / "wolves" / "1001.mp4").write_bytes(b"done")  # converted
    (out / "wolves" / "1002.mp4").write_bytes(b"")      # empty - not converted
    (out / "wolves" / "a_clip.mp4").write_bytes(b"done")
    (src / "foxes" / "2001.webm").write_bytes(b"x")     # no output folder at all
    return src, out


EXTS = {".webm", ".mkv", ".mov"}
FOLDERS = ["wolves", "foxes", "missing_category"]


def test_the_scan_finds_what_the_old_scan_found(tmp_path):
    src, out = make_tree(tmp_path)
    for overwrite in (False, True):
        new_rows, new_skipped, new_missing, error = scan_sources(
            FOLDERS, EXTS, overwrite, str(src), str(out))
        old_rows, old_skipped, old_missing = old_scan(
            FOLDERS, EXTS, overwrite, str(src), str(out))
        assert error == ""
        assert [r[:4] for r in new_rows] == old_rows
        assert new_skipped == old_skipped
        assert new_missing == old_missing


def test_what_it_finds(tmp_path):
    src, out = make_tree(tmp_path)
    rows, skipped, missing, _ = scan_sources(FOLDERS, EXTS, False, str(src), str(out))
    assert [r[1] for r in rows] == ["1002.webm", "1003.MKV", "1004.webm",
                                    "1005.webm", "b_clip.mov", "2001.webm"]
    assert skipped == 2
    assert missing == [str(src / "missing_category")]


def test_tags_already_fetched_ride_along(tmp_path):
    src, out = make_tree(tmp_path)

    class Meta:
        def get(self, pid):
            return {"artist": ["kenket"], "rating": "e", "tags": "wolf"} if pid == "1004" else None

    rows, *_ = scan_sources(["wolves"], EXTS, False, str(src), str(out), emeta=Meta())
    cells = {r[1]: r[4] for r in rows}
    assert cells["1004.webm"] == {"artist": "kenket", "rating": "E", "_tags": "wolf"}
    assert cells["1005.webm"] == {}


def test_a_scan_is_only_started_by_the_newest_request():
    """Two scans in flight: only the newer one lands in the table."""
    landed = []

    class Tab:
        _scan_done = convert_tab.ConvertTab._scan_done

        def __init__(self):
            self._scan_gen = 2
            self.scanning = True

    tab = Tab()
    tab.tasks = {}
    tab.table = type("T", (), {"add_many": lambda self, rows: landed.append(rows)})()
    tab._scan_done(1, [], ([], 0, [], ""))
    assert landed == [] and tab.scanning


# ── the queue table's filter ───────────────────────────────────────────

class Tree:
    """The ttk.Treeview calls the table makes, with the same ordering
    rules: move(iid, "", index) puts an item at that index of the attached
    items, detach takes it out."""

    def __init__(self):
        self.attached = []

    def insert(self, _parent, _where, iid=None, **_kw):
        self.attached.append(iid)

    def exists(self, iid):
        return True

    def move(self, iid, _parent, index):
        if iid in self.attached:
            self.attached.remove(iid)
        self.attached.insert(index, iid)

    def detach(self, iid):
        self.attached.remove(iid)

    def item(self, *_a, **_kw):
        pass

    def get_children(self):
        return tuple(self.attached)


class Label:
    def configure(self, **kw):
        self.text = kw.get("text")


def paper_table():
    table = QueueTable.__new__(QueueTable)
    table.tree = Tree()
    table.count = Label()
    table._rows, table._order, table._visible = {}, [], set()
    table._query, table._state_filter = "", "All"
    table._bars = {"queued": None}
    return table


def test_the_filter_brings_rows_back_in_their_order():
    import random
    rng = random.Random(5)
    table = paper_table()
    table.add_many([(f"r{i}", f"clip_{i}", {}) for i in range(300)])
    for i in range(300):
        table._rows[f"r{i}"]["state"] = rng.choice(["done", "failed", "queued"])
    for state in ("Done", "Failed", "All", "Queued", "All"):
        table._state_filter = state
        for query in ("clip_1", "", "clip_2", ""):
            table._query = query
            table.refresh_filter()
            expected = [iid for iid in table._order if table._matches(iid)]
            assert table.tree.attached == expected
            assert table._visible == set(expected)
    assert table.count.text == "300 files"


def test_the_count_matches_what_is_shown():
    table = paper_table()
    table.add_many([(f"r{i}", f"clip_{i}", {}) for i in range(10)])
    table._query = "clip_1"
    table.refresh_filter()
    assert table.count.text == "1 of 10"
