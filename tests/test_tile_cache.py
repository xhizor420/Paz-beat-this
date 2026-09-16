"""Prepared gallery tiles: kept, and prepared before they are asked for.

Composing one tile - read the 320px thumbnail, decode it, scale it to the
card, fill the letterbox, round the corners - is about three
milliseconds, and a page is forty-eight of them. That work is already off
the UI thread, so it is not a freeze; it is a sixth of a second between
asking for a page and seeing it. Keeping the tiles makes flipping back
free, and preparing the neighbouring pages makes flipping forward free
too.

What can go wrong is mostly about keys: a tile filed under the wrong card
width shows at the wrong size, and a tile kept across a thumbnail rebuild
is a picture of what the clip used to look like.
"""

from __future__ import annotations

import collections
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab              # noqa: E402


class Rec:
    def __init__(self, path):
        self.path = self.name = path


class Cfg:
    def __init__(self, page_size=4):
        self.page_size = page_size
        self.thumb_fit = "contain"


class FakeTab:
    TILE_CACHE = 6
    PHOTO_CACHE = 6
    TILE_WORKERS = LibraryTab.TILE_WORKERS
    _start_thumbs = LibraryTab._start_thumbs
    PREFETCH_PAUSE = 0.0          # no need to be polite in a test
    PREFETCH_AFTER_MS = LibraryTab.PREFETCH_AFTER_MS
    _tile_get = LibraryTab._tile_get
    _tile_put = LibraryTab._tile_put
    _photo_put = LibraryTab._photo_put
    _load_thumbs = LibraryTab._load_thumbs
    _prefetch_pages = LibraryTab._prefetch_pages

    CARD_W, IMG_H = 224, 126

    def __init__(self, clips=12, page_size=4):
        self.cfg = Cfg(page_size)
        self.filtered = [Rec(f"{i}.mp4") for i in range(clips)]
        self._tiles = collections.OrderedDict()
        self._photos = collections.OrderedDict()
        self._tiles_lock = threading.Lock()
        self._prefetch_token = 0
        self._page_token = 1
        self.page = 0
        self.built = []
        self.placed = []
        self.spawned = []
        self.deferred = []

    def _tile_build(self, rec, width, height, fit):
        self.built.append((rec.path, width, height, fit))
        return f"tile({rec.path},{width}x{height})"

    def ui(self, fn, *args):
        # Bound methods compare unequal each time they are looked up, so
        # the underlying function is what identifies the call.
        if getattr(fn, "__func__", None) is FakeTab._prefetch_pages:
            fn(*args)                  # the real thing, so threads are seen
            return
        self.placed.append(args)

    def _place_thumb(self, *_a):
        pass

    def after(self, _ms, fn):
        """The prefetch waits a moment before starting, so it does not
        compete with the page that is still settling. Captured rather
        than run, so a test can say when that moment arrives."""
        self.deferred.append(fn)
        return "job"

    def start_prefetch(self):
        """Let the waited-for prefetch begin, then run its thread body."""
        while self.deferred:
            self.deferred.pop(0)()
        if self.spawned:
            self.spawned.pop()()


def patch_threads(tab, monkeypatch):
    import paz_suite.library_tab as lt

    class Fake:
        """Captures the thread body with its arguments already bound, so a
        test can run it by calling it."""

        def __init__(self, target=None, args=(), daemon=False, **kw):
            tab.spawned.append(lambda: target(*args))

        def start(self):
            pass

    monkeypatch.setattr(lt.threading, "Thread", Fake)


def key(path, tab):
    return (path, tab.CARD_W, tab.IMG_H, tab.cfg.thumb_fit)


# ── the cache ───────────────────────────────────────────────────────────

def test_a_tile_is_prepared_once():
    tab = FakeTab()
    tab._tile_put(key("a.mp4", tab), "tile")
    assert tab._tile_get(key("a.mp4", tab)) == "tile"


def test_a_tile_for_a_different_card_width_is_a_different_tile():
    """The card width changes with the window, and a tile composed for one
    width shown at another is the wrong size."""
    tab = FakeTab()
    tab._tile_put(("a.mp4", 224, 126, "contain"), "narrow")
    assert tab._tile_get(("a.mp4", 400, 225, "contain")) is None


def test_a_tile_for_a_different_fit_is_a_different_tile():
    tab = FakeTab()
    tab._tile_put(("a.mp4", 224, 126, "contain"), "letterboxed")
    assert tab._tile_get(("a.mp4", 224, 126, "cover")) is None


def test_a_failed_tile_is_not_cached():
    """Otherwise a thumbnail that was not ready yet stays blank for as
    long as the cache holds it."""
    tab = FakeTab()
    tab._tile_put(key("a.mp4", tab), None)
    assert tab._tile_get(key("a.mp4", tab)) is None
    assert not tab._tiles


def test_the_cache_stops_growing():
    tab = FakeTab()
    for i in range(tab.TILE_CACHE * 3):
        tab._tile_put(key(f"{i}.mp4", tab), f"tile{i}")
    assert len(tab._tiles) == tab.TILE_CACHE


def test_the_least_recently_wanted_tile_goes_first():
    tab = FakeTab()
    for i in range(tab.TILE_CACHE):
        tab._tile_put(key(f"{i}.mp4", tab), f"tile{i}")
    tab._tile_get(key("0.mp4", tab))              # asked for again
    tab._tile_put(key("new.mp4", tab), "new")
    assert tab._tile_get(key("0.mp4", tab)) == "tile0"
    assert tab._tile_get(key("1.mp4", tab)) is None


# ── loading a page uses what is already there ───────────────────────────

def test_a_page_of_tiles_is_built_and_placed(monkeypatch):
    tab = FakeTab()
    patch_threads(tab, monkeypatch)     # so the prefetch does not also build
    tab._load_thumbs(tab.filtered[:4], tab._page_token)
    assert len(tab.built) == 4
    assert len(tab.placed) == 4


def test_a_page_seen_before_builds_nothing(monkeypatch):
    tab = FakeTab()
    patch_threads(tab, monkeypatch)
    batch = tab.filtered[:4]
    tab._load_thumbs(batch, tab._page_token)
    tab.built.clear()
    tab._load_thumbs(batch, tab._page_token)
    assert tab.built == []
    assert len(tab.placed) == 8          # still placed, just not rebuilt


def test_a_page_turn_mid_load_stops_preparing_the_old_page():
    """Card three of the page you just left must not be composed, let
    alone painted over card three of the one you are on. The tile that
    was already in flight when the page turned is stopped by
    _place_thumb's own token check, which is why the count below is the
    one that had already started rather than zero."""
    tab = FakeTab()

    def build(rec, width, height, fit):
        tab.built.append(rec.path)
        if len(tab.built) == 2:
            tab._page_token += 1         # the user turned the page
        return "tile"

    tab._tile_build = build
    tab._load_thumbs(tab.filtered[:4], 1)
    assert len(tab.built) == 2, "kept composing a page nobody is looking at"


def test_the_size_is_taken_once_not_per_tile(monkeypatch):
    """A resize part-way through a page must not file half the tiles under
    one width and half under another."""
    tab = FakeTab()
    patch_threads(tab, monkeypatch)

    def build(rec, width, height, fit):
        tab.built.append((rec.path, width))
        tab.CARD_W = 999                 # the window is being dragged
        return "tile"

    tab._tile_build = build
    tab._load_thumbs(tab.filtered[:4], tab._page_token)
    assert {w for _p, w in tab.built} == {224}


# ── preparing the pages around this one ─────────────────────────────────

def test_finishing_a_page_starts_the_prefetch(monkeypatch):
    tab = FakeTab()
    patch_threads(tab, monkeypatch)
    tab._load_thumbs(tab.filtered[:4], tab._page_token)
    assert tab.deferred, "nothing was queued to prepare ahead"
    tab.deferred.pop(0)()
    assert tab.spawned, "nothing was prepared ahead"


def test_an_abandoned_page_does_not_start_one(monkeypatch):
    tab = FakeTab()
    patch_threads(tab, monkeypatch)
    tab._load_thumbs(tab.filtered[:4], 999)     # a stale token
    assert tab.deferred == []
    assert tab.spawned == []


def test_the_prefetch_covers_forward_and_back(monkeypatch):
    tab = FakeTab(clips=12, page_size=4)
    tab.page = 1
    patch_threads(tab, monkeypatch)
    tab._prefetch_pages()
    tab.start_prefetch()
    prepared = {path for path, *_rest in tab.built}
    assert "8.mp4" in prepared, "the next page was not prepared"
    assert "0.mp4" in prepared, "the previous page was not prepared"


def test_the_prefetch_skips_what_is_already_prepared(monkeypatch):
    tab = FakeTab()
    patch_threads(tab, monkeypatch)
    tab._tile_put(key("4.mp4", tab), "already")
    tab._prefetch_pages()
    tab.start_prefetch()
    assert "4.mp4" not in {path for path, *_rest in tab.built}


def test_a_newer_prefetch_stops_the_older_one(monkeypatch):
    """Flipping quickly should not leave four threads preparing pages
    nobody is looking at any more."""
    tab = FakeTab(clips=40, page_size=4)
    patch_threads(tab, monkeypatch)
    tab._prefetch_pages()
    tab.deferred.pop(0)()                      # the first one gets going
    first = tab.spawned.pop()
    tab.page = 5
    tab._prefetch_pages()
    tab.deferred.pop(0)()
    tab.spawned.pop()                          # the newer one wins the token
    tab.built.clear()
    first()                                    # the older one runs late
    assert tab.built == []


def test_a_prefetch_past_the_last_page_prepares_nothing(monkeypatch):
    tab = FakeTab(clips=4, page_size=4)
    patch_threads(tab, monkeypatch)
    tab._prefetch_pages()
    assert tab.deferred == []
    assert tab.spawned == []


def test_no_results_means_no_prefetch(monkeypatch):
    tab = FakeTab(clips=0)
    patch_threads(tab, monkeypatch)
    tab._prefetch_pages()
    assert tab.deferred == []
    assert tab.spawned == []


# ── the prefetch gets out of the way ───────────────────────────────────

def test_the_prefetch_waits_before_it_starts():
    """The page that just rendered is still settling its own tiles, and
    they matter more than the next page's."""
    tab = FakeTab()
    tab._prefetch_pages()
    assert tab.deferred, "it went straight to work"
    assert tab.spawned == []


def test_a_page_turn_during_the_wait_cancels_it():
    tab = FakeTab(clips=40)
    tab._prefetch_pages()
    begin = tab.deferred.pop(0)
    tab._prefetch_token += 1            # the page turned meanwhile
    begin()
    assert tab.spawned == [], "prepared a page nobody is near any more"


def test_the_prefetch_pauses_between_tiles():
    """It is PIL work on a worker, and PIL holds the interpreter for much
    of its run - going flat out, three pages of it turned a 50ms worst
    case for clicking through clips into 300ms."""
    from paz_suite.library_tab import LibraryTab as Real
    assert Real.PREFETCH_PAUSE > 0
    assert Real.PREFETCH_AFTER_MS > 0


def test_it_prepares_one_page_either_side_not_more(monkeypatch):
    """Every page prepared is background work competing with the window."""
    tab = FakeTab(clips=400, page_size=4)
    patch_threads(tab, monkeypatch)
    tab.page = 10
    tab._prefetch_pages()
    tab.start_prefetch()
    assert len(tab.built) == 8


# ── a page is composed by several threads at once ──────────────────────

def test_the_stripes_between_them_cover_every_card():
    """Each loader handles index % step == start, so a wrong stripe
    silently leaves cards blank."""
    tab = FakeTab(clips=12, page_size=12)
    batch = tab.filtered[:12]
    for start in range(4):
        tab._load_thumbs(batch, tab._page_token, start, 4)
    placed = sorted(args[0] for args in tab.placed)
    assert placed == list(range(12))


def test_a_stripe_only_does_its_own_share(monkeypatch):
    tab = FakeTab(clips=12, page_size=12)
    patch_threads(tab, monkeypatch)
    tab._load_thumbs(tab.filtered[:12], tab._page_token, 1, 4)
    assert sorted(args[0] for args in tab.placed) == [1, 5, 9]


def test_one_stripe_asks_for_the_prefetch_not_all_of_them(monkeypatch):
    tab = FakeTab(clips=40, page_size=4)
    patch_threads(tab, monkeypatch)
    for start in range(4):
        tab._load_thumbs(tab.filtered[:4], tab._page_token, start, 4)
    assert len(tab.deferred) == 1, "every stripe queued its own prefetch"


def test_starting_a_page_spawns_a_worker_per_stripe(monkeypatch):
    tab = FakeTab(clips=48, page_size=48)
    patch_threads(tab, monkeypatch)
    tab._start_thumbs(tab.filtered[:48], tab._page_token)
    assert len(tab.spawned) == tab.TILE_WORKERS


def test_a_short_page_does_not_spawn_more_threads_than_cards(monkeypatch):
    tab = FakeTab(clips=1, page_size=1)
    patch_threads(tab, monkeypatch)
    tab._start_thumbs(tab.filtered[:1], tab._page_token)
    assert len(tab.spawned) == 1


def test_an_empty_page_spawns_one_that_does_nothing(monkeypatch):
    tab = FakeTab(clips=0)
    patch_threads(tab, monkeypatch)
    tab._start_thumbs([], tab._page_token)
    for body in tab.spawned:
        body()
    assert tab.placed == []
