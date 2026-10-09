"""Cover for the libVLC backend that does not need VLC or a display.

The thing worth pinning here is the property the backend exists for: the
UI thread never calls libVLC, so nothing libVLC does can stall the window.
Every method the inspector calls must return immediately and leave a
command behind for the worker to run. Whether VLC then draws a picture is
VLC's business and can only be checked against a real one; tests/…/verify
drives that.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import vlc_player as vp     # noqa: E402


@pytest.fixture
def player():
    """A player whose worker thread is never started, so the queue it
    leaves behind is exactly what the UI thread put there."""
    p = vp.VlcPlayer.__new__(vp.VlcPlayer)
    p._worker_started = False
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        p.__init__(widget=None, width=854, height=480)
    finally:
        threading.Thread.start = real_start
    return p


def drain(p):
    out = []
    while not p._q.empty():
        out.append(p._q.get_nowait())
    return out


def names(p):
    return [c[0] for c in drain(p)]


# ── the point of the design ─────────────────────────────────────────────

def test_no_ui_thread_call_touches_libvlc(player):
    """Not one of these may reach into VLC; they all queue and return."""
    player.load("/clips/a.mp4", 12.0, 24.0)
    player.play()
    player.seek(4.0)
    player.nudge(2.0)
    player.pause()
    player.set_volume(50)
    player.toggle_mute()
    player.set_speed(2.0)
    player.stop()
    # _player is still None: nothing above went near libVLC.
    assert player._player is None
    assert names(player) == ["load", "play", "seek", "seek", "pause",
                             "volume", "mute", "rate", "stop"]


def test_every_call_returns_at_once(player):
    player.load("/clips/a.mp4", 12.0, 24.0)
    start = time.monotonic()
    for _ in range(200):
        player.play()
        player.seek(3.0)
        player.pause()
    assert time.monotonic() - start < 0.1


def test_a_failed_start_reports_back_instead_of_going_quiet(player):
    told = []
    player.on_unavailable = told.append
    player._post = lambda fn, *a: fn(*a)
    player.widget = None            # winfo_id() will raise inside _boot
    assert player._boot() is False
    assert told and "VLC" in told[0]


def test_a_failed_start_stops_taking_commands(player):
    player._post = lambda fn, *a: fn(*a)
    player.on_unavailable = lambda why: None
    player._boot()
    drain(player)
    player.play()
    assert player._q.empty()


# ── state the inspector reads straight off the object ───────────────────

def test_load_reports_length_before_vlc_has_seen_the_file(player):
    player.load("/clips/a.mp4", 12.5, 24.0)
    assert player.path == "/clips/a.mp4"
    assert player.duration == 12.5
    assert player.position == 0.0
    assert player.playing is False


def test_seek_is_clamped_into_the_clip(player):
    player.load("/clips/a.mp4", 12.0, 24.0)
    player.seek(-5)
    assert player.position == 0.0
    player.seek(99)
    assert player.position == pytest.approx(11.9)


def test_seek_before_a_clip_is_loaded_does_nothing(player):
    player.seek(3.0)
    assert player._q.empty()


def test_unmuting_restores_the_volume(player):
    player.set_volume(70)
    player.muted = True
    drain(player)
    player.set_volume(40)
    assert player.muted is False
    assert ("mute", False) in drain(player) or player.volume == 40


# ── the stall watchdog ──────────────────────────────────────────────────

def test_a_moving_clock_is_not_a_stall(player):
    player.load("/clips/a.mp4", 12.0, 24.0)
    player.playing = True
    player._played_from = 0.0
    player._played_at = time.monotonic() - 99
    player._vlc_time = 5.0
    assert player.check_progress() == ""


def test_a_stopped_clock_is_reported(player):
    player.load("/clips/a.mp4", 12.0, 24.0)
    player.playing = True
    player._played_from = 0.0
    player._played_at = time.monotonic() - 99
    player._vlc_time = 0.0
    assert "VLC" in player.check_progress()


def test_the_smoothed_playhead_cannot_hide_a_stall(player):
    """position() coasts on between libVLC's reports by design. A stuck
    picture must still be caught off libVLC's own clock."""
    player.load("/clips/a.mp4", 12.0, 24.0)
    player.playing = True
    player._played_from = 0.0
    player._played_at = time.monotonic() - 99
    player._vlc_time = 0.0
    player.position = 0.5
    assert "VLC" in player.check_progress()


# ── a smooth playhead from a stepping clock ─────────────────────────────

class SteppingVlc:
    """libVLC as seen from _tick: a clock that moves in coarse steps."""

    def __init__(self, millis=0, moving=True):
        self.millis = millis
        self.moving = moving

    def get_time(self):
        return self.millis

    def get_length(self):
        return 12000

    def is_playing(self):
        return 1 if self.moving else 0

    def set_time(self, millis):
        pass                             # lands later, as the real one does


def _ticking(player, millis):
    player.load("/clips/a.mp4", 12.0, 60.0)
    player.playing = True
    player._played_at = time.monotonic()
    player._player = SteppingVlc(millis)
    return player._player


def test_the_playhead_moves_between_vlc_steps(player):
    """A clip with no sound reports time every 250ms. The seek bar
    followed it in quarter-second lurches."""
    vlc_side = _ticking(player, 2000)
    player._tick()
    player._vlc_at -= 0.1                # 100ms since libVLC last spoke
    player._tick()
    assert player.position == pytest.approx(2.1, abs=0.02)
    assert player._vlc_time == pytest.approx(2.0)
    vlc_side.millis = 2250
    player._tick()
    assert player.position == pytest.approx(2.25, abs=0.02)


def test_the_playhead_does_not_coast_for_ever(player):
    _ticking(player, 2000)
    player._tick()
    player._vlc_at -= 30.0
    player._tick()
    assert player.position == pytest.approx(2.0 + player.COAST, abs=0.02)


def test_a_late_report_does_not_twitch_the_playhead_back(player):
    vlc_side = _ticking(player, 2000)
    player._tick()
    player.position = 2.2
    vlc_side.millis = 2150               # behind where it coasted to
    player._tick()
    assert player.position >= 2.2


def test_a_loop_back_to_the_start_is_shown(player):
    vlc_side = _ticking(player, 11500)
    player._tick()
    vlc_side.millis = 0
    player._tick()
    assert player.position < 0.1


def test_after_a_seek_the_old_time_is_ignored(player):
    """Otherwise the playhead jumps back for a moment after every click."""
    vlc_side = _ticking(player, 2000)
    player._tick()
    player._apply(("seek", 8.0))
    vlc_side.millis = 2050               # libVLC hasn't moved yet
    player.position = 8.0
    player._tick()
    assert player.position == pytest.approx(8.0)
    vlc_side.millis = 8000
    player._tick()
    assert player.position == pytest.approx(8.0, abs=0.05)


def test_nothing_is_reported_while_paused(player):
    player.playing = False
    player._played_at = time.monotonic() - 99
    assert player.check_progress() == ""


# ── finding it ──────────────────────────────────────────────────────────

def test_why_not_names_the_missing_half(monkeypatch):
    monkeypatch.setattr(vp, "bindings_present", lambda: True)
    monkeypatch.setattr(vp, "library_path", lambda: "")
    assert "VLC itself" in vp.why_not()
    monkeypatch.setattr(vp, "bindings_present", lambda: False)
    monkeypatch.setattr(vp, "library_path", lambda: "/usr/lib/libvlc.so")
    assert "python-vlc" in vp.why_not()


def test_available_needs_both_halves(monkeypatch):
    """Both halves present is necessary but no longer sufficient - libVLC
    has to have actually loaded. Saying yes on the strength of the files
    being on disk meant picking a player that might be mid-load or
    broken, when falling through to the next one costs nothing."""
    monkeypatch.setattr(vp, "bindings_present", lambda: True)
    monkeypatch.setattr(vp, "library_path", lambda: "/usr/lib/libvlc.so")
    monkeypatch.setattr(vp, "_loaded", True)
    monkeypatch.setattr(vp, "_module", object())
    assert vp.available() is True
    monkeypatch.setattr(vp, "library_path", lambda: "")
    assert vp.available() is False


def test_not_available_until_it_has_loaded(monkeypatch):
    monkeypatch.setattr(vp, "bindings_present", lambda: True)
    monkeypatch.setattr(vp, "library_path", lambda: "/usr/lib/libvlc.so")
    monkeypatch.setattr(vp, "_loaded", False)
    monkeypatch.setattr(vp, "_module", None)
    monkeypatch.setattr(vp, "_load_started", True)   # don't start a thread
    assert vp.available() is False
    assert "loading" in vp.why_not()


def test_a_library_that_will_not_load_is_not_available(monkeypatch):
    monkeypatch.setattr(vp, "bindings_present", lambda: True)
    monkeypatch.setattr(vp, "library_path", lambda: "/usr/lib/libvlc.so")
    monkeypatch.setattr(vp, "_loaded", True)
    monkeypatch.setattr(vp, "_module", None)
    monkeypatch.setattr(vp, "_load_error", "OSError: wrong architecture")
    assert vp.available() is False
    assert "will not load" in vp.why_not()


def test_a_player_whose_library_never_loaded_says_so_and_steps_aside(monkeypatch):
    """Rather than reaching for the import itself on its worker thread,
    which is the thing that could take the process with it."""
    monkeypatch.setattr(vp, "_module", None)
    monkeypatch.setattr(vp, "_load_error", "OSError: nope")
    told = []
    p = vp.VlcPlayer.__new__(vp.VlcPlayer)
    p._worker = None
    p.__init__(widget=None, width=854, height=480)
    p.on_unavailable = told.append
    p._post = lambda fn, *a: fn(*a)
    assert p._boot() is False
    assert told and "libVLC" in told[0]
