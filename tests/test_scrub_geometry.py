"""The scrubber is drawn in one place and read back in another.

_draw_bar puts the playhead at a position; _track_width and _scrub_to map
a click back to a time. They use the same insets, and if the two ever
disagree the playhead lands somewhere other than where the pointer was -
the kind of wrongness that reads as the player being broken rather than
as a layout bug.

They also have to scale. The bar is a raw tk.Canvas, which CustomTkinter
does not scale, so every size in it was the literal number on screen at
any display scaling: a twenty-pixel bar with a ten-pixel playhead next to
a clock drawn with pt(), which did scale. On a 4K screen at 150% the grab
target was a third the size it was drawn to be.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                   # noqa: E402
from paz_suite.library_player import InlinePlayer             # noqa: E402


class Bar:
    def __init__(self, width):
        self._w = width

    def winfo_width(self):
        return self._w


class Player:
    """Just the parts of InlinePlayer the scrub arithmetic touches."""

    BAR_H = InlinePlayer.BAR_H
    BAR_PAD = InlinePlayer.BAR_PAD
    BAR_TRACK = InlinePlayer.BAR_TRACK
    BAR_HEAD = InlinePlayer.BAR_HEAD
    BAR_CLOCK_GAP = InlinePlayer.BAR_CLOCK_GAP
    _track_width = InlinePlayer._track_width

    def __init__(self, width=900, track_end=None):
        self.bar = Bar(width)
        if track_end is not None:
            self._track_end = track_end

    def head_x(self, frac):
        """Where _draw_bar puts the playhead for this fraction."""
        pad = theme.px(self.BAR_PAD)
        end = getattr(self, "_track_end", None) or self.bar.winfo_width() - pad
        return pad + frac * max(end - 2 * pad, 1)

    def frac_at(self, x):
        """What _scrub_to reads back from a click there."""
        return max(0.0, min((x - theme.px(self.BAR_PAD))
                            / self._track_width(), 1.0))


@pytest.fixture(autouse=True)
def scale_100():
    before = theme.T.SCALE
    theme.T.SCALE = 1.0
    yield
    theme.T.SCALE = before


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("frac", [0.0, 0.1, 0.25, 0.5, 0.75, 1.0])
def test_a_click_on_the_playhead_reads_back_the_same_position(scale, frac):
    theme.T.SCALE = scale
    player = Player(width=900, track_end=764)
    assert player.frac_at(player.head_x(frac)) == pytest.approx(frac, abs=1e-6)


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_a_click_at_the_far_left_is_the_start(scale):
    theme.T.SCALE = scale
    player = Player(width=900, track_end=764)
    assert player.frac_at(0) == 0.0


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_a_click_past_the_end_is_the_end(scale):
    theme.T.SCALE = scale
    player = Player(width=900, track_end=764)
    assert player.frac_at(5000) == 1.0


def test_the_bar_grows_with_the_display():
    """The whole point - at 150% every part of it is half again bigger."""
    theme.T.SCALE = 1.0
    small = (theme.px(InlinePlayer.BAR_H), theme.px(InlinePlayer.BAR_HEAD),
             theme.px(InlinePlayer.BAR_TRACK))
    theme.T.SCALE = 1.5
    big = (theme.px(InlinePlayer.BAR_H), theme.px(InlinePlayer.BAR_HEAD),
           theme.px(InlinePlayer.BAR_TRACK))
    assert all(b > s for b, s in zip(big, small, strict=True))
    assert big[0] == 30, "a 20px bar should be 30 at 150%"


def test_the_playhead_stays_a_reachable_target():
    """Ten pixels across on a 4K screen is not something you can grab."""
    theme.T.SCALE = 1.5
    assert theme.px(InlinePlayer.BAR_HEAD) * 2 >= 14


def test_the_track_never_collapses_on_a_slim_bar():
    theme.T.SCALE = 2.0
    assert Player(width=4, track_end=None)._track_width() >= 1
