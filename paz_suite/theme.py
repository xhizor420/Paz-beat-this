"""Shared visual identity: palette, fonts, app mark and status copy.

Every tab renders from this one module, so the suite looks like one app
instead of four that happen to share a colour scheme.

The palette is "Neon Den" - the scheme drawn up in design/*.dc.html and
published as a canvas. It keeps the four tab identities the app has
always had (pink Convert, violet Library, mint Vault, amber Beat This)
and their roles, but drops the ground much darker, pushes chroma on the
accents, and treats accents as light sources rather than flat fills:
anything active is meant to look lit, not painted. Since this is a
library of adult material, the explicit rating is deliberately the
loudest colour in the set - `is:e` should be readable at a glance across
a wall of thumbnails, not politely muted next to safe and questionable.
"""

from __future__ import annotations

import itertools
import os

import customtkinter as ctk
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk


class T:
    # ── ground ───────────────────────────────────────────────────────────
    BG        = "#07040C"    # near-black plum, deeper than the old #0B0711
    SURFACE   = "#140C1F"    # cards
    ELEVATED  = "#1C1230"    # raised cards / menus
    INPUT     = "#080510"    # wells: viewer, log, fields
    LINE      = "#2B1E45"    # hairlines
    LINE_SOFT = "#1A1230"

    # ── tab identities ───────────────────────────────────────────────────
    # Same four roles as before, higher chroma. *_DEEP is a dark fill that
    # sits behind *bright* text (buttons, the selected tab), so it stays
    # dark enough to keep that text legible.
    ACCENT      = "#FF2E9A"  # hot magenta — Convert
    ACCENT_HOV  = "#FF6BB5"
    ACCENT_DEEP = "#52123C"
    ACCENT2      = "#A46BFF"  # violet — Library
    ACCENT2_HOV  = "#C8A9FF"
    ACCENT2_DEEP = "#2C1A52"
    ACCENT3      = "#2EE6B0"  # mint — Vault
    ACCENT3_HOV  = "#7BF0CC"
    ACCENT3_DEEP = "#10402F"
    ACCENT4      = "#FFB03D"  # amber — Beat This
    ACCENT4_HOV  = "#FFCC80"
    ACCENT4_DEEP = "#4A3110"

    # Cycled to auto-assign each new Vault project its own mark, kept
    # distinct from the four tab identities above so a marked clip's
    # border reads as "which project", never as "which tab".
    PROJECT_PALETTE = (
        "#4DA3FF",  # blue
        "#FFB84D",  # amber
        "#FF6B4D",  # coral
        "#B8E64D",  # lime
        "#4DD9E6",  # cyan
        "#FFD24D",  # gold
        "#FF6B8A",  # rose
        "#8FD9A8",  # sage
    )

    # ── status ───────────────────────────────────────────────────────────
    OK        = "#2EE6B0"
    OK_DEEP   = "#0C2E24"
    WARN      = "#FFB03D"
    WARN_DEEP = "#3A2A0A"
    FAIL      = "#FF5C6E"
    FAIL_DEEP = "#3D1018"

    # ── text ─────────────────────────────────────────────────────────────
    TEXT  = "#F7EFFA"
    DIM   = "#C3B2DB"        # lifted from #AB9AC2 — panel copy reads better
    FAINT = "#6E5C8C"

    # ── controls ─────────────────────────────────────────────────────────
    BTN        = "#1C1230"
    BTN_HOV    = "#2B1E45"
    BTN_GO     = "#E82A8E"
    BTN_GO_H   = "#FF5CAD"
    BTN_STOP   = "#A3163F"
    BTN_STOP_H = "#C81E52"

    # ── rows ─────────────────────────────────────────────────────────────
    ROW      = "#120B1D"
    ROW_ALT  = "#170F24"
    ROW_SEL  = "#4A1236"

    CARD_SEL = "#4A1236"

    # e is the loudest of the three on purpose — see the module docstring.
    RATING = {"e": "#FF2D5A", "q": "#FFC24D", "s": "#53E0AE"}

    # ── tag categories ───────────────────────────────────────────────────
    #
    # e621's own category colours, lifted to sit on a near-black ground.
    # These are not decoration and not a free choice: anyone who has spent
    # time on e621 reads tags by colour without looking at the heading -
    # orange is an artist, green is a character, red-orange is a species.
    # That is years of muscle memory, and a library keyed on e621 post IDs
    # should speak the same language.
    #
    # The site's values are tuned for a light-grey page, so they are
    # brightened here rather than copied: #f2ac08 artist, #0a0 character,
    # #ed5d1f species, #d0d copyright, #282 lore, #b4c7d9 general.
    TAG = {
        "artist":    "#FFBB2E",   # e621 #f2ac08
        "character": "#3FD96B",   # e621 #0a0
        "species":   "#FF7A45",   # e621 #ed5d1f
        "copyright": "#F062F0",   # e621 #d0d
        "lore":      "#59C97A",   # e621 #282
        "general":   "#B4C7D9",   # e621 #b4c7d9
        "meta":      "#C3B2DB",
    }

    # ── type ─────────────────────────────────────────────────────────────
    # Placeholders. resolve_fonts() replaces these with whatever is
    # actually installed once a Tk root exists; the values here are the
    # Windows defaults so the app still looks right if that never runs.
    SCALE   = 1.0            # see the module-level pt()/px() helpers
    UI      = "Segoe UI"
    MONO    = "Cascadia Mono"
    DISPLAY = "Segoe UI"


# The design canvas is set in Space Grotesk / Bricolage Grotesque /
# JetBrains Mono. Tk can only use families installed on the machine, and
# none of those three ship with Windows or macOS, so each role is a
# preference list: install the real faces and the app picks them up, else
# it falls back to the closest thing already present. Ordered best-first.
# Bahnschrift sits *below* Segoe UI on purpose: it has more character but
# it is condensed, and these layouts were drawn to Space Grotesk's much
# wider metrics - swapping in a narrow face makes every label sit wrong.
_UI_STACK = ("Space Grotesk", "Segoe UI Variable Text", "Segoe UI",
             "Inter", "Bahnschrift", "DejaVu Sans", "Helvetica")
_MONO_STACK = ("JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Consolas",
               "SF Mono", "DejaVu Sans Mono", "Courier New")
_DISPLAY_STACK = ("Bricolage Grotesque", "Space Grotesk", "Bahnschrift",
                  "Segoe UI Variable Display", "Segoe UI Semibold",
                  "Segoe UI", "DejaVu Sans")


def resolve_fonts() -> None:
    """Pick the best installed family for each role. Call once, from the
    main thread, after the Tk root exists - tkinter.font.families() needs
    an interpreter to ask. Silently keeps the defaults if anything goes
    wrong, since a missing font should never stop the app from opening."""
    try:
        import tkinter.font as tkfont
        available = {name.lower() for name in tkfont.families()}
    except Exception:
        return

    def first(stack: tuple, fallback: str) -> str:
        for family in stack:
            if family.lower() in available:
                return family
        return fallback

    T.UI = first(_UI_STACK, T.UI)
    T.MONO = first(_MONO_STACK, T.MONO)
    T.DISPLAY = first(_DISPLAY_STACK, T.UI)


def mix(color: str, toward: str, amount: float) -> str:
    """Blend `color` toward `toward` by `amount` (0..1).

    Tk has no opacity, so anything the design canvas draws at partial
    alpha over a known background has to be pre-blended into a solid
    hex here instead - the dimmed tab dots, mainly."""
    a = color.lstrip("#")
    b = toward.lstrip("#")
    t = max(0.0, min(1.0, amount))
    channels = (
        round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t)
        for i in (0, 2, 4)
    )
    return "#" + "".join(f"{c:02X}" for c in channels)


# How much bigger everything this app draws by hand should be. CTk scales
# its own widgets from the system DPI, but a tk.Canvas gets none of that -
# so on a 4K screen the gallery kept drawing 8pt badges and 10pt captions
# at their literal pixel size while every button around them grew. Set
# once at startup (see app.PazApp._apply_scaling) and read by pt()/px().
SCALE = 1.0


def pt(size: float) -> int:
    """A hand-drawn font size, scaled. Floors at 7pt - past that the text
    is decoration, not something anyone can read."""
    return max(int(round(size * T.SCALE)), 7)


def px(value: float) -> int:
    """A hand-drawn pixel measurement, scaled."""
    return int(round(value * T.SCALE))


def unscaled(value: float) -> int:
    """Real screen pixels -> the units CustomTkinter wants.

    CTk multiplies every geometry option it is handed by the widget
    scaling, so a size that came off the screen (winfo_width) or was built
    with px() gets scaled a SECOND time on the way back into a CTk widget:
    at 150% the widget comes out half as wide again as the number asked
    for. That is how the inspector column ended up far wider than the
    picture inside it, with a band of empty panel either side, and how
    theater pushed its own button off the edge of the window.

    Raw Tk widgets - canvases, plain frames - are not scaled and take real
    pixels as they are. This is only for CTk's own width, height and
    wraplength options, and only when the number has to line up with
    something measured. A hand-drawn design number still goes in as it is
    and lets CTk do the one scaling it expects to do.
    """
    scale = T.SCALE or 1.0
    return max(int(round(value / scale)), 1)


# ── how wide is this text? ──────────────────────────────────────────────
#
# Asking Tk costs 424 microseconds. Drawing a canvas item costs 4.6, so
# one measurement is worth ninety of them - and Tk does not remember the
# answer, so asking twice about the same string costs twice. Measured on
# a 4K-scaled UI font; the absolute number will differ per platform and
# font backend, but the shape (measuring text costs far more than drawing
# it) does not.
#
# A gallery page asks about forty-eight clip names, a tag rail about a
# hundred and twenty chips, and the seek bar asks again on every playback
# tick. Two thirds of what drawing a page of the gallery cost was this.
#
# So there are two ways out, and this module offers both: remember every
# answer, and - better - do not ask when the answer cannot matter.
_WIDEST: dict = {}
_WIDTHS: dict = {}
# Enough for a large library's worth of distinct strings; cleared rather
# than evicted one by one, because the answers are cheap to re-earn and
# the point is to bound the memory, not to be clever about it.
WIDTH_CACHE = 8000

# The guard below needs a character at least as wide as any in the text,
# and that is only knowable for a repertoire you have looked at. So it is
# only offered for ASCII text: a filename carrying a CJK character or an
# emoji gets measured properly instead. (e621 tags are romanised by
# convention, but a filename can be anything.)
#
# Seven candidates rather than the whole printable range, because every
# one of these is a measurement paid at startup, per font. Checked
# against all of printable ASCII across the families and sizes this app
# resolves to, and they contain the widest glyph in every one - see
# test_text_width.py, which fails if that ever stops being true. If an
# unusual font did hide something wider, a caption would come out a
# pixel or two over its card and be clipped, which is cosmetic.
_WIDE_SAMPLE = "W@%M#m&"


_KEYS = itertools.count()


def _font_key(font_obj):
    """What identifies a font for caching.

    A number handed to the font object itself and kept there. Not its
    repr or its id: both are addresses, and CPython reuses an address as
    soon as the object at it is collected - so a new font could inherit
    a dead one's cached widths and silently draw at the wrong size. A
    tag that lives on the object cannot outlive it.
    """
    key = getattr(font_obj, "_paz_width_key", None)
    if key is None:
        key = next(_KEYS)
        try:
            font_obj._paz_width_key = key
        except Exception:
            # Cannot be tagged, so it gets no cache rather than a
            # possibly-wrong one.
            return None
    return key


def widest_char(font_obj) -> int:
    """The widest ASCII character in this font, measured once.

    An n-character ASCII string cannot be wider than n times this, which
    is what makes text_fits able to answer without asking Tk.
    """
    key = _font_key(font_obj)
    got = _WIDEST.get(key) if key is not None else None
    if got is None:
        try:
            got = max(font_obj.measure(ch) for ch in _WIDE_SAMPLE)
        except Exception:
            got = 0          # a font that cannot measure gets no shortcut
        if key is not None:
            _WIDEST[key] = got
    return got


def text_width(font_obj, text: str) -> int:
    """How wide `text` is, remembered. 0 if the font cannot say."""
    name = _font_key(font_obj)
    key = (name, text)
    got = _WIDTHS.get(key) if name is not None else None
    if got is None:
        try:
            got = int(font_obj.measure(text))
        except Exception:
            return 0
        if name is not None:
            if len(_WIDTHS) >= WIDTH_CACHE:
                _WIDTHS.clear()
            _WIDTHS[key] = got
    return got


def text_fits(font_obj, text: str, room: int) -> bool:
    """True when `text` fits in `room` pixels.

    Answers without measuring whenever it can prove the answer: the
    widest character times the length is an upper bound on the width, so
    if that fits, the text fits. Every string this app puts on a card, a
    badge or a chip - post IDs, "1080p.60", "2 min", an artist name -
    clears that bar, which is why a page of the gallery now measures
    nothing at all.
    """
    room = int(room)
    if room <= 0:
        return not text
    if not text:
        return True
    if text.isascii():
        wide = widest_char(font_obj)
        if wide and len(text) * wide <= room:
            return True
    return text_width(font_obj, text) <= room


def forget_text_widths() -> None:
    """Drop every remembered width. Only needed if the fonts change,
    which in this app means a restart - the display scale is applied
    once, before any widget exists."""
    _WIDEST.clear()
    _WIDTHS.clear()


def font(size: int = 12, weight: str = "normal", mono: bool = False,
         display: bool = False) -> ctk.CTkFont:
    if mono:
        family = T.MONO
    elif display:
        family = T.DISPLAY
    else:
        family = T.UI
    return ctk.CTkFont(family=family, size=size, weight=weight)


def mark_photo(size: int, color: str) -> "ImageTk.PhotoImage":
    """The app's window/taskbar icon: a paw knocked out of a rounded tile
    in the accent colour, with a soft bloom behind it so it reads as lit
    rather than flat. Drawn 8x and downsampled for a clean edge at 16px.

    A paw rather than the old plain square because at icon size the suite
    needs to be recognisable in a taskbar full of other dark squares, and
    because it says what this library is about without the icon having to
    be something you would rather not have on a shared screen.
    """
    big = size * 8
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.28, fill=color)

    # Bloom: a blurred copy of the tile under the crisp one, so the mark
    # carries the same "glow" the rest of the palette is built on.
    glow = tile.filter(ImageFilter.GaussianBlur(big * 0.06))
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.alpha_composite(glow)
    img.alpha_composite(tile)

    paw = ImageDraw.Draw(img)

    def ellipse(cx: float, cy: float, w: float, h: float) -> None:
        paw.ellipse((int((cx - w / 2) * big), int((cy - h / 2) * big),
                     int((cx + w / 2) * big), int((cy + h / 2) * big)),
                    fill=T.BG)

    ellipse(0.50, 0.69, 0.46, 0.34)      # main pad
    ellipse(0.235, 0.40, 0.16, 0.21)     # toes, outer pair sits lower
    ellipse(0.415, 0.325, 0.16, 0.21)
    ellipse(0.605, 0.325, 0.16, 0.21)
    ellipse(0.785, 0.40, 0.16, 0.21)

    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


def lens_photo(size: int, color: str) -> "ImageTk.PhotoImage":
    """A magnifier for the search field.

    Drawn rather than typed: the U+2312 "arc" character most fonts map for
    this is a hairline squiggle at UI sizes, and the emoji magnifier only
    exists in colour-emoji fonts that are not installed everywhere. Same
    8x-and-downsample trick as the paw mark, so the ring stays smooth.
    """
    big = size * 8
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ring = big * 0.10
    draw.ellipse((ring, ring, big * 0.72, big * 0.72), outline=color,
                 width=int(ring))
    draw.line((big * 0.63, big * 0.63, big - ring, big - ring), fill=color,
              width=int(ring), joint="curve")
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))

BANNER_H = 58          # header strip height in px
# Was 76. On a 1200px-tall window the strip behind the mark, the tab row
# and the Library's own search row came to 275px - nearly a quarter of
# the height - before a single clip was drawn. The mark and the clip
# count still fit; the difference goes to the gallery and the player.
_BANNER_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def _rgb(color: str) -> tuple:
    value = color.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _scrim(width: int, height: int) -> "Image.Image":
    """The wash that goes over a banner picture so the lockup and the
    status line stay readable no matter what the picture is.

    Heaviest at the two edges where text sits (the PAZ mark on the left,
    the live status on the right) and lightest across the middle, so the
    picture is genuinely visible rather than a dark rectangle with a hint
    of something behind it. Built as an L-mode alpha ramp and used as the
    mask for a flat ground colour - one paste, no per-pixel Python.
    """
    ramp = Image.new("L", (width, 1))
    pixels = ramp.load()
    for x in range(width):
        pos = x / max(width - 1, 1)
        # The floor was low enough that a bright picture read as the main
        # event - a saturated band across the top with the app underneath
        # it. A header should be the room the app sits in, not the thing
        # competing with it, so the picture stays visible but subordinate.
        if pos < 0.42:                      # left: under the lockup
            alpha = 242 - (pos / 0.42) * 92
        elif pos < 0.68:                    # middle: let the picture through
            alpha = 150
        else:                               # right: under the status line
            alpha = 150 + ((pos - 0.68) / 0.32) * 80
        pixels[x, 0] = int(alpha)
    mask = ramp.resize((width, height), Image.BILINEAR)

    # A second, vertical ramp darkens the bottom edge into the tab strip
    # below, so the header doesn't end on a hard seam.
    column = Image.new("L", (1, height))
    col = column.load()
    for y in range(height):
        col[0, y] = int(60 * (y / max(height - 1, 1)) ** 3)
    mask = ImageChops.lighter(mask, column.resize((width, height), Image.BILINEAR))
    return mask


def _default_banner(width: int, height: int) -> "Image.Image":
    """What the header looks like before anyone has set a picture: the four
    tab identities swept across the width in order - pink Convert, violet
    Library, mint Vault, amber Beat This - each pulled most of the way down
    to the ground colour so it reads as a lit edge, not a rainbow.

    Deliberately not a flat bar. The strip should look designed on first
    launch rather than like an empty slot waiting to be filled, and using
    the tab colours means the default says something instead of being
    decoration.
    """
    stops = [_rgb(mix(c, T.BG, 0.58))
             for c in (T.ACCENT, T.ACCENT2, T.ACCENT3, T.ACCENT4)]
    stops = [_rgb(T.BG)] + stops + [_rgb(T.BG)]
    span = len(stops) - 1
    ramp = Image.new("RGB", (width, 1))
    pixels = ramp.load()
    for x in range(width):
        pos = (x / max(width - 1, 1)) * span
        index = min(int(pos), span - 1)
        t = pos - index
        a, b = stops[index], stops[index + 1]
        pixels[x, 0] = tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))
    base = ramp.resize((width, height), Image.BILINEAR)

    # Sink the bottom half toward the ground so the colour reads as a glow
    # coming off the top edge rather than a solid painted band.
    column = Image.new("L", (1, height))
    col = column.load()
    for y in range(height):
        col[0, y] = int(215 * (y / max(height - 1, 1)) ** 1.7)
    base.paste(Image.new("RGB", (width, height), _rgb(T.BG)), (0, 0),
               column.resize((width, height), Image.BILINEAR))
    return base


def banner_image(path: str, width: int, height: int = BANNER_H,
                 zoom: float = 1.0, fx: float = 0.5, fy: float = 0.34) -> "Image.Image":
    """Render the header strip: the user's own picture (or the default
    sweep) cropped to fill, scrimmed, and capped with an accent hairline.

    The crop is anchored above centre rather than dead centre - in most
    pictures the part worth seeing sits in the upper half, and a 76px-tall
    slot through the middle of a portrait usually lands on nothing.
    """
    width = max(int(width), 200)
    height = max(int(height), 24)
    picture = None
    if path and os.path.isfile(path):
        try:
            with Image.open(path) as source:
                picture = source.convert("RGB")
        except Exception:
            picture = None

    if picture is None:
        base = _default_banner(width, height)
    else:
        # The crop is the slot's, not a fixed anchor a third of the way
        # down - see paz_suite/artwork.py. Recomputed at this width, so
        # the strip re-crops as the window resizes instead of stretching.
        from .artwork import crop_box
        box = crop_box(picture.size, (width, height), zoom, fx, fy)
        base = picture.crop(box)
        if base.size != (width, height):
            base = base.resize((width, height), Image.LANCZOS)

    if picture is not None:
        ground = Image.new("RGB", (width, height), _rgb(T.BG))
        base.paste(ground, (0, 0), _scrim(width, height))

    # Hairline along the bottom, brightest under the lockup - the same
    # lit-edge treatment the cards and the tab strip use.
    edge = ImageDraw.Draw(base)
    edge.line((0, height - 1, width, height - 1), fill=_rgb(T.LINE))
    edge.line((0, height - 1, int(width * 0.34), height - 1), fill=_rgb(T.ACCENT_DEEP))
    return base


def banner_photo(path: str, width: int, height: int = BANNER_H,
                 zoom: float = 1.0, fx: float = 0.5,
                 fy: float = 0.34) -> "ImageTk.PhotoImage":
    return ImageTk.PhotoImage(banner_image(path, width, height, zoom, fx, fy))


def is_image_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _BANNER_EXT

# Status/label copy, one plain string per key (no alternate wording, no
# toggle - just what the button or status line says). Kept as small dicts
# rather than inlined at every call site so a single place controls each
# piece of wording and any {placeholders} still get filled via .format().
CONVERT_LABELS = {
    "scan":       "Scan folders",
    "start":      "Start",
    "stop":       "Stop",
    "pause":      "Pause",
    "resume":     "Resume",
    "watch":      "Watch mode",
    "promote":    "Promote upscales",
    "dupes":      "Find duplicates",
    "gaps":       "Find upscale gaps",
    "gapping":    "Checking for gaps",
    "grid":       "Grid",
    "inspector":  "INSPECTOR",
    "log":        "LOG",
    "tagline":    "clip pipeline",
    "idle":       "Idle",
    "watching":   "Watching",
    "encoding":   "Encoding",
    "sorting":    "Sorting",
    "stopping":   "Stopping",
    "paused":     "Paused after this file",
    "finished":   "Finished",
    "stopped":    "Stopped",
    "checkup":    "Checking upscales",
    "no_selection": "Select a file to inspect it",
    "empty":      "Nothing queued",
    "nothing_msg": ("Nothing to convert. Everything here is already "
                     "converted, or the source folders are empty."),
    "run_start":  "Starting {n} files on {w} worker{s}",
    "run_done":   "Run finished: {d} converted · {f} failed · {srt} sorted",
    "watch_new":  "Watch: {n} new file{s} settled",
    "fetch_tags": "Fetch e621 tags",
    "fetching":   "Fetching e621 tags",
    "fetch_done": "e621: {n} tagged · {m} unavailable",
}

LIBRARY_LABELS = {
    "tagline":    "local library search",
    "sync":       "Sync library",
    "fetch":      "Fetch e621 tags",
    "idle":       "Ready",
    "scanning":   "Scanning folders",
    "indexing":   "Indexing",
    "synced":     "Library synced",
    "fetching":   "Fetching tags",
    "empty_db":   ("No library yet. Press Sync to build it - the first "
                    "build probes every file, later runs only touch changes."),
    "no_results": "No clips match this search.",
}

VAULT_LABELS = {
    "tagline":  "used-clip tracker",
    "idle":     "Ready",
    "empty":    "Paste a list of post IDs or filenames above, then press Look up.",
}

BEAT_LABELS = {
    "tagline":     "beat markers for Resolve",
    "idle":        "Pick a song, then press Analyze",
    "loading":     "Loading model",
    "analyzing":   "Analyzing audio",
    "writing":     "Writing",
    "done":        "Done",
    "no_file":     "Pick an audio file first.",
    "no_result":   "Analyze a song first.",
    "missing_deps": ("The beat tracker's dependencies aren't installed yet - press "
                      "Install dependencies in the Setup panel above."),
    "analyze_done": "{n} beats · {d} downbeats · {bpm:.1f} BPM",
    "saved_tsv":    "Saved .beats file to {path}",
    "saved_edl":    "Saved EDL to {path} — Resolve: Timeline > Import > Timeline Markers from EDL",
    "resolve_ok":   "{msg}",
    "resolve_fail": "{msg}",
}
