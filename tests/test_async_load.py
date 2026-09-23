"""Re-reading the library without freezing the window.

A load is ten thousand rows, the tag cache folded into each one, and a
walk of the premium pool - a quarter of a second on the real library.
That used to happen on the UI thread after every sync and every media
rebuild, which is a quarter-second freeze you can feel. It is now read on
a worker and installed in one step, so these tests are about the two
things that split can get wrong: a result installed over a newer one, and
a caller running before the records it needs are there.
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402


class Rec:
    def __init__(self, path):
        self.path = self.name = path
        self.tags = set()
        self.pid = ""


class FakeTab:
    """The three methods under test, with everything around them stubbed
    so no window, thread or database is needed."""

    _load_library = LibraryTab._load_library
    _adopt_library = LibraryTab._adopt_library
    load_library_async = LibraryTab.load_library_async

    def __init__(self, reads=None):
        self.reads = list(reads or [])
        self.posted = []
        self.spawned = []
        self.records = []
        self.by_path = {}
        self.tag_universe = set()
        self._project_colors = {}
        self._weights = "stale"
        self.app = None
        self.read_calls = 0
        # Prepared gallery tiles: a sync can have replaced the thumbnail
        # behind a path, so installing a library drops them.
        self._tiles = {"stale-tile": object()}
        self._photos = {"stale-photo": object()}
        self._tiles_lock = threading.Lock()
        self._prefetch_token = 0

    # -- the surroundings --------------------------------------------------
    def _read_library(self):
        self.read_calls += 1
        if self.reads:
            return self.reads.pop(0)
        return bundle([])

    def ui(self, fn, *args):
        """uithread.post, recorded rather than run - the test decides
        when the reply lands, which is the whole point."""
        self.posted.append((fn, args))

    def _refresh_missing_badge(self):
        pass

    def after(self, _ms, fn):
        """Timers are recorded, not run - see the settle test below."""
        self.timers = getattr(self, "timers", [])
        self.timers.append(fn)
        return len(self.timers)

    def after_cancel(self, _job):
        pass

    def _warm_weights(self):
        pass

    # -- helpers -----------------------------------------------------------
    def run_worker(self):
        """Run the last worker body the async load started."""
        self.spawned.pop()()

    def deliver(self, index=0):
        fn, args = self.posted.pop(index)
        fn(*args)


def bundle(paths, colors=None):
    recs = [Rec(p) for p in paths]
    return {"records": recs, "by_path": {r.path: r for r in recs},
            "tag_universe": {p for p in paths}, "colors": colors or {}}


def patch_threads(tab, monkeypatch):
    """Capture thread bodies instead of running them."""
    import paz_suite.library_tab as lt

    class Fake:
        def __init__(self, target=None, daemon=False, **kw):
            tab.spawned.append(target)

        def start(self):
            pass

    monkeypatch.setattr(lt.threading, "Thread", Fake)


# ── the synchronous form still works ────────────────────────────────────

def test_the_constructor_load_installs_at_once():
    tab = FakeTab([bundle(["a.mp4", "b.mp4"])])
    tab._load_library()
    assert [r.path for r in tab.records] == ["a.mp4", "b.mp4"]
    assert tab.posted == []                     # nothing deferred


def test_installing_clears_the_similarity_weights():
    """They are built from the whole library, so they cannot outlive it."""
    tab = FakeTab([bundle(["a.mp4"])])
    tab._load_library()
    assert tab._weights is None


def test_installing_fills_the_lookups():
    tab = FakeTab([bundle(["a.mp4"], {"PMV": "#ff4fa3"})])
    tab._load_library()
    assert tab.by_path["a.mp4"] is tab.records[0]
    assert tab.tag_universe == {"a.mp4"}
    assert tab._project_colors == {"PMV": "#ff4fa3"}


# ── the async form ──────────────────────────────────────────────────────

def test_the_read_happens_off_the_thread(monkeypatch):
    tab = FakeTab([bundle(["a.mp4"])])
    patch_threads(tab, monkeypatch)
    tab.load_library_async()
    assert tab.read_calls == 0                  # not yet - it is on a worker
    assert tab.records == []
    tab.run_worker()
    assert tab.read_calls == 1
    assert tab.records == []                    # still not installed
    tab.deliver()
    assert [r.path for r in tab.records] == ["a.mp4"]


def test_the_caller_runs_after_the_records_are_there(monkeypatch):
    tab = FakeTab([bundle(["a.mp4", "b.mp4"])])
    patch_threads(tab, monkeypatch)
    seen = []
    tab.load_library_async(lambda: seen.append(len(tab.records)))
    tab.run_worker()
    tab.deliver()
    assert seen == [2]


def test_no_callback_is_fine(monkeypatch):
    tab = FakeTab([bundle(["a.mp4"])])
    patch_threads(tab, monkeypatch)
    tab.load_library_async()
    tab.run_worker()
    tab.deliver()
    assert len(tab.records) == 1


def test_a_read_that_throws_does_not_install_anything(monkeypatch):
    tab = FakeTab()
    patch_threads(tab, monkeypatch)

    def boom():
        raise OSError("the database went away")

    tab._read_library = boom
    tab.load_library_async(lambda: tab.posted.append("ran"))
    tab.run_worker()                            # must not raise
    assert tab.posted == []                     # nothing to deliver
    assert tab.records == []


# ── the newer read wins ─────────────────────────────────────────────────

def test_a_stale_result_is_thrown_away(monkeypatch):
    """Sync twice in a row and the first read must not land on top of the
    second - that is how you end up looking at a library that is one
    operation out of date."""
    tab = FakeTab([bundle(["old.mp4"]), bundle(["new.mp4"])])
    patch_threads(tab, monkeypatch)
    tab.load_library_async()
    tab.run_worker()                            # first read done, not delivered
    tab.load_library_async()
    tab.run_worker()                            # second read done

    tab.deliver(1)                              # the newer one lands first
    assert [r.path for r in tab.records] == ["new.mp4"]
    tab.deliver(0)                              # the older one arrives late
    assert [r.path for r in tab.records] == ["new.mp4"]


def test_a_stale_callback_does_not_run(monkeypatch):
    tab = FakeTab([bundle(["old.mp4"]), bundle(["new.mp4"])])
    patch_threads(tab, monkeypatch)
    ran = []
    tab.load_library_async(lambda: ran.append("old"))
    tab.run_worker()
    tab.load_library_async(lambda: ran.append("new"))
    tab.run_worker()
    tab.deliver(1)
    tab.deliver(0)
    assert ran == ["new"]


def test_the_synchronous_load_also_invalidates_a_read_in_flight(monkeypatch):
    """_load_library counts as a newer load, or a worker started before it
    would overwrite what it just installed."""
    tab = FakeTab([bundle(["async.mp4"]), bundle(["sync.mp4"])])
    patch_threads(tab, monkeypatch)
    tab.load_library_async()
    tab.run_worker()
    tab._load_library()
    assert [r.path for r in tab.records] == ["sync.mp4"]
    tab.deliver()
    assert [r.path for r in tab.records] == ["sync.mp4"]


# ── prepared tiles do not survive a new library ─────────────────────────

def test_installing_a_library_drops_the_prepared_tiles():
    """Tiles are keyed by path, and a sync or a rebuild can have replaced
    the thumbnail behind one - so a kept tile would be a picture of what
    the clip used to look like."""
    tab = FakeTab([bundle(["a.mp4"])])
    tab._load_library()
    assert tab._tiles == {}
    assert tab._photos == {}


def test_installing_a_library_cancels_a_prefetch_in_flight():
    tab = FakeTab([bundle(["a.mp4"])])
    before = tab._prefetch_token
    tab._load_library()
    assert tab._prefetch_token != before


def test_a_finished_load_settles_the_heap_and_warms_like_this():
    """The heap is frozen and the similarity weights built after every
    load - see paz_suite/heap.py - on timers, never inside the load."""
    from paz_suite import heap
    tab = FakeTab()
    tab.load_library_async()
    fn, args = tab.posted[-1] if tab.posted else (None, None)
    if fn is None:
        # The worker has to run first; the fake runs it on the spot.
        for spawn in tab.spawned:
            spawn()
        fn, args = tab.posted[-1]
    fn(*args)
    assert heap.settle in tab.timers
    assert tab._warm_weights in tab.timers


def test_warmed_weights_are_kept_only_for_the_library_they_came_from(monkeypatch):
    from paz_suite import library_tab

    class Now:
        def __init__(self, target, daemon=True):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(library_tab.threading, "Thread", Now)

    class Tab:
        _warm_weights = LibraryTab._warm_weights

        def __init__(self):
            self.records = []
            self._weights = None
            self.later = []

        def ui(self, fn, *args):
            self.later.append((fn, args))

    class Rec:
        def __init__(self, tags):
            self.tags = set(tags)

    tab = Tab()
    tab.records = [Rec(["wolf", "fox"]), Rec(["wolf"]), Rec(["deer"])]
    tab._warm_weights()
    fn, args = tab.later.pop()
    fn(*args)
    assert tab._weights and "fox" in tab._weights

    # A reload lands while a warm-up is still out: its answer is dropped.
    tab._weights = None
    tab._warm_weights()
    tab.records = [Rec(["other"])]
    fn, args = tab.later.pop()
    fn(*args)
    assert tab._weights is None
