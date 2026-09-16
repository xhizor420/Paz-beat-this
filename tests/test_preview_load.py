"""Preview work must not take the machine away from the user.

Thumbnails, single frames, storyboard sheets and hover reels are all
ffmpeg, all optional, and all fighting one disk. Unbounded, a page turn
plus a hover plus a sheet build is a dozen children at once - slower for
everyone, and it lands while the user is upscaling or converting in
another program, which is when the window stops answering.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import files, media      # noqa: E402


def test_preview_work_is_bounded():
    assert isinstance(media.PREVIEW_SLOTS, type(threading.Semaphore()))
    # Two at the very least, and never one per core - the cores are what
    # the other program is using.
    cap = media.PREVIEW_SLOTS._value
    assert cap >= 2
    assert cap <= max(2, (os.cpu_count() or 4) // 2)


def test_preview_children_yield_to_the_users_own_work():
    """On Windows they run below normal. The encoder does not - that one
    IS the job."""
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        assert files.PREVIEW_FLAGS & subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert files.PREVIEW_FLAGS & files.NO_WINDOW == files.NO_WINDOW


def test_the_bound_is_actually_taken():
    """A regression guard: the semaphore is only worth anything if the
    preview paths hold it."""
    source = open(os.path.join(os.path.dirname(media.__file__),
                               "media.py"), encoding="utf-8").read()
    assert source.count("with PREVIEW_SLOTS:") >= 4
    # And nothing in the preview paths may quietly go back to the
    # plain no-window flags.
    assert source.count("creationflags=PREVIEW_FLAGS") >= 4


def test_a_slot_is_released_even_when_ffmpeg_fails():
    before = media.PREVIEW_SLOTS._value
    try:
        with media.PREVIEW_SLOTS:
            raise RuntimeError("ffmpeg fell over")
    except RuntimeError:
        pass
    assert media.PREVIEW_SLOTS._value == before
