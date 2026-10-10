"""Gallery thumbnails have to be sharp on the screen they are shown on.

A 4K display at 150% draws cards around 500px wide. A thumbnail saved at
the old 240px default was enlarged onto that card, which is the soft
wall. The stored width moves to 720 once, and a width someone actually
chose is left alone.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from paz_suite.config import AppConfig
from paz_suite import media


def test_old_default_widths_move_to_720_once():
    cfg = AppConfig()
    cfg.thumb_width = 240
    assert cfg._upgrade_thumb_width() is True
    assert cfg.thumb_width == 720
    cfg.thumb_width = 480
    assert cfg._upgrade_thumb_width() is False
    assert cfg.thumb_width == 480


def test_a_chosen_width_is_kept():
    cfg = AppConfig()
    cfg.thumb_width = 640
    assert cfg._upgrade_thumb_width() is True
    assert cfg.thumb_width == 640


def test_scale_filter_is_lanczos_and_still_sharpen_is_optional():
    assert media._scale_filter(720) == "scale=720:-2:flags=lanczos"
    assert "unsharp=5:5:0.35" in media._scale_filter(720, sharpen=True)


def test_thumb_width_is_read_from_the_header(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "THUMB_DIR", str(tmp_path))
    path = r"C:\clips\example.mp4"
    Image.new("RGB", (720, 405), (12, 8, 20)).save(
        tmp_path / media.thumb_key(path), "JPEG", quality=90)
    assert media.thumb_pixel_width(path) == 720
    assert media.thumb_pixel_width(r"C:\clips\missing.mp4") == 0
