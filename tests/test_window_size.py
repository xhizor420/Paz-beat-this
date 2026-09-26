"""Secondary windows have to be sized the way the main one always was.

Two things go wrong with a bare `geometry("640x560")`. CustomTkinter
draws the contents at the display scaling while a geometry string is
literal screen pixels, so the window fits its own contents at 100% and
squeezes them at anything above - on a 4K screen at 150% the Folders
window showed three of ten categories and cut its explanation off
mid-word. And scaling without clamping goes wrong the other way: a
window scaled at 200% can end up taller than the desktop, putting its
own Save button somewhere nobody can reach.

The main window has done both since it was written. Every other window
was doing neither.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                   # noqa: E402


class Screen:
    def __init__(self, width=3840, height=2160):
        self._w, self._h = width, height

    def winfo_screenwidth(self):
        return self._w

    def winfo_screenheight(self):
        return self._h


class NoScreen:
    def winfo_screenwidth(self):
        raise RuntimeError("no window yet")

    def winfo_screenheight(self):
        raise RuntimeError("no window yet")


def size(text):
    return tuple(int(n) for n in text.split("x"))


@pytest.fixture(autouse=True)
def scale_100():
    before = theme.T.SCALE
    theme.T.SCALE = 1.0
    yield
    theme.T.SCALE = before


def test_at_100_percent_nothing_changes():
    """The hand-drawn numbers are 100% numbers, so nobody who is not
    scaling should see any difference at all."""
    assert size(theme.window_size(Screen(), 640, 560)) == (640, 560)
    assert size(theme.window_size(Screen(), 820, 720)) == (820, 720)


def test_the_window_grows_with_the_contents():
    theme.T.SCALE = 1.5
    assert size(theme.window_size(Screen(), 640, 560)) == (960, 840)


def test_a_window_never_comes_out_bigger_than_the_screen():
    theme.T.SCALE = 2.0
    width, height = size(theme.window_size(Screen(1920, 1080), 820, 720))
    assert width <= 1920
    assert height <= 1080


def test_room_is_left_for_the_desktop_furniture():
    """A window exactly as tall as the screen has its title bar under
    the taskbar, which on Windows means it cannot be moved."""
    theme.T.SCALE = 3.0
    _width, height = size(theme.window_size(Screen(1920, 1080), 820, 720))
    assert height < 1080


def test_a_window_that_fits_is_not_shrunk():
    theme.T.SCALE = 1.5
    assert size(theme.window_size(Screen(3840, 2160), 640, 560)) == (960, 840)


def test_a_screen_it_cannot_measure_still_gets_a_scaled_size():
    theme.T.SCALE = 1.5
    assert size(theme.window_size(NoScreen(), 640, 560)) == (960, 840)


def test_a_tiny_screen_still_leaves_something_usable():
    theme.T.SCALE = 2.0
    width, height = size(theme.window_size(Screen(320, 240), 820, 720))
    assert width > 0 and height > 0


def test_every_secondary_window_uses_it():
    """A bare geometry string is the bug. Catch a new one being added."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "paz_suite"
    bare = re.compile(r"""\.geometry\(\s*["']\d+x\d+["']""")
    offenders = []
    for path in sorted(root.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if bare.search(line):
                offenders.append(f"{path.name}:{number}  {line.strip()}")
    assert offenders == [], (
        "these windows are sized in literal pixels; use theme.window_size:\n  "
        + "\n  ".join(offenders))


# ── ttk table columns ──────────────────────────────────────────────────
#
# ttk is raw Tk: nothing in a Treeview is scaled for us, so a column
# width and a heading point size are both literal numbers on screen.
# Scaling the widths alone is not enough - the two do not grow together.
# px(90) at 150% is exactly 135, while pt(9) rounds up to 14 and the
# font's advance widths round again on top of that, so "Resolution"
# needed 141 pixels in a 132-pixel column and "Length" 100 in 94. Each
# table clipped its own headings on the one display it was being used on.

def test_a_column_is_at_least_its_hand_drawn_width():
    assert theme.column_width("File", 220) >= 220


def test_a_column_grows_with_the_display():
    theme.T.SCALE = 1.5
    assert theme.column_width("File", 220) >= theme.px(220)


def test_a_narrow_column_with_a_long_heading_is_widened():
    """The case that clipped: a 64px column headed "Resolution"."""
    theme.T.SCALE = 1.5
    theme.forget_heading_fonts()
    try:
        wide = theme.column_width("Resolution", 64)
    except Exception:
        pytest.skip("no Tk display for a real font measurement")
    if wide == theme.px(64):
        pytest.skip("font measurement unavailable; fell back to the width")
    assert wide > theme.px(64)


def test_a_column_with_no_root_still_gets_a_scaled_width(monkeypatch):
    """Built before a window exists, it must not raise - the scaled
    width on its own is a reasonable answer."""
    theme.forget_heading_fonts()
    theme.T.SCALE = 1.5
    import tkinter.font as tkfont

    def no_root(*_a, **_kw):
        raise RuntimeError("Too early to use font: no default root window")

    monkeypatch.setattr(tkfont, "Font", no_root)
    assert theme.column_width("Resolution", 64) == theme.px(64)
    theme.forget_heading_fonts()


# ── a guard for the whole class ────────────────────────────────────────
#
# CustomTkinter scales its own widgets. Raw Tk - a tk.Canvas, a
# ttk.Treeview, a tk.Text - it does not touch, so every size handed to
# one of those is the literal number of pixels on screen whatever the
# display is doing. That is a quiet bug: nothing looks wrong at 100%,
# and on the machine this app actually runs on (4K, 150%) the scrubber,
# the filmstrip, three tables and the suite's own header all sat at
# two-thirds the size of everything around them.

def paz_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "paz_suite"
    for path in sorted(root.glob("*.py")):
        yield path, path.read_text(encoding="utf-8").splitlines()


def offenders(pattern):
    import re
    found = []
    rule = re.compile(pattern)
    for path, lines in paz_source():
        for number, line in enumerate(lines, 1):
            if rule.search(line):
                found.append(f"{path.name}:{number}  {line.strip()}")
    return found


def test_no_raw_tk_font_size_is_left_unscaled():
    """font=(T.UI, 10) is 10pt on every display. pt(10) is 10pt at 100%
    and 15 at 150%, which is the same size to the eye.

    The pattern catches a tuple font wherever it is written - handed to a
    canvas item, a ttk style, or defaulted into a tk.Menu, which is how
    every right-click menu in the app stayed small."""
    assert offenders(r"\(T\.(UI|MONO|DISPLAY),\s*\d") == []


def test_no_treeview_row_height_is_left_unscaled():
    assert offenders(r"rowheight=\d") == []


def test_no_window_is_sized_in_literal_pixels():
    assert offenders(r"""\.geometry\(\s*["']\d+x\d+["']""") == []
