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
    player.position = 5.0
    assert player.check_progress() == ""


def test_a_stopped_clock_is_reported(player):
    player.load("/clips/a.mp4", 12.0, 24.0)
    player.playing = True
    player._played_from = 0.0
    player._played_at = time.monotonic() - 99
    player.position = 0.0
    assert "VLC" in player.check_progress()


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
    monkeypatch.setattr(vp, "bindings_present", lambda: True)
    monkeypatch.setattr(vp, "library_path", lambda: "/usr/lib/libvlc.so")
    assert vp.available() is True
    monkeypatch.setattr(vp, "library_path", lambda: "")
    assert vp.available() is False
