"""Shared visual identity: palette, fonts and status copy.

Both tabs render from this one module, so the pair look like one suite
instead of two apps that happen to share a colour scheme.
"""

from __future__ import annotations

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk


class T:
    BG        = "#0B0711"    # near-black plum
    SURFACE   = "#161021"    # cards
    ELEVATED  = "#1F1631"    # raised cards / menus
    INPUT     = "#0F0917"    # wells: viewer, log, fields
    LINE      = "#2F2247"    # hairlines
    LINE_SOFT = "#1D1430"

    ACCENT      = "#FF3D9E"  # hot pink — Convert identity
    ACCENT_HOV  = "#FF7AC1"
    ACCENT_DEEP = "#441233"
    ACCENT2      = "#9A6BFF"  # violet — Library identity
    ACCENT2_HOV  = "#B48CFF"
    ACCENT2_DEEP = "#241542"
    ACCENT3      = "#33D9A8"  # teal — Vault identity
    ACCENT3_HOV  = "#5CE6BC"
    ACCENT3_DEEP = "#123D33"

    # Colours cycled to auto-assign each new Vault project its own mark,
    # distinct from the pink/violet/teal tab identities above so a marked
    # clip's border never reads as "which tab" instead of "which project".
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

    OK        = "#53E0AE"
    OK_DEEP   = "#0E2B22"
    WARN      = "#FFC24D"
    WARN_DEEP = "#332409"
    FAIL      = "#FF5C6E"
    FAIL_DEEP = "#33101B"

    TEXT  = "#F5EFF9"
    DIM   = "#AB9AC2"
    FAINT = "#6E5C88"

    BTN        = "#231838"
    BTN_HOV    = "#30204D"
    BTN_GO     = "#E01F84"
    BTN_GO_H   = "#FF3D9E"
    BTN_STOP   = "#8A1538"
    BTN_STOP_H = "#B01C49"

    ROW      = "#140E1E"
    ROW_ALT  = "#181126"
    ROW_SEL  = "#41173A"

    CARD_SEL = "#41173A"

    RATING = {"e": "#FF5C6E", "q": "#FFC24D", "s": "#53E0AE"}

    UI   = "Segoe UI"
    MONO = "Cascadia Mono"


def font(size: int = 12, weight: str = "normal", mono: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family=T.MONO if mono else T.UI, size=size, weight=weight)


def mark_photo(size: int, color: str) -> "ImageTk.PhotoImage":
    """The app's window/taskbar icon: a plain rounded-square mark, drawn
    4x and downsampled for a crisp edge. No mascot, just a colour."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.28, fill=color)
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


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
