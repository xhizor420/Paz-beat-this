"""A hand-set column width has to be trustworthy before it outranks
everything else.

The handle used to decide the width from where the pointer WAS rather
than how far it had moved, so a click on it saved a width - including
the first press of the double-click that resets it. Anyone who touched
it is pinned to an arbitrary number, and because a hand-set width beats
every other rule, all the work on how the column sizes itself would look
like it had not happened.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.config import AppConfig      # noqa: E402


def test_a_width_from_before_the_fix_is_forgotten_once():
    cfg = AppConfig()
    cfg.panel_width_px = 1002
    cfg.tags_height_px = 90
    assert cfg._upgrade_layout_sizes() is True
    assert cfg.panel_width_px == 0
    assert cfg.tags_height_px == 0
    assert cfg.layout_gen == AppConfig.LAYOUT_GEN


def test_a_width_set_after_the_fix_is_left_alone():
    cfg = AppConfig()
    cfg.layout_gen = AppConfig.LAYOUT_GEN
    cfg.panel_width_px = 1002
    assert cfg._upgrade_layout_sizes() is False
    assert cfg.panel_width_px == 1002


def test_the_reset_happens_once_and_not_again():
    cfg = AppConfig()
    cfg._upgrade_layout_sizes()
    cfg.panel_width_px = 880          # dragged deliberately, after the fix
    assert cfg._upgrade_layout_sizes() is False
    assert cfg.panel_width_px == 880


def test_the_marker_is_written_even_when_there_was_nothing_to_forget():
    cfg = AppConfig()
    assert cfg._upgrade_layout_sizes() is True
    assert cfg.layout_gen == AppConfig.LAYOUT_GEN


def test_the_marker_survives_a_round_trip_through_the_file(tmp_path):
    path = tmp_path / "paz_config.json"
    cfg = AppConfig()
    cfg._upgrade_layout_sizes()
    path.write_text(json.dumps({"layout_gen": cfg.layout_gen,
                                "panel_width_px": 700}))
    fresh = AppConfig()
    fresh._read(str(path))
    assert fresh._upgrade_layout_sizes() is False
    assert fresh.panel_width_px == 700
