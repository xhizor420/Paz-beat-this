"""Cropping a user's picture into a slot.

The crop is stored as a focal point and a zoom rather than a pixel
rectangle, so that a slot which changes size - the header follows the
window - re-crops correctly instead of stretching. That only works if the
box is always inside the picture and always the slot's shape, whatever it
is handed, so that is what these check.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import artwork                              # noqa: E402


def shape(box):
    return (box[2] - box[0], box[3] - box[1])


def aspect(box):
    w, h = shape(box)
    return w / h


# ── the box is always usable ────────────────────────────────────────────

@pytest.mark.parametrize("source", [
    (4000, 3000), (1920, 1080), (600, 4000), (4000, 600), (1, 1),
    (1000, 1000), (37, 4001),
])
@pytest.mark.parametrize("target", [(1760, 76), (1280, 720), (256, 256)])
def test_the_box_stays_inside_the_picture(source, target):
    for zoom in (1.0, 1.7, 4.0):
        for fx, fy in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (0.2, 0.9)):
            box = artwork.crop_box(source, target, zoom, fx, fy)
            assert 0 <= box[0] < box[2] <= source[0], (source, target, box)
            assert 0 <= box[1] < box[3] <= source[1], (source, target, box)


@pytest.mark.parametrize("source", [(4000, 3000), (600, 4000), (1920, 1080)])
@pytest.mark.parametrize("target", [(1760, 76), (1280, 720), (256, 256)])
def test_the_box_has_the_slot_shape(source, target):
    box = artwork.crop_box(source, target, 1.0, 0.5, 0.5)
    want = target[0] / target[1]
    w, h = shape(box)
    # The tolerance comes from the geometry rather than a round number.
    # A box is whole pixels, so the short edge can only be right to within
    # half a pixel - and on the header's 23:1 strip half a pixel of height
    # is eleven pixels of width. Anything inside that is as close as
    # integer cropping can get; anything outside it is a real mistake.
    allowed = want / 2 + 1
    assert abs(w - h * want) <= allowed, (source, target, (w, h), want)


def test_zoom_one_takes_as_much_as_it_can():
    """The default has to be the widest possible crop, or picking a
    picture and touching nothing would already have thrown detail away."""
    box = artwork.crop_box((2000, 1000), (1000, 1000), 1.0, 0.5, 0.5)
    assert shape(box) == (1000, 1000), "should use the full height"


def test_zooming_in_takes_less():
    wide = artwork.crop_box((2000, 1000), (100, 100), 1.0, 0.5, 0.5)
    close = artwork.crop_box((2000, 1000), (100, 100), 2.0, 0.5, 0.5)
    assert shape(close)[0] < shape(wide)[0]


def test_zoom_is_clamped_to_the_allowed_range():
    silly = artwork.crop_box((2000, 1000), (100, 100), 99.0, 0.5, 0.5)
    most = artwork.crop_box((2000, 1000), (100, 100), artwork.ZOOM_MAX, 0.5, 0.5)
    assert shape(silly) == shape(most)
    under = artwork.crop_box((2000, 1000), (100, 100), 0.01, 0.5, 0.5)
    full = artwork.crop_box((2000, 1000), (100, 100), 1.0, 0.5, 0.5)
    assert shape(under) == shape(full)


# ── the focal point ─────────────────────────────────────────────────────

def test_the_focal_point_moves_the_crop():
    left = artwork.crop_box((4000, 1000), (100, 100), 1.0, 0.0, 0.5)
    right = artwork.crop_box((4000, 1000), (100, 100), 1.0, 1.0, 0.5)
    assert left[0] == 0
    assert right[2] == 4000
    assert left[0] < right[0]


def test_a_focal_point_at_the_edge_slides_back_in():
    """Otherwise the crop hangs off the side and comes back letterboxed."""
    box = artwork.crop_box((4000, 1000), (100, 100), 1.0, 0.0, 0.0)
    assert shape(box) == (1000, 1000)
    assert box[0] == 0 and box[1] == 0


def test_a_focal_point_outside_the_picture_is_clamped():
    inside = artwork.crop_box((2000, 2000), (100, 100), 2.0, 1.0, 1.0)
    beyond = artwork.crop_box((2000, 2000), (100, 100), 2.0, 9.0, 9.0)
    assert inside == beyond


def test_the_middle_is_the_middle():
    box = artwork.crop_box((2000, 1000), (100, 100), 1.0, 0.5, 0.5)
    assert box[0] == 500 and box[2] == 1500


# ── rendering ───────────────────────────────────────────────────────────

@pytest.fixture
def picture(tmp_path):
    from PIL import Image
    path = str(tmp_path / "source.png")
    Image.new("RGB", (1200, 800), (200, 40, 120)).save(path)
    return path


def test_render_gives_back_exactly_the_slot_size(picture):
    for target in ((1760, 76), (854, 480), (256, 256), (13, 400)):
        out = artwork.render(picture, target)
        assert out is not None
        assert out.size == target


def test_render_of_a_missing_file_is_none(tmp_path):
    assert artwork.render(str(tmp_path / "nope.png"), (100, 100)) is None
    assert artwork.render("", (100, 100)) is None


def test_render_of_something_that_is_not_a_picture_is_none(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not a picture", encoding="utf-8")
    assert artwork.render(str(path), (100, 100)) is None


# ── what the user is told ───────────────────────────────────────────────

def test_the_resolution_is_reported(picture):
    slot = artwork.PROJECT_COVER
    assert "1200 × 800" in artwork.describe(picture, slot)


def test_a_wider_picture_says_the_sides_go(tmp_path):
    from PIL import Image
    path = str(tmp_path / "wide.png")
    Image.new("RGB", (4000, 500), (0, 0, 0)).save(path)
    assert "sides" in artwork.describe(path, artwork.PROJECT_COVER)


def test_a_taller_picture_says_the_top_and_bottom_go(tmp_path):
    from PIL import Image
    path = str(tmp_path / "tall.png")
    Image.new("RGB", (500, 4000), (0, 0, 0)).save(path)
    said = artwork.describe(path, artwork.PROJECT_COVER)
    assert "top and bottom" in said


def test_a_small_picture_is_called_out(tmp_path):
    from PIL import Image
    path = str(tmp_path / "tiny.png")
    Image.new("RGB", (60, 40), (0, 0, 0)).save(path)
    assert "soft" in artwork.describe(path, artwork.PROJECT_COVER)


def test_the_right_shape_is_recognised(tmp_path):
    from PIL import Image
    path = str(tmp_path / "fits.png")
    Image.new("RGB", (1708, 960), (0, 0, 0)).save(path)     # 16:9
    assert "right shape" in artwork.describe(path, artwork.PROJECT_COVER)


def test_describe_survives_a_missing_file(tmp_path):
    assert "Not a picture" in artwork.describe(str(tmp_path / "x.png"),
                                               artwork.PROJECT_COVER)


def test_every_slot_states_a_size():
    for slot in artwork.SLOTS + (artwork.PROJECT_COVER,):
        assert slot.wanted()
        assert slot.size[0] > 0 and slot.size[1] > 0


# ── config round trip ───────────────────────────────────────────────────

def test_a_slot_round_trips_through_the_config(tmp_path, monkeypatch):
    from paz_suite import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(tmp_path))
    cfg = cfg_mod.AppConfig()
    artwork.write_slot(cfg, "banner", "/pics/a.png", 1.75, 0.25, 0.8)
    cfg.save()
    back = artwork.read_slot(cfg_mod.AppConfig.load(), "banner")
    assert back == {"path": "/pics/a.png", "zoom": 1.75, "fx": 0.25, "fy": 0.8}


def test_reading_a_slot_that_was_never_set():
    class Bare:
        pass
    assert artwork.read_slot(Bare(), "banner") == {
        "path": "", "zoom": 1.0, "fx": 0.5, "fy": 0.5}


def test_written_values_are_clamped(tmp_path, monkeypatch):
    from paz_suite import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(tmp_path))
    cfg = cfg_mod.AppConfig()
    artwork.write_slot(cfg, "banner", "/pics/a.png", 99.0, -3.0, 4.0)
    got = artwork.read_slot(cfg, "banner")
    assert got["zoom"] == artwork.ZOOM_MAX
    assert got["fx"] == 0.0 and got["fy"] == 1.0


# ── backdrop softening ──────────────────────────────────────────────────

def _flat(size, colour=(220, 40, 160)):
    from PIL import Image
    return Image.new("RGB", size, colour)


def test_dimming_moves_the_picture_toward_the_ground():
    from PIL import Image
    bright = _flat((40, 40), (255, 255, 255))
    out = artwork.treat(bright, blur=0, dim=0.5, ground="#000000")
    assert out.getpixel((20, 20))[0] == pytest.approx(127, abs=2)
    darker = artwork.treat(bright, blur=0, dim=0.9, ground="#000000")
    assert darker.getpixel((20, 20))[0] < out.getpixel((20, 20))[0]
    assert isinstance(out, Image.Image)


def test_no_softening_leaves_it_alone():
    original = _flat((20, 20), (10, 20, 30))
    out = artwork.treat(original, blur=0, dim=0)
    assert out.getpixel((10, 10)) == (10, 20, 30)


def test_blur_softens_an_edge():
    from PIL import Image
    sharp = Image.new("RGB", (60, 20), (0, 0, 0))
    for x in range(30, 60):
        for y in range(20):
            sharp.putpixel((x, y), (255, 255, 255))
    out = artwork.treat(sharp, blur=6, dim=0)
    # Right at the edge the two sides should have moved toward each other.
    assert 20 < out.getpixel((29, 10))[0] < 235
    assert 20 < out.getpixel((30, 10))[0] < 235


def test_the_treatments_are_clamped():
    original = _flat((20, 20), (255, 255, 255))
    a = artwork.treat(original, blur=999, dim=9)
    b = artwork.treat(original, blur=artwork.BLUR_MAX, dim=artwork.DIM_MAX)
    assert a.getpixel((10, 10)) == b.getpixel((10, 10))
    # dim is capped below 1.0, so the picture never vanishes completely
    assert artwork.DIM_MAX < 1.0


def test_a_backdrop_defaults_to_being_hard_to_read():
    """A backdrop that is legible is a backdrop that is in the way."""
    assert artwork.DIM_DEFAULT >= 0.5
    assert artwork.BLUR_DEFAULT >= 5


def test_only_the_backdrop_slot_offers_softening():
    treated = [slot.key for slot in artwork.SLOTS if slot.treatments]
    assert treated == ["wallpaper"]
    assert not artwork.PROJECT_COVER.treatments


def test_treatments_round_trip_through_the_config(tmp_path, monkeypatch):
    from paz_suite import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(tmp_path))
    cfg = cfg_mod.AppConfig()
    artwork.write_treatments(cfg, "wallpaper", 22.0, 0.4)
    cfg.save()
    back = artwork.read_treatments(cfg_mod.AppConfig.load(), "wallpaper")
    assert back == {"blur": 22.0, "dim": 0.4}


def test_a_backdrop_with_no_picture_is_none():
    class Bare:
        art_wallpaper_path = ""
    assert artwork.render_backdrop(Bare(), "wallpaper", (100, 100)) is None
