"""Swapping a sharpened thumbnail in must not fail when the player has
not shown a picture yet - it did, for every clip, 10,808 times in a log."""

from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab  # noqa: E402


def tab(thumb_src):
    player = SimpleNamespace(_thumb_src=thumb_src, _prepared={("a.mp4", 1, 1): 1},
                             _prep_lock=threading.Lock(), playing=False)
    return SimpleNamespace(CARD_W=100, IMG_H=56, cfg=SimpleNamespace(thumb_fit="contain"),
                           _photos={}, _tiles={}, _tiles_lock=threading.Lock(),
                           player=player, _layout=[])


def test_no_picture_shown_yet_is_fine():
    t = tab(None)
    LibraryTab._reshow_thumb(t, "a.mp4")
    assert t.player._prepared == {}


def test_the_shown_picture_of_that_clip_is_dropped():
    t = tab(("a.mp4", object()))
    LibraryTab._reshow_thumb(t, "a.mp4")
    assert t.player._thumb_src is None
    other = tab(("b.mp4", "img"))
    LibraryTab._reshow_thumb(other, "a.mp4")
    assert other.player._thumb_src == ("b.mp4", "img")
