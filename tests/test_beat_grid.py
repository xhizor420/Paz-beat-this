"""The beat grid's bar heights: how loud the song is at each beat."""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import beat_engine as be   # noqa: E402

RATE = 22050


def song(parts):
    """Concatenated sine bursts: [(seconds, amplitude), ...]."""
    chunks = []
    for seconds, amp in parts:
        t = np.arange(int(seconds * RATE)) / RATE
        chunks.append(amp * np.sin(2 * np.pi * 220 * t))
    return np.concatenate(chunks)


def test_a_loud_part_stands_taller_than_a_quiet_one():
    signal = song([(2.0, 0.1), (2.0, 0.9)])
    levels = be.beat_levels(signal, RATE, [1.0, 3.0])
    assert levels[1] > levels[0] * 2


def test_levels_are_between_zero_and_one():
    signal = song([(1.0, 0.2), (1.0, 1.0), (1.0, 0.5)])
    levels = be.beat_levels(signal, RATE, np.linspace(0.1, 2.9, 20))
    assert levels.min() >= 0.0 and levels.max() <= 1.0


def test_one_spike_does_not_flatten_the_rest():
    """Scaled to the loud end of the song, not to its single peak."""
    signal = song([(4.0, 0.5)])
    signal[int(2.0 * RATE):int(2.0 * RATE) + 400] = 1.0
    beats = np.linspace(0.5, 3.5, 30)
    levels = be.beat_levels(signal, RATE, beats)
    assert np.median(levels) > 0.8


def test_beats_off_either_end_do_not_crash():
    signal = song([(1.0, 0.5)])
    levels = be.beat_levels(signal, RATE, [-0.5, 0.0, 0.99, 5.0])
    assert len(levels) == 4


@pytest.mark.parametrize("signal, beats", [(np.zeros(0), [1.0]), (song([(1.0, 0.5)]), [])])
def test_nothing_to_measure_is_all_zero(signal, beats):
    assert list(be.beat_levels(signal, RATE, beats)) == [0.0] * len(beats)


# ── the widget itself ───────────────────────────────────────────────────

def test_the_grid_builds_draws_and_clears():
    """Built for real: an override of CustomTkinter's own _draw once broke
    the whole Beat This tab, and nothing short of building it shows that."""
    import customtkinter as ctk
    from conftest import make_root
    from paz_suite.beat_tab import BeatGrid
    root = make_root(ctk.CTk)
    try:
        picked = []
        grid = BeatGrid(root, on_pick=picked.append)
        grid.pack(fill="both", expand=True)
        root.geometry("800x260")
        root.update()
        beats = np.arange(0.5, 60, 0.5)
        down = (np.arange(len(beats)) % 4) == 0
        grid.show(beats, np.linspace(0, 1, len(beats)), down, 60.0, "Purple", "Red",
                  summary="x")
        root.update()
        assert len(grid.canvas.find_all()) > len(beats)
        grid._click(type("E", (), {"x": grid.canvas.winfo_width() // 2})())
        assert picked and 40 < picked[0] < 80
        grid.clear()
        root.update()
        assert len(grid.canvas.find_all()) == 2      # just the empty state: icon and line
    finally:
        root.destroy()
