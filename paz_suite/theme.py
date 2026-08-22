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

    # ── type ─────────────────────────────────────────────────────────────
    # Placeholders. resolve_fonts() replaces these with whatever is
    # actually installed once a Tk root exists; the values here are the
    # Windows defaults so the app still looks right if that never runs.
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


BANNER_H = 76          # header strip height in px
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
        if pos < 0.42:                      # left: under the lockup
            alpha = 235 - (pos / 0.42) * 130
        elif pos < 0.68:                    # middle: let the picture through
            alpha = 105
        else:                               # right: under the status line
            alpha = 105 + ((pos - 0.68) / 0.32) * 95
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


def banner_image(path: str, width: int, height: int = BANNER_H) -> "Image.Image":
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
        scale = max(width / picture.width, height / picture.height)
        size = (max(int(picture.width * scale), width),
                max(int(picture.height * scale), height))
        picture = picture.resize(size, Image.LANCZOS)
        top = int((picture.height - height) * 0.34)
        left = (picture.width - width) // 2
        base = picture.crop((left, top, left + width, top + height))

    if picture is not None:
        ground = Image.new("RGB", (width, height), _rgb(T.BG))
        base.paste(ground, (0, 0), _scrim(width, height))

    # Hairline along the bottom, brightest under the lockup - the same
    # lit-edge treatment the cards and the tab strip use.
    edge = ImageDraw.Draw(base)
    edge.line((0, height - 1, width, height - 1), fill=_rgb(T.LINE))
    edge.line((0, height - 1, int(width * 0.34), height - 1), fill=_rgb(T.ACCENT_DEEP))
    return base


def banner_photo(path: str, width: int, height: int = BANNER_H) -> "ImageTk.PhotoImage":
    return ImageTk.PhotoImage(banner_image(path, width, height))


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
