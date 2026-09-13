"""Two clips must never play at once.

The app can now make sound in three places - the Library player, the
Vault player, and Convert's inspector - and they share one pair of
speakers. Two clips over each other is not a preview of either, and the
second one is playing on a page you may not even be looking at, with no
obvious way to find and stop it. So starting one stops the rest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.player_engine import claim_playback, joins_playback   # noqa: E402
from paz_suite.vault_tab import VaultTab                             # noqa: E402


class FakePlayer:
    def __init__(self, playing=False):
        self.playing = playing
        self.paused = 0

    def pause(self):
        self.playing = False
        self.paused += 1


class Wrapper:
    """A player that drives an engine, the way InlinePlayer does."""

    def __init__(self, engine):
        self.engine = engine

    @property
    def playing(self):
        return self.engine.playing

    def pause(self):
        self.engine.pause()


def test_starting_one_player_stops_the_others():
    a, b, c = FakePlayer(playing=True), FakePlayer(playing=True), FakePlayer()
    for p in (a, b, c):
        joins_playback(p)
    claim_playback(c)
    assert not a.playing and not b.playing
    assert a.paused == 1 and b.paused == 1


def test_a_player_does_not_stop_its_own_engine():
    """InlinePlayer registers itself AND holds a registered engine. Pausing
    that engine on the way into play() would stop the clip it is starting."""
    engine = FakePlayer(playing=True)
    wrapper = Wrapper(engine)
    joins_playback(engine)
    joins_playback(wrapper)
    claim_playback(wrapper)
    assert engine.paused == 0


def test_a_player_already_stopped_is_left_alone():
    idle, starting = FakePlayer(), FakePlayer()
    joins_playback(idle)
    joins_playback(starting)
    claim_playback(starting)
    assert idle.paused == 0


def test_a_player_that_throws_does_not_stop_the_one_starting():
    class Broken:
        @property
        def playing(self):
            raise RuntimeError("mid-teardown")

    other = FakePlayer(playing=True)
    joins_playback(Broken())
    joins_playback(other)
    claim_playback(None)
    assert other.paused == 1


def test_a_player_that_has_gone_away_drops_out_by_itself():
    import gc
    gone = FakePlayer(playing=True)
    joins_playback(gone)
    del gone
    gc.collect()
    claim_playback(None)          # must not raise on a dead reference


# ── the Vault's player ──────────────────────────────────────────────────

class FakeCaption:
    def __init__(self):
        self.text = None

    def configure(self, **kw):
        if "text" in kw:
            self.text = kw["text"]


class Rec:
    name = "4390562.mp4"
    width, height = 3840, 2160
    duration = 214.0
    artists = ["someartist"]


class FakeVault:
    _show_in_player = VaultTab._show_in_player
    on_hidden = VaultTab.on_hidden

    def __init__(self):
        self.player = FakePlayer()
        self.player.shown = []
        self.player.show_rec = self.player.shown.append
        self.player_caption = FakeCaption()


def test_picking_a_clip_puts_it_in_the_vault_player():
    vault = FakeVault()
    rec = Rec()
    vault._show_in_player(rec)
    assert vault.player.shown == [rec]
    assert "4390562.mp4" in vault.player_caption.text
    assert "3840x2160" in vault.player_caption.text


def test_picking_nothing_empties_the_vault_player():
    vault = FakeVault()
    vault._show_in_player(None)
    assert vault.player.shown == [None]
    assert "Pick a clip" in vault.player_caption.text


def test_leaving_the_vault_stops_its_player():
    vault = FakeVault()
    vault.player.playing = True
    vault.on_hidden()
    assert vault.player.paused == 1


def test_leaving_the_vault_before_it_has_a_player_is_harmless():
    vault = FakeVault()
    del vault.player
    vault.on_hidden()
