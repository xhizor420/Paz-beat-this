"""Line icons from Lucide (ISC licence, paz_suite/fonts/Lucide-ISC.txt).

The controls used to be text: "▶", "⏸", "🔊", "⋯", "★". Those come from
whatever font Windows falls back to, so they sit at different heights and
weights, and the emoji ones arrive in colour. Lucide is one hand-drawn set
on a single grid with a single stroke weight, so a row of buttons reads as
one family.

Glyphs are drawn from the bundled TTF with Pillow rather than set as a
font on the button, because CTkButton takes one font for its whole label
and "▶ Play" needs the icon in one face and the word in another. An image
beside the text does that. Each icon is drawn 4x and downsampled for a
clean stroke, and cached, so asking again is free.
"""
from __future__ import annotations

import os

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageTk

from .theme import T

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Lucide.ttf")

# Codepoints from lucide-static 0.460.0's font/info.json.
GLYPHS = {
    "play": 0xE140,
    "pause": 0xE132,
    "volume": 0xE1AB,
    "muted": 0xE1AC,
    "back": 0xE14C,          # rotate-ccw: jump back
    "forward": 0xE14D,       # rotate-cw: jump forward
    "x": 0xE1B2,
    "history": 0xE1F5,
    "star": 0xE17A,
    "more": 0xE0BA,          # ellipsis
    "left": 0xE072,          # chevron-left
    "right": 0xE073,         # chevron-right
    "collapse": 0xE21C,      # panel-left-close
    "expand": 0xE21D,        # panel-left-open
    "search": 0xE155,
    "no-results": 0xE4B2,    # search-x
    "check": 0xE070,
    "clip": 0xE29B,          # clapperboard
    "film": 0xE0D4,
    "folder": 0xE247,        # folder-open
    "waveform": 0xE560,
    "video-file": 0xE32C,
    "inbox": 0xE0FA,
    "pointer": 0xE124,       # mouse-pointer-click
    "keyboard": 0xE284,
    "help": 0xE082,          # circle-help
    "shuffle": 0xE162,
    "repeat": 0xE14A,
    "settings": 0xE245,      # settings-2
    "grid": 0xE504,          # grid-2x2
    "sparkles": 0xE417,
    "down": 0xE071,          # chevron-down
    "music": 0xE126,
    "info": 0xE0FE,
    "tag": 0xE183,
}

OVERSAMPLE = 4
_fonts: dict = {}
_images: dict = {}
_photos: dict = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    face = _fonts.get(size)
    if face is None:
        face = _fonts[size] = ImageFont.truetype(FONT_PATH, size)
    return face


def render(name: str, size: int, color: str) -> Image.Image:
    """The glyph as a size x size RGBA image, centred on its own ink."""
    big = size * OVERSAMPLE
    char = chr(GLYPHS[name])
    face = _font(big)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = draw.textbbox((0, 0), char, font=face)
    x = (big - (right - left)) / 2 - left
    y = (big - (bottom - top)) / 2 - top
    draw.text((x, y), char, font=face, fill=color)
    return img.resize((size, size), Image.LANCZOS)


def icon(name: str, size: int = 14, color: str | None = None) -> ctk.CTkImage:
    """For a CTk widget's image=. `size` is in CTk's unscaled units, like
    every other CTk measurement; the image is drawn at the scaled size so
    CTk's own resize is a no-op rather than a second, blurring pass."""
    color = color or T.TEXT
    key = (name, size, color, T.SCALE)
    image = _images.get(key)
    if image is None:
        real = max(int(round(size * (T.SCALE or 1.0))), 1)
        drawn = render(name, real, color)
        image = _images[key] = ctk.CTkImage(drawn, drawn, size=(size, size))
    return image


def photo(name: str, size: int, color: str | None = None) -> ImageTk.PhotoImage:
    """For a raw Tk canvas. `size` is real pixels (use px())."""
    color = color or T.TEXT
    key = (name, size, color)
    image = _photos.get(key)
    if image is None:
        image = _photos[key] = ImageTk.PhotoImage(render(name, size, color))
    return image
