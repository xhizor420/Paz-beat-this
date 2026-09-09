"""The sync fix: the picture is paced to the sound that has actually been
heard, not to the wall clock.

Two processes with no clock between them is what made the old built-in
player drift, and no amount of tuning fixes a missing clock. These pin the
clock's behaviour - including the ways it has to get out of the way, since
plenty of the converted library has no audio track at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import audio_out                          # noqa: E402
from paz_suite.player_engine import ClipPlayer           # noqa: E402


class FakeTrack:
    """Stands in for the audio device, so the clock can be tested without
    one - CI has no sound card and neither does a build machine."""

    def __init__(self, heard=None, failed=False, ended=False):
        self._heard = heard
        self.failed = failed
        self.ended = ended
        self.gain = 1.0

    def position(self):
        return self._heard

    def set_gain(self, gain):
        self.gain = gain

    def stop(self):
        pass


@pytest.fixture
def engine():
    p = ClipPlayer.__new__(ClipPlayer)
    p.audio_track = None
    p.speed = 1.0
    p.stream_fps = 24.0
    p._clock_t0 = time.monotonic() - 5.0
    p._clock_pos = 10.0
    p.muted = False
    p.volume = 80
    return p


# ── which clock is in charge ────────────────────────────────────────────

def test_the_sound_drives_the_picture(engine):
    engine.audio_track = FakeTrack(heard=12.5)
    assert engine._elapsed() == pytest.approx(2.5)


def test_the_wall_clock_takes_over_when_the_sound_fails(engine):
    engine.audio_track = FakeTrack(failed=True)
    assert engine._elapsed() == pytest.approx(5.0, abs=0.3)


def test_the_wall_clock_takes_over_when_the_sound_runs_out(engine):
    """A clip whose audio is shorter than its video still has to finish."""
    engine.audio_track = FakeTrack(heard=None, ended=True)
    assert engine._elapsed() == pytest.approx(5.0, abs=0.3)


def test_the_picture_holds_until_the_sound_starts(engine):
    """Otherwise it runs out ahead in the first second and never catches
    back up - the sound cannot be rewound."""
    engine.audio_track = FakeTrack(heard=None)
    assert engine._elapsed() == 0.0


def test_the_wall_clock_is_kept_level_with_the_sound(engine):
    """So that when the sound does stop, the picture carries on from
    where it was rather than jumping."""
    engine.audio_track = FakeTrack(heard=12.5)
    engine._elapsed()
    engine.audio_track = FakeTrack(failed=True)
    assert engine._elapsed() == pytest.approx(2.5, abs=0.05)


def test_the_sound_never_makes_time_run_backwards(engine):
    engine.audio_track = FakeTrack(heard=9.0)     # behind the start point
    assert engine._elapsed() == 0.0


def test_speed_scales_the_wall_clock_fallback(engine):
    engine.audio_track = None
    engine.speed = 2.0
    assert engine._elapsed() == pytest.approx(10.0, abs=0.6)


# ── volume without breaking sync ────────────────────────────────────────

def test_muting_does_not_stop_the_clock(engine):
    """Stopping the sound to mute it would stop the picture with it."""
    track = FakeTrack(heard=12.0)
    engine.audio_track = track
    engine.muted = True
    engine._apply_gain()
    assert engine.audio_track is track      # still running
    assert track.gain == 0.0


def test_volume_is_a_gain_not_a_restart(engine):
    track = FakeTrack(heard=12.0)
    engine.audio_track = track
    engine.volume = 40
    engine._apply_gain()
    assert engine.audio_track is track
    assert track.gain == pytest.approx(0.4)


def test_turning_the_volume_up_unmutes(engine):
    engine.audio_track = FakeTrack(heard=12.0)
    engine.muted = True
    engine.set_volume(60)
    assert engine.muted is False


# ── the tempo filter ────────────────────────────────────────────────────

def test_atempo_passes_through_what_it_can_do_alone():
    assert audio_out._atempo(1.5) == "atempo=1.5000"


def test_atempo_chains_past_its_own_limits():
    assert audio_out._atempo(4.0) == "atempo=2.0,atempo=2.0000"
    assert audio_out._atempo(0.25) == "atempo=0.5,atempo=0.5000"


# ── against real ffmpeg ─────────────────────────────────────────────────

@pytest.mark.skipif(not audio_out.available(),
                    reason="sounddevice not installed")
def test_a_clip_with_no_audio_gives_up_at_once(tmp_path):
    """Half the converted library is silent. Waiting out the start
    timeout on each one would freeze the first frame for two seconds
    every time a clip is played."""
    path = str(tmp_path / "silent.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=24:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path],
        check=True)
    track = audio_out.AudioTrack(path, 0.0)
    deadline = time.monotonic() + track.START_TIMEOUT
    while time.monotonic() < deadline and not track.failed:
        time.sleep(0.02)
    waited = time.monotonic() - (deadline - track.START_TIMEOUT)
    track.stop()
    assert track.failed, "never noticed the clip has no sound"
    assert waited < track.START_TIMEOUT * 0.9, (
        f"took {waited:.2f}s to notice - that is a stall on every silent clip")


def test_stopping_returns_immediately(tmp_path):
    """stop() is called from the UI thread on every seek and every change
    of clip."""
    track = audio_out.AudioTrack(str(tmp_path / "nothing.mp4"), 0.0)
    start = time.monotonic()
    track.stop()
    assert time.monotonic() - start < 0.1


# ── frame stepping ──────────────────────────────────────────────────────

class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def _stepper(monkeypatch):
    """A paused engine with a live decoder and frames waiting, which is
    exactly the state pausing leaves behind."""
    import queue as _queue
    p = ClipPlayer.__new__(ClipPlayer)
    p.path = "/clips/a.mp4"
    p.playing = False
    p.duration = 30.0
    p.position = 4.0
    p.stream_fps = 25.0
    p.proc = FakeProc()
    p._queue = _queue.Queue()
    p.on_tick = None
    p.blitted = []
    p.seeks = []
    p._blit = p.blitted.append
    p.seek = p.seeks.append
    return p


def test_stepping_forward_takes_the_frame_already_decoded(monkeypatch):
    p = _stepper(monkeypatch)
    p._queue.put(b"next-frame")
    p.step(1)
    assert p.blitted == [b"next-frame"], "should have come off the queue"
    assert p.seeks == [], "no respawn needed to go forward one frame"
    assert p.position == pytest.approx(4.04)


def test_stepping_backwards_has_to_seek(monkeypatch):
    p = _stepper(monkeypatch)
    p._queue.put(b"next-frame")
    p.step(-1)
    assert p.seeks == [pytest.approx(3.96)]
    assert p.blitted == [], "a pipe cannot be rewound"


def test_stepping_seeks_when_nothing_is_decoded_yet(monkeypatch):
    p = _stepper(monkeypatch)          # empty queue
    p.step(1)
    assert p.seeks == [pytest.approx(4.04)]


def test_stepping_seeks_when_the_decoder_has_gone(monkeypatch):
    p = _stepper(monkeypatch)
    p.proc = FakeProc(alive=False)
    p._queue.put(b"stale")
    p.step(1)
    assert p.seeks == [pytest.approx(4.04)]


def test_the_end_of_the_clip_is_not_stepped_into(monkeypatch):
    p = _stepper(monkeypatch)
    p._queue.put(None)                 # the decoder's end marker
    p.step(1)
    assert p.blitted == []
    assert p.seeks == [pytest.approx(4.04)]


def test_stepping_while_playing_seeks(monkeypatch):
    """Taking a frame off the queue under the pacing loop would steal it."""
    p = _stepper(monkeypatch)
    p.playing = True
    p._queue.put(b"next-frame")
    p.step(1)
    assert p.seeks == [pytest.approx(4.04)]
    assert p.blitted == []


def test_stepping_stays_inside_the_clip(monkeypatch):
    p = _stepper(monkeypatch)
    p.position = 29.99
    p._queue.put(b"last")
    p.step(1)
    assert p.position == pytest.approx(30.0)


def test_a_step_of_nothing_does_nothing(monkeypatch):
    p = _stepper(monkeypatch)
    p.step(0)
    assert p.seeks == [] and p.blitted == []
