"""Text in a fixed-width font is measured by multiplication, not by Tk.

Asking Tk for a width is about 0.4ms, and the gallery's badges (in the
mono font) put some fifty new strings on each unseen page - 20ms of a
page flip. In a fixed-width font every ASCII character has the same
advance, so the width is the advance times the length.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import theme                                 # noqa: E402


class Font:
    def __init__(self, fixed=True, lying=False):
        self.fixed = fixed
        self.lying = lying
        self.asked = 0

    def metrics(self, what):
        assert what == "fixed"
        return 1 if (self.fixed or self.lying) else 0

    def measure(self, text):
        self.asked += 1
        if self.fixed:
            return 7 * len(text)
        return sum(4 if c in "il." else 9 for c in text)


def test_a_fixed_font_is_multiplied_not_asked():
    font = Font()
    before = None
    for text in ("4 min 32 sec", "1080p.60", "9.2 MB", "#4000127"):
        assert theme.text_width(font, text) == 7 * len(text)
        before = font.asked
    # Two letters to confirm it is fixed; nothing per string after that.
    assert before == 2


def test_a_font_that_only_says_it_is_fixed_is_asked():
    font = Font(fixed=False, lying=True)
    assert theme.text_width(font, "ill") == 4 + 4 + 4
    assert theme.text_width(font, "WWW") == 27


def test_non_ascii_is_always_asked():
    """A fallback glyph - a tick, an arrow - can have its own width."""
    font = Font()
    asked = font.asked
    theme.text_width(font, "4K ✓")
    assert font.asked > asked


def test_a_proportional_font_is_asked():
    font = Font(fixed=False)
    assert theme.text_width(font, "wolf") == font.measure("wolf")
