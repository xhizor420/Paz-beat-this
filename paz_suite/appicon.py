"""The app icon: title bar, taskbar, Alt+Tab, and every dialog.

Two things went wrong before this. The icon went in through iconphoto()
as one 32px picture, so the taskbar at 150% scaling stretched it soft.
And CustomTkinter puts its own logo on every window 200ms after it opens
unless iconbitmap() has been called on that window - iconphoto() does
not count - so the title bar of the main window and of every dialog
showed the CustomTkinter logo anyway.

So the mark is drawn at each icon size on its own (16px is not a 256px
picture shrunk - the paw gets heavier and the tile loses its margin so
it stays legible), packed into a real .ico, and handed to every window
through iconbitmap(), which also stops CustomTkinter's swap.
"""

from __future__ import annotations

import hashlib
import os

from PIL import Image, ImageDraw, ImageFilter

from .theme import T, mix

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

_current = {"path": ""}


def _hex(colour: str) -> tuple:
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def render(size: int, top: str = "", bottom: str = "", ink: str = "") -> Image.Image:
    """The mark at `size` px: a paw knocked out of a rounded tile lit with
    a diagonal gradient (Convert's magenta into Library's violet)."""
    top = top or T.ACCENT
    bottom = bottom or T.ACCENT2
    ink = ink or T.BG
    small = size <= 24
    scale = 8
    big = size * scale
    margin = 0 if small else round(big * 0.035)
    radius = big * (0.24 if small else 0.26)

    # Diagonal gradient, top-left to bottom-right.
    a, b = _hex(top), _hex(bottom)
    ramp = Image.new("L", (64, 64))
    ramp.putdata([round((x + y) * 255 / 126) for y in range(64) for x in range(64)])
    ramp = ramp.resize((big, big), Image.BILINEAR)
    fill = Image.composite(Image.new("RGB", (big, big), b),
                           Image.new("RGB", (big, big), a), ramp)

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (margin, margin, big - 1 - margin, big - 1 - margin), radius=radius, fill=255)

    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    tile.paste(fill, (0, 0), mask)

    if not small:
        # A soft light across the top third, so it reads as lit glass
        # rather than a flat sticker.
        gloss = Image.new("L", (big, big), 0)
        ImageDraw.Draw(gloss).ellipse(
            (-big * 0.25, -big * 0.78, big * 1.25, big * 0.42), fill=58)
        gloss = gloss.filter(ImageFilter.GaussianBlur(big * 0.05))
        gloss = Image.composite(gloss, Image.new("L", (big, big), 0), mask)
        white = Image.new("RGBA", (big, big), (255, 255, 255, 0))
        white.putalpha(gloss)
        tile.alpha_composite(white)

    paw = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(paw)
    # Heavier at small sizes: at 16px the toes are two pixels across and
    # the gaps between them close up unless they are pushed apart.
    grow = 1.12 if small else 1.0

    def ellipse(cx: float, cy: float, w: float, h: float) -> None:
        w, h = w * grow, h * grow
        draw.ellipse((int((cx - w / 2) * big), int((cy - h / 2) * big),
                      int((cx + w / 2) * big), int((cy + h / 2) * big)), fill=255)

    ellipse(0.50, 0.675, 0.44, 0.33)         # main pad
    ellipse(0.225, 0.415, 0.15, 0.20)        # toes - the outer pair sits lower
    ellipse(0.405, 0.30, 0.155, 0.205)
    ellipse(0.595, 0.30, 0.155, 0.205)
    ellipse(0.775, 0.415, 0.15, 0.20)
    if not small:
        # The paw sits a little into the glass: a faint shadow under it.
        shadow = paw.filter(ImageFilter.GaussianBlur(big * 0.012))
        dark = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        dark.putalpha(shadow.point(lambda v: v * 0.35))
        offset = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        offset.paste(dark, (0, round(big * 0.012)))
        tile.alpha_composite(offset)
    inked = Image.new("RGBA", (big, big), _hex(ink) + (255,))
    tile.paste(inked, (0, 0), paw)
    return tile.resize((size, size), Image.LANCZOS)


def _icon_dir() -> str:
    from .config import CONFIG_DIR
    os.makedirs(CONFIG_DIR, exist_ok=True)
    return CONFIG_DIR


def build_ico(picture: Image.Image | None = None) -> str:
    """Write the .ico and return its path. `picture` is the user's own
    square from Settings > Artwork, when they set one."""
    if picture is not None:
        picture = picture.convert("RGBA")
        frames = [picture.resize((s, s), Image.LANCZOS) for s in SIZES]
        key = hashlib.sha1(picture.tobytes()).hexdigest()[:10]
    else:
        frames = [render(s) for s in SIZES]
        key = hashlib.sha1(b"".join(f.tobytes() for f in frames)).hexdigest()[:10]
    path = os.path.join(_icon_dir(), f"paz_icon_{key}.ico")
    if not os.path.exists(path):
        frames[-1].save(path, format="ICO", sizes=[(s, s) for s in SIZES],
                        append_images=frames[:-1])
    return path


def apply(window, path: str = "") -> None:
    """Put the icon on one window - and, for the root, make it the default
    for every window opened after it."""
    path = path or _current["path"]
    if not path:
        return
    try:
        if window.winfo_toplevel() is window and not window.master:
            window.iconbitmap(path, default=path)
        else:
            window.iconbitmap(path)
    except Exception:
        pass


def install(root, picture: Image.Image | None = None) -> str:
    """Build the icon, put it on the root, and on every CTkToplevel as it
    opens, in place of CustomTkinter's own logo."""
    import customtkinter as ctk
    _current["path"] = build_ico(picture)
    apply(root)
    ctk.CTkToplevel._windows_set_titlebar_icon = lambda self: apply(self)
    return _current["path"]


def header_mark(size: int, colour: str) -> Image.Image:
    """The same mark for the header, in one tab's colour."""
    return render(size, colour, mix(colour, T.BG, 0.28))
