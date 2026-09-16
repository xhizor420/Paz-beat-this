"""User-supplied pictures, and the crop that makes them fit.

Every slot in the app that can take a picture has a fixed shape to fill -
a wide thin header, a 16:9 panel, a square icon - and almost no picture
anyone picks will be that shape. So a slot stores where the picture is
plus *how to crop it*, and the crop is recomputed whenever the slot is
drawn. Two consequences worth having: the original file is never touched
or copied, and a slot that changes size (the header follows the window)
re-crops correctly instead of stretching.

The crop is a focal point and a zoom rather than a pixel rectangle, for
the same reason - a rectangle measured against one target size is wrong
at any other.

  zoom   1.0 covers the slot exactly, the largest the picture can be
         drawn while still filling it. Above 1.0 pulls in closer.
  fx,fy  which point of the picture ends up in the middle of the slot,
         0..1 across and down. 0.5,0.5 is dead centre.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PIL import Image

# What each slot is for, and the shape it has to fill. `size` is the
# nominal pixel size at 100% scaling - the header's width is really the
# window's, so its entry is the shape rather than a promise.
@dataclass(frozen=True)
class Slot:
    key: str            # config key prefix
    title: str
    size: tuple         # (w, h) the picture is cropped to
    flexible_width: bool = False
    note: str = ""
    # A backdrop sits under the whole app, so it has to be softened until
    # it cannot compete with anything on top of it. Slots that are the
    # content - a project cover - are shown as they are.
    treatments: bool = False

    @property
    def aspect(self) -> float:
        return self.size[0] / self.size[1]

    def wanted(self) -> str:
        """What to tell the user they should hand over."""
        w, h = self.size
        if self.flexible_width:
            return f"{w} × {h} or wider — anything wide and short suits it"
        return f"{w} × {h}"


# Only two slots, deliberately. There was a third - a backdrop behind the
# player - and it was a bad idea: it is on screen for the couple of
# seconds before a clip is picked and then a thumbnail covers it for the
# rest of the session. The same is true of every other empty-looking
# panel here (the Convert inspector, the Beat This panels, the
# no-results gallery): they look empty because nothing is selected yet,
# which is precisely when nobody is looking at them. This app is full of
# content already, so a picture only earns a place if it is either always
# on screen or is itself an asset.
SLOTS = (
    Slot("banner", "Header strip", (1760, 58), flexible_width=True,
         note="Runs the full width behind the PAZ mark, on every tab, all "
              "the time. A wide crop suits it better than a whole picture."),
    Slot("icon", "Window icon", (256, 256),
         note="The taskbar, the window, and alt-tab. Square."),
    Slot("wallpaper", "Backdrop", (1920, 1080), treatments=True,
         note="Behind the clip grid, under everything. Blur and dim it "
              "until it reads as texture - it is the ground the app sits "
              "on, not something to look at."),
)

# Not in SLOTS: a project's cover is a picture per project rather than one
# for the app, and it lives with the project in the database instead of in
# the config. The shape is a video thumbnail's, because that is what it is
# for - the still you upload the finished PMV with.
PROJECT_COVER = Slot("cover", "Project cover", (1280, 720),
                     note="Kept with the project. The still for the finished "
                          "video, so it is here when you need it rather than "
                          "in a folder somewhere.")

SLOTS_BY_KEY = {slot.key: slot for slot in SLOTS}

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
ZOOM_MIN, ZOOM_MAX = 1.0, 4.0


def is_image(path: str) -> bool:
    return bool(path) and os.path.splitext(path)[1].lower() in IMAGE_TYPES


def measure(path: str):
    """(width, height) of a picture without decoding all of it, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with Image.open(path) as source:
            return source.size
    except Exception:
        return None


def describe(path: str, slot: Slot) -> str:
    """One line about the picked file: what it is, and what will happen to
    it. This is the whole point of showing a resolution - not trivia, but
    telling someone in advance that their 4000px square is about to lose
    most of its height."""
    size = measure(path)
    if size is None:
        return "Not a picture this can read."
    width, height = size
    target = slot.size[1] if slot.flexible_width else None
    bits = [f"{width} × {height}"]
    if slot.flexible_width:
        bits.append(f"cropped to a {slot.size[0]} × {target} strip"
                    if height > target * 1.2 else "about the right shape")
    else:
        want = slot.aspect
        have = width / max(height, 1)
        if abs(have - want) < 0.02:
            bits.append("already the right shape")
        elif have > want:
            bits.append("wider than the slot — the sides get cropped")
        else:
            bits.append("taller than the slot — the top and bottom get cropped")
    if width < slot.size[0] // 2 or height < slot.size[1] // 2:
        bits.append("small, so it will look soft")
    return " · ".join(bits)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))


def crop_box(source: tuple, target: tuple, zoom: float,
             fx: float, fy: float) -> tuple:
    """The rectangle to take out of `source` to fill `target`.

    Returns (left, top, right, bottom) in source pixels, always inside the
    picture and always the target's aspect ratio. A focal point that would
    push the box off an edge slides back in rather than clipping, so the
    result is never letterboxed by accident.
    """
    sw, sh = max(int(source[0]), 1), max(int(source[1]), 1)
    tw, th = max(int(target[0]), 1), max(int(target[1]), 1)
    zoom = clamp(zoom, ZOOM_MIN, ZOOM_MAX)

    # The largest box of the target's shape that fits in the picture, then
    # shrunk by the zoom - a smaller box is a closer crop.
    scale = min(sw / tw, sh / th) / zoom
    box_w = min(max(tw * scale, 1.0), float(sw))
    box_h = min(max(th * scale, 1.0), float(sh))

    # Round to whole pixels along the long edge and derive the short one
    # from it, so the box keeps the slot's shape. Rounding both
    # independently costs up to a pixel each, which on the header's 23:1
    # strip is twenty-odd pixels of width - a visible stretch once it is
    # scaled into the slot.
    want = tw / th
    if box_w >= box_h:
        bw = max(int(round(box_w)), 1)
        bh = max(int(round(bw / want)), 1)
        if bh > sh:
            bh = sh
            bw = min(max(int(round(bh * want)), 1), sw)
    else:
        bh = max(int(round(box_h)), 1)
        bw = max(int(round(bh * want)), 1)
        if bw > sw:
            bw = sw
            bh = min(max(int(round(bw / want)), 1), sh)
    bw, bh = min(bw, sw), min(bh, sh)

    left = clamp(fx, 0.0, 1.0) * sw - bw / 2
    top = clamp(fy, 0.0, 1.0) * sh - bh / 2
    left = int(round(clamp(left, 0.0, sw - bw)))
    top = int(round(clamp(top, 0.0, sh - bh)))
    return (left, top, left + bw, top + bh)


def render(path: str, target: tuple, zoom: float = 1.0,
           fx: float = 0.5, fy: float = 0.5) -> "Image.Image | None":
    """The picture, cropped and scaled to exactly `target`. None if it
    cannot be read."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with Image.open(path) as source:
            picture = source.convert("RGB")
    except Exception:
        return None
    box = crop_box(picture.size, target, zoom, fx, fy)
    cropped = picture.crop(box)
    tw, th = max(int(target[0]), 1), max(int(target[1]), 1)
    if cropped.size != (tw, th):
        cropped = cropped.resize((tw, th), Image.LANCZOS)
    return cropped


# ── how a slot's settings live in the config ────────────────────────────

# How far the backdrop treatments can go. The blur is a radius in pixels
# at 1920 wide; the dim is how much of the ground colour is laid over it.
BLUR_MAX = 40.0
DIM_MAX = 0.95
DIM_DEFAULT = 0.72
BLUR_DEFAULT = 14.0


def treat(picture, blur: float, dim: float, ground: str = "#07040C"):
    """Soften a backdrop until it cannot fight the app on top of it.

    Blur first, then flatten toward the ground colour. Both matter: blur
    alone leaves a bright picture bright, and dimming alone leaves edges
    sharp enough to read as content rather than texture.
    """
    from PIL import Image, ImageFilter
    blur = max(0.0, min(float(blur), BLUR_MAX))
    dim = max(0.0, min(float(dim), DIM_MAX))
    if blur > 0.05:
        picture = picture.filter(ImageFilter.GaussianBlur(radius=blur))
    if dim > 0.001:
        flat = Image.new("RGB", picture.size, ground)
        picture = Image.blend(picture, flat, dim)
    return picture


def slot_keys(key: str) -> tuple:
    return (f"art_{key}_path", f"art_{key}_zoom",
            f"art_{key}_fx", f"art_{key}_fy")


def treatment_keys(key: str) -> tuple:
    return (f"art_{key}_blur", f"art_{key}_dim")


def read_treatments(cfg, key: str) -> dict:
    blur_k, dim_k = treatment_keys(key)
    return {
        "blur": clamp(getattr(cfg, blur_k, BLUR_DEFAULT), 0.0, BLUR_MAX),
        "dim": clamp(getattr(cfg, dim_k, DIM_DEFAULT), 0.0, DIM_MAX),
    }


def write_treatments(cfg, key: str, blur: float, dim: float) -> None:
    blur_k, dim_k = treatment_keys(key)
    setattr(cfg, blur_k, round(clamp(blur, 0.0, BLUR_MAX), 3))
    setattr(cfg, dim_k, round(clamp(dim, 0.0, DIM_MAX), 4))


def render_backdrop(cfg, key: str, target: tuple, ground: str = "#07040C"):
    """A slot rendered and then softened, for the slots that are backdrops."""
    picture = render_slot(cfg, key, target)
    if picture is None:
        return None
    treats = read_treatments(cfg, key)
    return treat(picture, treats["blur"], treats["dim"], ground)


def read_slot(cfg, key: str) -> dict:
    path_k, zoom_k, fx_k, fy_k = slot_keys(key)
    return {
        "path": str(getattr(cfg, path_k, "") or ""),
        "zoom": clamp(getattr(cfg, zoom_k, 1.0) or 1.0, ZOOM_MIN, ZOOM_MAX),
        "fx": clamp(getattr(cfg, fx_k, 0.5), 0.0, 1.0),
        "fy": clamp(getattr(cfg, fy_k, 0.5), 0.0, 1.0),
    }


def write_slot(cfg, key: str, path: str, zoom: float, fx: float, fy: float) -> None:
    path_k, zoom_k, fx_k, fy_k = slot_keys(key)
    setattr(cfg, path_k, path or "")
    setattr(cfg, zoom_k, round(clamp(zoom, ZOOM_MIN, ZOOM_MAX), 4))
    setattr(cfg, fx_k, round(clamp(fx, 0.0, 1.0), 4))
    setattr(cfg, fy_k, round(clamp(fy, 0.0, 1.0), 4))


def render_slot(cfg, key: str, target: tuple) -> "Image.Image | None":
    slot = read_slot(cfg, key)
    if not slot["path"]:
        return None
    return render(slot["path"], target, slot["zoom"], slot["fx"], slot["fy"])
