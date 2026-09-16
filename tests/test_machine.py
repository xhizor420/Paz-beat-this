"""Remembered answers about the machine, and when they stop being true.

Two questions cost a quarter of a second each at startup and were the
last two entries in the stutter log: which encoders this ffmpeg has, and
where libVLC is. Neither answer changes between launches unless the tool
does - so they are remembered.

The whole risk of that is a cache that lies. An ffmpeg that gained
hardware encoders after a driver update, a VLC that was uninstalled: if
the stored answer survives those, the app is confidently wrong about
what it can do, which is worse than being slow. So every test here is
about when a remembered answer must be thrown away.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import machine                             # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(machine, "CACHE_PATH", str(tmp_path / "machine.json"))
    monkeypatch.setattr(machine, "_data", None)
    return tmp_path / "machine.json"


# ── remembering ─────────────────────────────────────────────────────────

def test_an_answer_comes_back(store):
    machine.remember("encoders", "ffmpeg-v6", ["libx264"])
    assert machine.remembered("encoders", "ffmpeg-v6") == ["libx264"]


def test_nothing_stored_means_ask(store):
    assert machine.remembered("encoders", "ffmpeg-v6") is None


def test_an_answer_survives_the_process(store):
    machine.remember("encoders", "ffmpeg-v6", ["libx264"])
    machine._data = None                        # a new launch
    assert machine.remembered("encoders", "ffmpeg-v6") == ["libx264"]


def test_two_answers_do_not_collide(store):
    machine.remember("encoders", "a", ["x264"])
    machine.remember("vlc", "b", "/usr/lib/libvlc.so")
    assert machine.remembered("encoders", "a") == ["x264"]
    assert machine.remembered("vlc", "b") == "/usr/lib/libvlc.so"


def test_storing_again_replaces(store):
    machine.remember("encoders", "a", ["old"])
    machine.remember("encoders", "a", ["new"])
    assert machine.remembered("encoders", "a") == ["new"]


# ── and forgetting, which is the part that matters ─────────────────────

def test_a_different_install_is_not_answered_from_the_old_one(store):
    """The whole point of the stamp: upgrade ffmpeg and the question has
    to be asked again."""
    machine.remember("encoders", "ffmpeg-v6", ["libx264"])
    assert machine.remembered("encoders", "ffmpeg-v7") is None


def test_forget_drops_everything(store):
    machine.remember("encoders", "a", ["x264"])
    machine.forget()
    assert machine.remembered("encoders", "a") is None
    assert not store.exists()


# ── the stamp identifies an install ────────────────────────────────────

def test_a_tools_stamp_changes_when_the_tool_does(tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"x" * 100)
    first = machine.tool_stamp(str(exe))
    exe.write_bytes(b"x" * 200)                 # upgraded in place
    assert machine.tool_stamp(str(exe)) != first


def test_the_same_tool_stamps_the_same(tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"x" * 100)
    assert machine.tool_stamp(str(exe)) == machine.tool_stamp(str(exe))


def test_a_tool_somewhere_else_is_a_different_tool(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for folder in ("a", "b"):
        (tmp_path / folder / "ffmpeg").write_bytes(b"x" * 100)
    assert (machine.tool_stamp(str(tmp_path / "a" / "ffmpeg"))
            != machine.tool_stamp(str(tmp_path / "b" / "ffmpeg")))


def test_a_missing_tool_stamps_as_missing(tmp_path):
    assert machine.tool_stamp(str(tmp_path / "nope")) == "none"
    assert machine.tool_stamp("") == "none"


def test_a_tool_that_appears_is_noticed(tmp_path, monkeypatch):
    """Answered "not installed" once, then the user installs it. The
    stamp has to change, or the app never notices."""
    exe = tmp_path / "ffmpeg"
    missing = machine.tool_stamp(str(exe))
    exe.write_bytes(b"x" * 10)
    assert machine.tool_stamp(str(exe)) != missing


# ── it must never be the reason the app breaks ─────────────────────────

def test_a_corrupt_store_reads_as_empty(store):
    store.write_text("this is not json", encoding="utf-8")
    machine._data = None
    assert machine.remembered("encoders", "a") is None


def test_a_store_that_is_not_a_dict_reads_as_empty(store):
    store.write_text(json.dumps(["surprise"]), encoding="utf-8")
    machine._data = None
    assert machine.remembered("encoders", "a") is None


def test_junk_under_a_name_reads_as_ask_again(store):
    store.write_text(json.dumps({"encoders": "not a record"}), encoding="utf-8")
    machine._data = None
    assert machine.remembered("encoders", "a") is None


def test_an_unwritable_folder_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "CONFIG_DIR", str(tmp_path / "\0bad"))
    monkeypatch.setattr(machine, "CACHE_PATH", str(tmp_path / "\0bad" / "m.json"))
    monkeypatch.setattr(machine, "_data", None)
    machine.remember("encoders", "a", ["x264"])      # must not raise
    # Still answers from memory for this process, having failed to store.
    assert machine.remembered("encoders", "a") == ["x264"]


def test_a_stamp_survives_the_json_round_trip(store):
    """Stamps are compared after being read back, so they have to be a
    type JSON returns unchanged - a tuple would come back a list and
    never match again."""
    machine.remember("encoders", ("a", 1), ["x264"])
    machine._data = None
    assert machine.remembered("encoders", ("a", 1)) == ["x264"]
