"""Sweeping a gallery tile has to walk the whole clip.

These clips run three to seven minutes. A tile shows one frame of that,
and the in-tile reel shows eleven seconds of it, so neither answers "what
is in this clip" - which is the whole question a media bin exists to
answer. Hover-scrub does: the pointer's position across a card is a
position in the clip, and the card shows that moment.

Three things have to hold or the gesture is worse than useless:

  * the frame shown must be the moment the pointer is ON, not a moment it
    passed through - a sweep fires motion events far faster than frames
    can be decoded, and a queue of them plays the sweep back in slow
    motion seconds after the hand has finished;
  * work for a card that is no longer under the pointer, or no longer
    even the same clip after a page turn, must never paint;
  * letting go must carry on from where you stopped, not jump back to the
    start of the clip.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402
from paz_suite.media import ThumbCache            # noqa: E402


class Rec:
    def __init__(self, path="P:/clips/1.mp4", duration=300.0):
        self.path, self.duration = path, duration


class FakeCanvas:
    """Records what was painted; answers the few queries the scrub asks."""

    def __init__(self):
        self.images = {}       # tag -> image handed to itemconfigure
        self.texts = []
        self.deleted = []

    def canvasx(self, x):
        return float(x)

    def canvasy(self, y):
        return float(y)

    def find_withtag(self, tag):
        return (1,)

    def itemconfigure(self, tag, **kw):
        if "image" in kw:
            self.images[tag] = kw["image"]

    def delete(self, tag):
        self.deleted.append(tag)

    def create_rectangle(self, *a, **kw):
        return 1

    def create_line(self, *a, **kw):
        return 1

    def create_text(self, *a, **kw):
        self.texts.append(kw.get("text", ""))
        return 1


class FakeFont:
    def measure(self, text):
        return 7 * len(text)


class FakeFrames:
    """A ThumbCache stand-in that records what moment was asked for."""

    def __init__(self):
        self.asked = []

    def hover_frame(self, path, duration, frac, **kw):
        self.asked.append((path, round(frac, 4)))
        return b"jpeg-" + str(round(frac, 4)).encode()

    def prime_hover(self, *a, **kw):
        pass

    reel_start = ThumbCache.reel_start


class FakeTab:
    """Just enough of the tab to exercise the gesture, without Tk."""

    CARD_W = 200
    IMG_H = 112
    SCRUB_MIN_PX = LibraryTab.SCRUB_MIN_PX
    SCRUB_REST_MS = LibraryTab.SCRUB_REST_MS

    _scrub_motion = LibraryTab._scrub_motion
    _scrub_arm_rest = LibraryTab._scrub_arm_rest
    _scrub_rest = LibraryTab._scrub_rest
    _scrub_request = LibraryTab._scrub_request
    _scrub_fetch = LibraryTab._scrub_fetch
    _scrub_show = LibraryTab._scrub_show
    _scrub_reset = LibraryTab._scrub_reset
    _scrub_stop = LibraryTab._scrub_stop
    _draw_scrub = LibraryTab._draw_scrub
    _reel_key = LibraryTab._reel_key
    _set_hover = LibraryTab._set_hover
    _card_box = LibraryTab._card_box
    _still_on_card = LibraryTab._still_on_card
    _unhover = LibraryTab._unhover
    IMG_H = 112
    CAP_H = 50

    def __init__(self, recs=None, card_w=200):
        self.CARD_W = card_w
        self.gallery = FakeCanvas()
        self.frames = FakeFrames()
        self._badge_font = FakeFont()
        self._layout = [{"rec": r, "x": i * card_w, "y": 0, "tag": f"card{i}"}
                        for i, r in enumerate(recs or [Rec()])]
        self._static_thumb = {}
        self._sb_index = None
        self._sb_token = 0
        self._sb_busy = False
        self._sb_want = None
        self._sb_frac = 0.0
        self._sb_x = None
        self._sb_photo = None
        self._sb_after = None
        self.armed = []          # (index, at) the reel was asked to play from
        self.stops = []          # restore= flags the reel was stopped with
        self.timers = []
        self.painted = []        # (index, frac) actually drawn
        self._hover_index = None
        self.restyled = 0

    # -- the surrounding machinery, stubbed --------------------------------
    def ui(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def after(self, ms, fn):
        self.timers.append((ms, fn))
        return len(self.timers)

    def after_cancel(self, tid):
        pass

    def _preview_stop(self, restore=True):
        self.stops.append(restore)

    def _preview_arm(self, index, at=None):
        self.armed.append((index, at))

    def _tile_image(self, data):
        # The worker's half of the job: bytes -> a shaped picture.
        return data

    def _tile_photo(self, image):
        return image

    def _draw_progress(self, index, frac):
        self.painted.append((index, round(frac, 4)))

    def _restyle_cards(self):
        self.restyled += 1


class Event:
    def __init__(self, x, y=50):
        self.x, self.y = x, y


@pytest.fixture
def inline(monkeypatch):
    """Run the scrub's worker inline so the test is deterministic.

    Patched on the library_tab module only, and reverted by pytest - a
    global threading patch leaks into whatever else is running.
    """
    class InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._call = (target, args, kwargs or {})

        def start(self):
            target, args, kwargs = self._call
            target(*args, **kwargs)

    import paz_suite.library_tab as lt

    class Shim:
        Thread = InlineThread
    monkeypatch.setattr(lt, "threading", Shim)
    return None


# ── the pointer's position is a position in the clip ────────────────────

def test_the_pointer_position_across_a_card_is_a_position_in_the_clip(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._scrub_motion(Event(150), 0)
    assert tab.frames.asked == [("P:/clips/1.mp4", 0.75)]
    assert tab.painted == [(0, 0.75)]


def test_the_ends_of_a_card_are_the_ends_of_the_clip(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._scrub_motion(Event(0), 0)
    tab._sb_x = None
    tab._scrub_motion(Event(200), 0)
    assert [f for _, f in tab.frames.asked] == [0.0, 1.0]


def test_a_pointer_past_the_edge_cannot_ask_for_a_moment_outside_the_clip(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._scrub_motion(Event(-40), 0)
    tab._sb_x = None
    tab._scrub_motion(Event(999), 0)
    assert [f for _, f in tab.frames.asked] == [0.0, 1.0]


def test_a_card_further_along_the_row_still_scrubs_from_its_own_left_edge(inline):
    tab = FakeTab([Rec("a.mp4"), Rec("b.mp4"), Rec("c.mp4")], card_w=200)
    tab._scrub_motion(Event(500), 2)          # 100px into the third card
    assert tab.frames.asked == [("c.mp4", 0.5)]


def test_a_clip_with_no_duration_is_not_scrubbed(inline):
    tab = FakeTab([Rec(duration=0.0)])
    tab._scrub_motion(Event(100), 0)
    assert tab.frames.asked == []


def test_a_pointer_that_has_not_moved_leaves_the_reel_alone(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._scrub_motion(Event(100), 0)
    before = len(tab.frames.asked)
    tab._scrub_motion(Event(101), 0)          # inside SCRUB_MIN_PX
    assert len(tab.frames.asked) == before


# ── a sweep must not queue up behind itself ─────────────────────────────

def test_a_sweep_only_ever_fetches_the_position_the_hand_is_on_now():
    """No inline fixture: the fetch stays "in flight", which is the case
    that matters - every motion event during it must collapse into one."""
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._sb_index = 0
    rec = tab._layout[0]["rec"]
    tab._sb_busy = True                        # a fetch is already out
    for x in range(10, 200, 10):
        tab._scrub_request(0, rec, x / 200)
    assert tab.frames.asked == []              # nothing new was started
    assert tab._sb_want == pytest.approx(0.95)  # only the newest survived


def test_the_position_that_arrived_mid_fetch_is_the_one_fetched_next(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._sb_index = 0
    tab._sb_busy = True
    tab._scrub_request(0, tab._layout[0]["rec"], 0.4)
    tab._sb_busy = False
    tab._scrub_show(0, 0.1, b"jpeg", tab._sb_token)
    assert [f for _, f in tab.frames.asked] == [0.4]


# ── stale work must never paint ─────────────────────────────────────────

def test_a_frame_for_a_card_the_pointer_has_left_is_dropped(inline):
    tab = FakeTab([Rec("a.mp4"), Rec("b.mp4")])
    tab._sb_index = 1
    tab._scrub_show(0, 0.5, b"jpeg", tab._sb_token)
    assert tab.painted == []


def test_a_frame_from_before_the_page_turned_is_dropped(inline):
    tab = FakeTab([Rec(duration=400.0)])
    tab._sb_index = 0
    stale = tab._sb_token
    tab._scrub_reset()                         # what a page rebuild does
    tab._scrub_show(0, 0.5, b"jpeg", stale)
    assert tab.painted == []


def test_leaving_a_card_puts_the_resting_thumbnail_back():
    tab = FakeTab([Rec()])
    tab._static_thumb[0] = "resting"
    tab._sb_index = 0
    tab._scrub_stop()
    assert tab.gallery.images["im0"] == "resting"
    assert tab._sb_index is None


# ── scrubbing must not strobe the tile ──────────────────────────────────

def test_scrubbing_stops_the_reel_without_restoring_the_thumbnail(inline):
    """The scrub paints the tile itself, a moment later. Putting the
    resting thumbnail back in between would flash the card once per
    motion event - which is every few pixels of a sweep."""
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    for x in (20, 60, 100):
        tab._sb_x = None
        tab._scrub_motion(Event(x), 0)
    assert tab.stops == [False, False, False]


# ── letting go carries on from where you stopped ────────────────────────

def test_resting_after_a_scrub_plays_from_where_the_scrub_stopped(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._scrub_motion(Event(150), 0)
    assert tab.timers, "stillness should have been armed"
    tab.timers[-1][1]()                        # the rest timer fires
    assert tab.armed == [(0, pytest.approx(300.0))]


def test_stillness_is_armed_fresh_on_every_move(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    for x in (20, 60, 100):
        tab._sb_x = None
        tab._scrub_motion(Event(x), 0)
    assert len(tab.timers) == 3


# ── the reel that follows is a different reel ───────────────────────────

def test_reels_are_told_apart_by_where_they_start():
    tab = FakeTab([Rec()])
    assert tab._reel_key("a.mp4", 0.0) != tab._reel_key("a.mp4", 120.0)
    assert tab._reel_key("a.mp4", 120.04) == tab._reel_key("a.mp4", 120.0)


def test_a_reel_asked_for_a_moment_starts_there():
    assert ThumbCache.reel_start(400.0, 120.0) == 120.0


def test_a_reel_asked_for_nothing_starts_a_little_way_in():
    """The first moments of a clip are often a fade or a title card."""
    assert ThumbCache.reel_start(400.0) == pytest.approx(32.0)
    assert ThumbCache.reel_start(4.0) == 0.0


def test_a_reel_cannot_start_past_the_end_of_the_clip():
    assert ThumbCache.reel_start(400.0, 400.0) == pytest.approx(399.5)
    assert ThumbCache.reel_start(400.0, -5.0) == 0.0
    assert ThumbCache.reel_start(0.0, 10.0) == 0.0


# ── the readout ─────────────────────────────────────────────────────────

def test_the_moment_is_written_on_the_tile_in_figures():
    """A bar alone says roughly where you are; on a seven-minute clip
    "roughly" is a minute wide."""
    tab = FakeTab([Rec(duration=420.0)], card_w=200)
    tab._draw_scrub(0, 0.5)
    assert "3:30 / 7:00" in tab.gallery.texts


# ── a card is made of many items, and crossing between them is not
#    leaving the card ───────────────────────────────────────────────────

def test_crossing_between_a_cards_own_items_does_not_stop_the_scrub(inline):
    """Tk raises Leave then Enter every time the pointer crosses from a
    card's picture to its badge, its pill or its caption - all of which
    wear the same tag. Believing those tore the scrub down and rebuilt it
    every few pixels of a sweep, which is when it is actually in use."""
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._hover_index = 0
    tab._scrub_motion(Event(100), 0)
    assert tab._sb_index == 0
    tab._unhover(0, Event(120, 60))        # still inside the card
    assert tab._sb_index == 0
    assert tab._hover_index == 0


def test_leaving_the_card_for_real_does_stop_the_scrub(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._hover_index = 0
    tab._scrub_motion(Event(100), 0)
    tab._unhover(0, Event(300, 60))        # past the right edge
    assert tab._sb_index is None
    assert tab._hover_index is None


def test_a_leave_below_the_caption_is_a_real_leave(inline):
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._hover_index = 0
    tab._scrub_motion(Event(100), 0)
    tab._unhover(0, Event(100, FakeTab.IMG_H + FakeTab.CAP_H + 5))
    assert tab._sb_index is None


def test_a_leave_with_no_event_is_a_real_leave(inline):
    """Code paths that unhover without an event - a page rebuild, say -
    mean it."""
    tab = FakeTab([Rec(duration=400.0)], card_w=200)
    tab._hover_index = 0
    tab._scrub_motion(Event(100), 0)
    tab._unhover(0)
    assert tab._sb_index is None


def test_the_pointer_leaving_the_grid_is_not_a_card(inline):
    """_leave_grid unhovers with no card at all. Priming a storyboard for
    "nothing" used to be a crash in the middle of the hover path."""
    tab = FakeTab([Rec(duration=400.0)])
    tab._hover_index = 0
    tab._set_hover(None)
    assert tab._hover_index is None
