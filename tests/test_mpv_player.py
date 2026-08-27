"""Cover for the mpv backend that does not need a display.

The parts worth pinning here are the ones that decide *what* mpv is told
to do — the command line it is started with, and the JSON that goes down
the IPC channel. Whether mpv then draws a picture is mpv's business and
can only be checked against a real one; tests/…/verify drives that.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import mpv_player as mp     # noqa: E402


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data.decode())

    def close(self):
        pass


@pytest.fixture
def player():
    p = mp.MpvPlayer.__new__(mp.MpvPlayer)
    p.__init__(widget=None, width=854, height=480)
    p._sock = FakeSocket()
    return p


def commands(player):
    return [json.loads(line)["command"] for line in player._sock.sent]


def test_the_command_line_never_asks_for_low_latency(player, monkeypatch):
    """--profile=low-latency sets vd-lavc-threads=1, which is the exact
    opposite of what a 4K file needs - it halved playback speed."""
    seen = {}

    class FakeProc:
        def poll(self): return None

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    class FakeWidget:
        def winfo_id(self): return 12345

    monkeypatch.setattr(mp.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mp, "mpv_path", lambda: "/usr/bin/mpv")
    monkeypatch.setattr(mp.MpvPlayer, "_connect", lambda self, timeout=10.0: False)
    player.widget = FakeWidget()
    player._spawn()               # the synchronous half of _start

    cmd = seen["cmd"]
    assert not any("low-latency" in part for part in cmd)
    assert "--hwdec=auto-safe" in cmd
    assert "--wid=12345" in cmd
    assert "--idle=yes" in cmd
    assert any(part.startswith("--input-ipc-server=") for part in cmd)


def test_the_video_output_override_is_passed_through(player, monkeypatch):
    """It has to apply however mpv comes to be started - load() gets there
    before play() does, and the override used to be lost on that path."""
    seen = {}

    class FakeProc:
        def poll(self): return None

    monkeypatch.setattr(mp.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), FakeProc())[1])
    monkeypatch.setattr(mp, "mpv_path", lambda: "/usr/bin/mpv")
    monkeypatch.setattr(mp.MpvPlayer, "_connect", lambda self, timeout=10.0: False)

    class FakeWidget:
        def winfo_id(self): return 1
    player.widget = FakeWidget()
    player.vo = "x11"
    player._spawn()                       # no argument, as load() leads to
    assert "--vo=x11" in seen["cmd"]


def test_seek_is_absolute_and_clamped(player):
    player.duration = 10.0
    player.seek(4.5)
    player.seek(-3)
    player.seek(999)
    sent = commands(player)
    assert sent[0] == ["seek", 4.5, "absolute+exact"]
    assert sent[1][1] == 0.0
    assert sent[2][1] == pytest.approx(9.95)


def test_transport_maps_onto_properties(player):
    player.play = lambda: None            # play() would start a process
    player.pause()
    player.set_volume(55)
    player.toggle_mute()
    player.toggle_loop()
    player.set_speed(2.0)
    sent = commands(player)
    assert ["set_property", "pause", True] in sent
    assert ["set_property", "volume", 55] in sent
    assert ["set_property", "mute", True] in sent
    assert ["set_property", "loop-file", "no"] in sent
    assert ["set_property", "speed", 2.0] in sent


def test_a_property_change_moves_the_clock_and_reports_state(player):
    seen = {"pos": [], "state": []}
    player.on_tick = seen["pos"].append
    player.on_state = seen["state"].append
    player._post = lambda fn, *a: fn(*a)

    player._handle({"event": "property-change", "name": "duration", "data": 42.0})
    player._handle({"event": "property-change", "name": "time-pos", "data": 3.5})
    player._handle({"event": "property-change", "name": "pause", "data": False})

    assert player.duration == 42.0
    assert player.position == 3.5
    assert seen["pos"] == [3.5]
    assert seen["state"] == [True]


def test_eof_only_ends_playback_when_not_looping(player):
    ended = []
    player.on_eof = lambda: ended.append(True)
    player._post = lambda fn, *a: fn(*a)

    player.loop = True
    player._handle({"event": "property-change", "name": "eof-reached", "data": True})
    assert ended == []

    player.loop = False
    player._handle({"event": "property-change", "name": "eof-reached", "data": True})
    assert ended == [True]


def test_it_matches_the_interface_the_player_holds():
    """InlinePlayer swaps between this and ClipPlayer without asking which
    it has, so anything it touches has to exist on both."""
    from paz_suite.player_engine import ClipPlayer
    used = ("clear", "duration", "load", "loop", "muted", "nudge", "path",
            "pause", "play", "playing", "position", "seek", "set_size",
            "set_volume", "stop", "toggle", "toggle_loop", "toggle_mute",
            "view_h", "view_w", "volume")
    for name in used:
        assert hasattr(mp.MpvPlayer, name) or name in mp.MpvPlayer.__init__.__code__.co_names, name
        assert hasattr(ClipPlayer, name) or name in ClipPlayer.__init__.__code__.co_names, name


# ── the freeze ──────────────────────────────────────────────────────────
#
# _connect used to sleep on the calling thread for up to ten seconds
# waiting for an IPC endpoint. That thread is the UI thread, so a named
# pipe that never appeared froze the whole window every time a clip was
# clicked. Nothing about starting mpv may block the caller now.

def test_starting_mpv_never_blocks_the_caller(player, monkeypatch):
    import time as _t

    class NeverReadyProc:
        def poll(self): return None

    monkeypatch.setattr(mp, "mpv_path", lambda: "/usr/bin/mpv")
    monkeypatch.setattr(mp.subprocess, "Popen", lambda *a, **k: NeverReadyProc())
    # A connect that never succeeds - the exact case that used to hang.
    monkeypatch.setattr(mp.MpvPlayer, "_connect",
                        lambda self, timeout=6.0: (_t.sleep(1.0), False)[1])

    class FakeWidget:
        def winfo_id(self): return 1
    player.widget = FakeWidget()
    player._sock = None

    started = _t.monotonic()
    player._start()
    assert (_t.monotonic() - started) < 0.1, "start() blocked the caller"


def test_a_failed_start_reports_back_instead_of_hanging(player, monkeypatch):
    import time as _t
    told = []
    player.on_unavailable = told.append
    player._post = lambda fn, *a: fn(*a)

    monkeypatch.setattr(mp, "mpv_path", lambda: "/usr/bin/mpv")
    monkeypatch.setattr(mp.MpvPlayer, "_spawn", lambda self, vo=None: False)
    player._start_blocking()
    assert told and "built-in" in told[0]


def test_commands_issued_before_it_is_up_are_not_lost(player):
    player._sock = None
    player._starting = True
    player._send("set_property", "pause", False)
    player._send("seek", 3.0, "absolute+exact")
    assert len(player._queued) == 2

    sock = FakeSocket()
    player._sock = sock
    player._starting = False
    player._flush_pending()
    sent = [json.loads(line)["command"] for line in sock.sent]
    assert ["set_property", "pause", False] in sent
    assert ["seek", 3.0, "absolute+exact"] in sent
    assert player._queued == []
