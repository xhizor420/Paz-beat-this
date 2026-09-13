"""Clicking the timeline of a playing clip means "watch from there".

Convert's inspector pauses while you drag the scrubber - a scrub and the
decoder painting the same canvas show neither cleanly - but it used to
leave it paused afterwards. So a click to move along a clip you were
watching stopped the clip, and the only way back was to find Play again.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.convert_widgets import ScrubPreview     # noqa: E402


class FakeEngine:
    def __init__(self, playing=False):
        self.playing = playing
        self.paused = 0
        self.sought = []

    def pause(self):
        self.playing = False
        self.paused += 1

    def seek(self, pos):
        self.sought.append(pos)


class Event:
    def __init__(self, x):
        self.x = x


class FakeScrub:
    _on_press = ScrubPreview._on_press
    _on_release = ScrubPreview._on_release

    def __init__(self, playing=False, duration=120.0):
        self.player_engine = FakeEngine(playing)
        self._duration_value = duration
        self._dragging = False
        self._resume_on_release = False
        self._pos = 0.0
        self.frames_requested = []

    def _duration(self):
        return self._duration_value

    def _ghost_hide(self):
        pass

    def _time_at(self, x):
        return x / 10.0

    def _draw_timeline(self):
        pass

    def _request_frame(self, immediate=False):
        self.frames_requested.append(immediate)


def test_scrubbing_a_playing_clip_carries_on_from_where_it_was_dropped():
    scrub = FakeScrub(playing=True)
    scrub._on_press(Event(600))            # 60s in
    assert scrub.player_engine.paused == 1  # quiet while dragging
    scrub._on_release(Event(600))
    assert scrub.player_engine.sought == [60.0]


def test_scrubbing_a_paused_clip_leaves_it_paused():
    scrub = FakeScrub(playing=False)
    scrub._on_press(Event(300))
    scrub._on_release(Event(300))
    assert scrub.player_engine.sought == []
    assert scrub.frames_requested == [False, True]   # drag frame, then final


def test_a_second_scrub_does_not_inherit_the_first_ones_resume():
    scrub = FakeScrub(playing=True)
    scrub._on_press(Event(600))
    scrub._on_release(Event(600))
    scrub._on_press(Event(200))            # now paused
    scrub._on_release(Event(200))
    assert scrub.player_engine.sought == [60.0]


def test_a_release_without_a_drag_does_nothing():
    scrub = FakeScrub(playing=True)
    scrub._on_release(Event(600))
    assert scrub.player_engine.sought == []
    assert scrub.frames_requested == []
