"""The libmpv backend, without libmpv or a display.

What is pinned here is what the design depends on: the UI thread only
ever queues; a burst of loads (holding Down) opens only the newest clip;
a stop pauses instead of tearing the video output down; and the stall
watchdog still works.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import libmpv_player as lm      # noqa: E402


@pytest.fixture
def player():
    """A player whose worker never starts, so the queue it leaves behind is
    exactly what the UI thread put there."""
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        p = lm.LibMpvPlayer(widget=None, width=854, height=480)
    finally:
        threading.Thread.start = real_start
    return p


def drain(p):
    out = []
    while not p._q.empty():
        out.append(p._q.get_nowait())
    return out


class FakeMpv:
    """Records what the worker asks of mpv."""

    def __init__(self):
        self.loaded = []
        self.commands = []
        self.pause = True
        self.idle_active = False
        self.volume = 0
        self.mute = False
        self.speed = 1.0
        self.loop_file = "inf"

    def loadfile(self, path, mode, **options):
        self.loaded.append((path, mode, options))

    def command(self, *args):
        self.commands.append(args)


# ── the UI thread only queues ───────────────────────────────────────────

def test_no_ui_thread_call_touches_libmpv(player):
    player.load("/a.mp4", 12.0, 30.0)
    player.play()
    player.seek(4.0)
    player.nudge(1.0)
    player.pause()
    player.set_volume(40)
    player.toggle_mute()
    player.set_speed(1.5)
    player.toggle_loop()
    player.step(1)
    player.stop()
    assert player._mpv is None
    assert [c[0] for c in drain(player)] == [
        "load", "play", "seek", "seek", "pause", "volume", "mute", "rate",
        "loop", "step", "stop"]


def test_every_call_returns_at_once(player):
    player.load("/a.mp4", 12.0, 30.0)
    start = time.monotonic()
    for _ in range(300):
        player.play()
        player.seek(3.0)
        player.pause()
    assert time.monotonic() - start < 0.1


# ── holding Down ────────────────────────────────────────────────────────

def test_only_the_newest_load_is_opened(player):
    for name in ("a", "b", "c", "d"):
        player.load(f"/{name}.mp4", 10.0, 30.0)
    player.play()
    first = player._q.get_nowait()
    newest = player._newest_load(first)
    assert newest[1] == "/d.mp4"
    assert [c[0] for c in drain(player)] == ["play"], "the play after it must survive"


def test_a_drag_of_seeks_shows_only_where_the_pointer_is(player):
    player.load("/a.mp4", 12.0, 30.0)
    drain(player)
    for t in (1.0, 2.0, 3.0, 4.0):
        player.seek(t)
    player.play()
    first = player._q.get_nowait()
    assert player._newest_seek(first) == ("seek", 4.0)
    assert [c[0] for c in drain(player)] == ["play"]


# ── what the worker does ────────────────────────────────────────────────

def test_a_load_opens_paused_on_its_first_frame(player):
    player._mpv = FakeMpv()
    player._mpv.pause = False
    player._apply(("load", "/a.mp4", 0.0, False))
    path, mode, _options = player._mpv.loaded[-1]
    assert (path, mode) == ("/a.mp4", "replace")
    assert player._mpv.pause is True


def test_play_straight_after_a_load_is_not_undone_by_it(player):
    """Holding Down: load then play, back to back. The pause must not be
    a per-file option, which mpv applies later - after the play."""
    player._mpv = FakeMpv()
    player._apply(("load", "/a.mp4", 0.0, False))
    player._apply(("play",))
    _path, _mode, options = player._mpv.loaded[-1]
    assert "pause" not in options
    assert player._mpv.pause is False


def test_a_stop_pauses_rather_than_unloading(player):
    """Unloading takes the video output down; the next load would build it
    again - the cost this backend exists to avoid."""
    player._mpv = FakeMpv()
    player._mpv.pause = False
    player._apply(("stop",))
    assert player._mpv.pause is True
    assert player._mpv.commands == []


def test_play_after_the_end_starts_again(player):
    player._mpv = FakeMpv()
    player.path, player.duration, player.position = "/a.mp4", 10.0, 10.0
    player._eof = True
    player._apply(("play",))
    path, _mode, options = player._mpv.loaded[-1]
    assert path == "/a.mp4" and options["start"] == "0.000"


def test_a_clip_that_ends_without_looping_says_so(player):
    told = []
    player.on_state = lambda playing: told.append(("state", playing))
    player.on_eof = lambda: told.append(("eof",))
    player._post = lambda fn, *a: fn(*a)
    player.loop, player.playing = False, True
    player._seen_eof("eof-reached", True)
    player._tick()
    assert player.playing is False
    assert told == [("state", False), ("eof",)]


def test_a_seek_before_the_file_is_open_is_held_not_lost(player):
    """mpv drops a seek that arrives while it is still opening the file -
    Convert's preview opened every clip at 0:00 instead of where it
    asked for. Held, then made the moment the file is in."""
    player._mpv = FakeMpv()
    player._apply(("load", "/a.mp4", 0.0, False))
    player._apply(("seek", 3.0))
    assert player._mpv.commands == [], "sent while the file was still opening"
    player._seen_loaded(None)
    player._tick()
    assert player._mpv.commands == [("seek", "3.000", "absolute+exact")]


def test_a_seek_once_open_goes_straight_through(player):
    player._mpv = FakeMpv()
    player._apply(("load", "/a.mp4", 0.0, False))
    player._seen_loaded(None)
    player._apply(("seek", 4.0))
    assert player._mpv.commands == [("seek", "4.000", "absolute+exact")]


def test_frame_steps_use_mpvs_own(player):
    player._mpv = FakeMpv()
    player._apply(("step", 1))
    player._apply(("step", -1))
    assert player._mpv.commands == [("frame-step",), ("frame-back-step",)]


# ── the stall watchdog ──────────────────────────────────────────────────

def test_a_moving_clock_is_not_a_stall(player):
    player.playing = True
    player._played_at = time.monotonic() - 99
    player._moved_at = time.monotonic()
    assert player.check_progress() == ""


def test_a_stopped_clock_is_reported(player):
    player.playing = True
    player._played_at = time.monotonic() - 99
    player._moved_at = time.monotonic() - 99
    assert "mpv" in player.check_progress()


def test_nothing_is_reported_while_paused(player):
    player.playing = False
    player._played_at = time.monotonic() - 99
    assert player.check_progress() == ""


# ── finding it ──────────────────────────────────────────────────────────

def test_not_installed_is_not_available(monkeypatch):
    monkeypatch.setattr(lm, "library_dir", lambda: "")
    assert lm.available() is False
    assert "libmpv" in lm.why_not()


def test_a_load_that_failed_is_not_available(monkeypatch):
    monkeypatch.setattr(lm, "library_dir", lambda: "C:/x")
    monkeypatch.setattr(lm, "bindings_present", lambda: True)
    monkeypatch.setattr(lm, "_loaded", True)
    monkeypatch.setattr(lm, "_module", None)
    monkeypatch.setattr(lm, "_error", "OSError: bad dll")
    assert lm.available() is False
    assert "bad dll" in lm.why_not()
