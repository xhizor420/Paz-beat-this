"""Pressing Play plays - the first time, the right copy, from the right place.

Three ways it did not:

* The details panel re-renders for things that are not a new selection (a
  background tag fetch landing, a refresh, a folded tag group) and each
  re-render stopped the player. Play pressed just before a fetch finished
  started and was stopped again, and took a second press.
* With 4K preferred, the 4K copy only loaded once an ffprobe of it came
  back - on the library drive, a while. Play pressed before then played
  the small copy, under a button reading "4K ✓".
* VLC reopening a clip to resume was handed the position with set_time()
  before the file was open, which it ignores: back to 0:00.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import library_player as lp            # noqa: E402
from paz_suite import vlc_player as vp                # noqa: E402
from paz_suite.library_tab import LibraryTab          # noqa: E402


class Rec:
    def __init__(self, path, premium_path="", duration=12.0, fps=30.0):
        self.path = path
        self.premium_path = premium_path
        self.duration = duration
        self.fps = fps


class Engine:
    def __init__(self):
        self.calls = []
        self.playing = False
        self.path = ""
        self.duration = 0.0

    def stop(self):
        self.calls.append("stop")
        self.playing = False

    def load(self, path, duration, fps):
        self.calls.append(("load", path, duration, fps))
        self.path, self.duration = path, duration

    def clear(self):
        self.calls.append("clear")


class Cfg:
    player_prefer_premium = False


class Tab:
    def __init__(self):
        self.cfg = Cfg()
        self.frames = type("F", (), {"prime_hover": lambda *a: None})()


def inline_player():
    p = lp.InlinePlayer.__new__(lp.InlinePlayer)
    p.engine = Engine()
    p.tab = Tab()
    p.rec = None
    p._premium_probe = {}
    p._last_seek_pos = None
    for name in ("_peek_hide", "_draw_bar", "_update_quality_btn", "_offer_premium"):
        setattr(p, name, lambda *a, **k: None)
    p._show_thumb = lambda *a, **k: None
    p._show_idle_text = lambda *a, **k: None
    return p


# ── a re-render is not a new selection ──────────────────────────────────

def test_showing_the_clip_already_loaded_leaves_it_playing():
    p = inline_player()
    p.show_rec(Rec("/a.mp4"))
    p.engine.calls.clear()
    p.engine.playing = True
    p.show_rec(Rec("/a.mp4"))          # a refresh hands over a fresh record
    assert p.engine.calls == [], "re-rendering the details stopped playback"
    assert p.engine.playing


def test_a_different_clip_still_stops_the_old_one():
    p = inline_player()
    p.show_rec(Rec("/a.mp4"))
    p.engine.calls.clear()
    p.show_rec(Rec("/b.mp4"))
    assert p.engine.calls[0] == "stop"
    assert p.engine.calls[1][:2] == ("load", "/b.mp4")


# ── the 4K copy, at once ────────────────────────────────────────────────

def test_the_4k_copy_loads_without_waiting_for_its_probe():
    p = inline_player()
    p.tab.cfg.player_prefer_premium = True
    p.show_rec(Rec("/small.mp4", premium_path="/big.mp4", duration=9.5))
    assert p.engine.calls[-1] == ("load", "/big.mp4", 9.5, 60.0)


def test_the_probe_refines_the_length_without_a_reload():
    p = inline_player()
    p.tab.cfg.player_prefer_premium = True
    rec = Rec("/small.mp4", premium_path="/big.mp4", duration=9.5)
    p.show_rec(rec)
    p.engine.calls.clear()
    p.engine.playing = True
    p._premium_ready(rec, "/big.mp4", (9.52, 60.0))
    assert p.engine.calls == [], "a reload would stop the clip"
    assert p.engine.duration == 9.52


def test_without_4k_preferred_the_original_plays():
    p = inline_player()
    p.show_rec(Rec("/small.mp4", premium_path="/big.mp4", duration=9.5, fps=24.0))
    assert p.engine.calls[-1] == ("load", "/small.mp4", 9.5, 24.0)


# ── Up/Down play what they land on ──────────────────────────────────────

class Player:
    def __init__(self):
        self.playing = False
        self.plays = 0

    def play(self):
        self.plays += 1
        self.playing = True


def test_the_clip_the_arrows_rest_on_plays():
    tab = type("T", (), {})()
    tab.player, tab.selected, tab._autoplay_job = Player(), "clip", "job"
    LibraryTab._autoplay(tab, "clip")
    assert tab.player.plays == 1


def test_a_clip_passed_on_the_way_does_not():
    tab = type("T", (), {})()
    tab.player, tab.selected, tab._autoplay_job = Player(), "next clip", "job"
    LibraryTab._autoplay(tab, "clip")
    assert tab.player.plays == 0


def test_a_clip_already_playing_is_not_restarted():
    tab = type("T", (), {})()
    tab.player, tab.selected, tab._autoplay_job = Player(), "clip", "job"
    tab.player.playing = True
    LibraryTab._autoplay(tab, "clip")
    assert tab.player.plays == 0


# ── VLC resumes where it was ────────────────────────────────────────────

class Media:
    def __init__(self):
        self.options = []

    def add_option(self, option):
        self.options.append(option)

    def release(self):
        pass


class Instance:
    def __init__(self):
        self.made = []

    def media_new_path(self, path):
        media = Media()
        self.made.append(media)
        return media


class VlcSide:
    def __init__(self, state):
        self.state = state
        self.played = 0
        self.times = []

    def get_state(self):
        return self.state

    def set_media(self, media):
        pass

    def play(self):
        self.played += 1

    def set_time(self, ms):
        self.times.append(ms)


def test_reopening_to_resume_opens_at_the_position():
    import threading
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        p = vp.VlcPlayer(widget=None, width=854, height=480)
    finally:
        threading.Thread.start = real_start
    states = type("S", (), {"Ended": "ended", "Stopped": "stopped",
                            "NothingSpecial": "nothing"})
    p._vlc = type("V", (), {"State": states})
    p._instance = Instance()
    p._player = VlcSide("stopped")
    p.path, p.position, p.playing = "/clip.mp4", 7.25, True
    p._apply(("play",))
    assert p._player.played == 1
    assert "start-time=7.250" in p._instance.made[-1].options
    assert p._player.times == [], "set_time before the file is open is ignored"
