"""The player's two hard rules: never block the UI thread, and never let
sound run ahead of picture.

Both were broken in ways that only showed up on real footage - a 4K file
takes long enough to open and long enough to shut down that the costs
below were invisible on a small test clip.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from paz_suite.player_engine import ClipPlayer, _reap    # noqa: E402


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("video") / "clip.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=1280x720:rate=30:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path],
        check=True)
    return path


@pytest.fixture(scope="module")
def root():
    """One Tk root for the whole module.

    Creating and destroying a root per test aborts the interpreter part
    way through the run: PhotoImages from a destroyed interpreter free
    themselves by calling into Tk, and there is no interpreter left to
    call. That is a Tkinter/PIL limitation, not something under test here,
    so the root is made once and reused.
    """
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    window.geometry("640x400")
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def player(root, clip):
    canvas = tk.Canvas(root, width=640, height=360)
    canvas.pack()
    engine = ClipPlayer(canvas, 640, 360)
    engine.muted = True          # no audio device in CI
    engine.load(clip, 6.0, 30.0)
    root.update()
    yield root, engine
    engine.stop()
    engine._photo = None
    engine._canvas_item = None
    for _ in range(3):
        root.update()
        time.sleep(0.02)
    canvas.destroy()
    root.update()


# One second per seek, every seek: terminate() returned at once but
# wait(timeout=1) then sat there until it gave up, on the UI thread. With
# audio running it was two. That is what swallowed clicks on the seek bar.
BUDGET_MS = 150


def test_seeking_does_not_block_the_ui_thread(player):
    root, engine = player
    engine.play()
    root.update()
    time.sleep(0.8)
    for _ in range(5):
        root.update()

    worst = 0.0
    for position in (1.0, 4.0, 2.0, 5.0):
        started = time.monotonic()
        engine.seek(position)
        worst = max(worst, time.monotonic() - started)
        for _ in range(10):
            root.update()
            time.sleep(0.01)

    assert worst * 1000 < BUDGET_MS, f"seek blocked for {worst * 1000:.0f} ms"


def test_stopping_does_not_block_the_ui_thread(player):
    root, engine = player
    engine.play()
    root.update()
    time.sleep(0.5)
    started = time.monotonic()
    engine.stop()
    assert (time.monotonic() - started) * 1000 < BUDGET_MS


def test_the_clock_waits_for_the_first_frame(player):
    """Playback used to start counting the moment play() was called, while
    ffmpeg was still opening the file - so audio, which really had started,
    led the picture by however long the decoder took."""
    root, engine = player
    engine.play()
    assert engine._clock_t0 is None, "clock started before any frame existed"

    deadline = time.monotonic() + 6
    while engine._clock_t0 is None and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)

    assert engine._clock_t0 is not None, "playback never started"
    assert engine._frames_shown >= 1, "clock started without showing a frame"


def test_audio_is_held_back_until_there_is_a_picture(player):
    root, engine = player
    engine.muted = False
    engine._wants_audio = False
    engine.play()
    # Audio is requested, not yet spawned.
    assert engine._wants_audio is True
    assert engine.audio_proc is None
    engine.muted = True


def test_reap_returns_immediately_and_still_collects(clip):
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", clip, "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    _reap(proc)
    assert (time.monotonic() - started) * 1000 < 50

    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, "process was never collected"
