"""Fitting a frame into a tile, and not doing the invisible half of it.

"contain" fills the space around a clip with a blurred, zoomed copy of
the clip itself, so a portrait or ultrawide clip lands intact instead of
in dead black. That fill is half of what composing a tile costs - and for
the common case it is entirely hidden, because the cached thumbnail is
320x180 and the card is 331x186, so a widescreen clip covers the card
with a one-pixel edge rather than a letterbox.

So the fill is skipped when the picture covers the box. These tests are
the two halves of that: it must still be there when it would show, and
gone when it would not.
"""

from __future__ import annotations

import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import media                                  # noqa: E402

CARD = (331, 186)          # a gallery card at 150% scaling


def noisy(width: int, height: int):
    """A picture with different colours in different places, so a blurred
    copy of it is not a flat colour."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7) % 256, (y * 11) % 256,
                            ((x + y) * 5) % 256)
    return image


def margin_colours(image, box_w: int, box_h: int, inner_w: int) -> set:
    """The distinct colours down the left-hand margin beside the picture."""
    edge = max((box_w - inner_w) // 2 - 1, 0)
    return {image.getpixel((min(edge, box_w - 1), y))
            for y in range(0, box_h, 7)}


# ── the fill is still there when it would show ─────────────────────────

def test_a_portrait_clip_gets_a_blurred_backdrop():
    """The whole point of "contain": a vertical clip in a wide tile is
    surrounded by a soft version of itself, not dead black."""
    out = media.fit_frame(noisy(200, 400), *CARD, "contain")
    assert out.size == CARD
    inner_w = int(200 * min(CARD[0] / 200, CARD[1] / 400))
    colours = margin_colours(out, *CARD, inner_w)
    assert len(colours) > 1, "the margin is flat - the backdrop is missing"


def test_an_ultrawide_clip_gets_one_too():
    out = media.fit_frame(noisy(1000, 200), *CARD, "contain")
    assert out.size == CARD
    # Sample a horizontal band above the picture rather than beside it.
    inner_h = int(200 * min(CARD[0] / 1000, CARD[1] / 200))
    band = max((CARD[1] - inner_h) // 2 - 1, 0)
    colours = {out.getpixel((x, min(band, CARD[1] - 1)))
               for x in range(0, CARD[0], 9)}
    assert len(colours) > 1, "the margin is flat - the backdrop is missing"


def test_a_square_clip_gets_one_too():
    out = media.fit_frame(noisy(300, 300), *CARD, "contain")
    inner_w = int(300 * min(CARD[0] / 300, CARD[1] / 300))
    assert len(margin_colours(out, *CARD, inner_w)) > 1


# ── and gone when it would not ─────────────────────────────────────────

def test_a_widescreen_clip_skips_the_fill():
    """320x180 into a 331x186 card: the picture covers it bar a pixel, so
    a blurred copy behind it is work nobody can see."""
    out = media.fit_frame(noisy(320, 180), *CARD, "contain")
    assert out.size == CARD
    # 320 scales to 330 in a 331-wide card, centred at offset 0, so the
    # one spare pixel is the last column - and it is a single flat
    # colour rather than a blurred picture.
    edge = {out.getpixel((CARD[0] - 1, y)) for y in range(0, CARD[1], 7)}
    assert len(edge) == 1, "the skipped fill left something patterned behind"


def test_an_exact_fit_skips_it():
    out = media.fit_frame(noisy(331, 186), *CARD, "contain")
    assert out.size == CARD
    assert out.getpixel((0, 0)) == noisy(331, 186).getpixel((0, 0))


def test_skipping_is_faster_than_not():
    """The reason this exists. Not a tight bound - just that the cheap
    path is actually cheaper, so a refactor cannot quietly undo it."""
    import time
    wide = noisy(320, 180)
    tall = noisy(200, 400)
    for _ in range(3):                      # warm PIL up
        media.fit_frame(wide, *CARD, "contain")
        media.fit_frame(tall, *CARD, "contain")

    def clock(image):
        t0 = time.perf_counter()
        for _ in range(6):
            media.fit_frame(image, *CARD, "contain")
        return time.perf_counter() - t0

    assert clock(wide) < clock(tall)


# ── everything else about it still holds ───────────────────────────────

def test_the_result_is_always_exactly_the_box():
    for size in ((320, 180), (200, 400), (1000, 200), (300, 300), (4, 4)):
        assert media.fit_frame(noisy(*size), *CARD, "contain").size == CARD


def test_cover_crops_to_fill_and_is_unaffected():
    out = media.fit_frame(noisy(200, 400), *CARD, "cover")
    assert out.size == CARD


def test_a_degenerate_source_does_not_raise():
    assert media.fit_frame(Image.new("RGB", (1, 1)), *CARD, "contain").size == CARD


def test_a_tiny_box_still_works():
    assert media.fit_frame(noisy(320, 180), 8, 4, "contain").size == (8, 4)


def test_tile_image_end_to_end_is_the_card_size():
    import io
    buf = io.BytesIO()
    noisy(320, 180).save(buf, "JPEG")
    out = media.tile_image(buf.getvalue(), *CARD, "contain", backing="#141018")
    assert out.size == CARD
    assert out.mode == "RGB"
