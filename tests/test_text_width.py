"""Asking Tk how wide text is, and mostly not asking.

Measured on this app's fonts: `font.measure()` costs 424 microseconds a
call, against 4.6 to create a canvas item - one measurement is worth
ninety of them - and Tk does not remember the answer, so asking twice
costs twice. A gallery page asked about forty-eight clip names and a tag
rail about a hundred and twenty chips, which was two thirds of what
drawing a page cost.

Two things fix that, and these tests pin both: every answer is
remembered, and the question is not asked at all when the answer can be
proved. The proof is that an n-character string cannot be wider than n
times the font's widest character - so if that fits, it fits.

The hazard in that proof is the word "widest". It is sampled over a
handful of ASCII candidates, so it says nothing about a name carrying a
CJK character or an emoji, and those must fall through to a real
measurement rather than get a wrong yes.
"""

from __future__ import annotations

import os
import string
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                  # noqa: E402


class Font:
    """Counts what it is asked, so a test can prove an answer was reused
    rather than measured again. 10px per character, and 40px for anything
    outside ASCII - so a font where the guard's sample says nothing."""

    def __init__(self):
        self.asked = []

    def measure(self, text: str) -> int:
        self.asked.append(text)
        return sum(10 if ch.isascii() else 40 for ch in text)


@pytest.fixture(autouse=True)
def fresh():
    theme.forget_text_widths()
    yield
    theme.forget_text_widths()


# ── remembering ─────────────────────────────────────────────────────────

def test_a_width_is_measured_once():
    f = Font()
    assert theme.text_width(f, "hello") == 50
    assert theme.text_width(f, "hello") == 50
    assert f.asked == ["hello"]


def test_different_strings_are_different_answers():
    f = Font()
    assert theme.text_width(f, "ab") == 20
    assert theme.text_width(f, "abcd") == 40


def test_two_fonts_do_not_share_answers():
    """The same word is a different width in a different font."""
    small, big = Font(), Font()
    big.measure = lambda t: len(t) * 30
    theme.text_width(small, "word")
    assert theme.text_width(big, "word") == 120


def test_a_font_that_cannot_measure_reports_nothing_and_does_not_raise():
    class Broken:
        def measure(self, _text):
            raise RuntimeError("no font")

    assert theme.text_width(Broken(), "x") == 0


def test_forgetting_makes_it_ask_again():
    f = Font()
    theme.text_width(f, "hello")
    theme.forget_text_widths()
    theme.text_width(f, "hello")
    assert f.asked == ["hello", "hello"]


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(theme, "WIDTH_CACHE", 20)
    f = Font()
    for i in range(60):
        theme.text_width(f, f"text{i}")
    assert len(theme._WIDTHS) <= 20


def test_a_font_is_not_confused_with_a_dead_one():
    """Keying on repr() or id() would let a new font inherit a collected
    one's widths, because CPython reuses addresses."""
    first = Font()
    theme.text_width(first, "word")
    key = first._paz_width_key
    del first
    second = Font()
    second.measure = lambda t: len(t) * 99
    assert theme.text_width(second, "word") == 4 * 99
    assert second._paz_width_key != key


# ── and not asking ──────────────────────────────────────────────────────

def test_text_that_provably_fits_is_never_measured():
    f = Font()
    theme.widest_char(f)                  # the one-off sampling
    f.asked.clear()
    assert theme.text_fits(f, "4006437", 1000) is True
    assert f.asked == [], "measured something it could have proved"


def test_text_that_might_not_fit_is_measured():
    f = Font()
    theme.widest_char(f)
    f.asked.clear()
    theme.text_fits(f, "a" * 40, 100)
    assert f.asked == ["a" * 40]


def test_the_verdict_is_right_either_way():
    f = Font()
    assert theme.text_fits(f, "abc", 30) is True        # exactly 30
    assert theme.text_fits(f, "abcd", 30) is False


def test_non_ascii_is_never_guessed_about():
    """The guard's sample is ASCII, so it knows nothing about a wide
    glyph - and a wrong yes here means a caption overflowing its card."""
    f = Font()
    theme.widest_char(f)
    f.asked.clear()
    # Four 40px characters is 160px, which does NOT fit in 100 - but
    # len * widest_ascii would say 4 * 10 = 40 and wrongly allow it.
    assert theme.text_fits(f, "美美美美", 100) is False
    assert f.asked, "took the ASCII shortcut on non-ASCII text"


def test_nothing_fits_in_no_room():
    f = Font()
    assert theme.text_fits(f, "x", 0) is False
    assert theme.text_fits(f, "x", -5) is False
    assert theme.text_fits(f, "", 0) is True


def test_empty_text_always_fits():
    f = Font()
    assert theme.text_fits(f, "", 10) is True


def test_the_widest_character_is_measured_once_per_font():
    f = Font()
    first = theme.widest_char(f)
    asked = len(f.asked)
    assert theme.widest_char(f) == first
    assert len(f.asked) == asked


def test_a_font_that_cannot_measure_gets_no_shortcut():
    """widest_char of 0 must disable the guard rather than claim that
    everything fits in nothing."""
    class Broken:
        def measure(self, _text):
            raise RuntimeError("no font")

    assert theme.widest_char(Broken()) == 0
    assert theme.text_fits(Broken(), "anything", 10) is True  # width 0 <= 10


# ── the sample really does contain the widest glyph ─────────────────────

def test_the_candidate_set_finds_the_true_widest_glyph():
    """The guard is only sound if _WIDE_SAMPLE contains a character at
    least as wide as anything else the font can draw. Checked against all
    of printable ASCII, for whatever fonts this machine has."""
    tkfont = pytest.importorskip("tkinter.font")
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        printable = [c for c in string.printable if c.isprintable()]
        checked = 0
        for family in ("DejaVu Sans", "DejaVu Sans Mono", "Liberation Sans",
                       "TkDefaultFont", "TkFixedFont"):
            for size in (10, 15, 18, 24):
                try:
                    f = tkfont.Font(family=family, size=size)
                    true_widest = max(f.measure(c) for c in printable)
                except Exception:
                    continue
                checked += 1
                assert theme.widest_char(f) >= true_widest, (
                    f"{family} {size}pt has a glyph wider than the sample")
        if not checked:
            pytest.skip("no usable fonts on this machine")
    finally:
        root.destroy()
