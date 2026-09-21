"""Verification must not be allowed to delete a good encode.

Every conversion is decoded end to end afterwards to catch a truncated
file, and that check had a flat two-minute limit whatever the clip was.
Decoding 4K/60 is not free and is not always faster than real time -
measured on this machine, a thirty-second 4K/60 file decodes in 12s with
every core free and in 46s, half again longer than the clip itself,
pinned to one core. One core is the realistic case: the machine is
usually encoding the next file, upscaling in another program, or editing
at the same time.

So any result that took over two minutes to decode was declared corrupt.
The consequence was not a warning: convert() deletes the output of a
failed attempt and falls back to the CPU encoder, which produces a file
that fails exactly the same way. An hour of encoding spent, the good file
deleted, and nothing at the end of it - for a clip that was never broken.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import convert_engine as ce                     # noqa: E402


# ── the budget ─────────────────────────────────────────────────────────

def test_a_short_clip_keeps_the_old_allowance():
    assert ce.verify_budget(20) == ce.VERIFY_FLOOR


def test_a_long_clip_gets_more_time_than_it_is_long():
    """The whole point: the limit has to be above the clip's own length,
    because a loaded machine decodes slower than real time."""
    for duration in (180, 300, 900):
        assert ce.verify_budget(duration) > duration


def test_the_five_minute_clip_that_used_to_fail():
    """Five minutes of 4K/60 needs about two minutes of decoding on an
    idle machine and over seven on a busy one. The old flat 120s sat
    right in between."""
    assert ce.verify_budget(300) > 120 * 2


def test_a_missing_duration_falls_back_to_the_floor():
    for unknown in (0, None, 0.0):
        assert ce.verify_budget(unknown) == ce.VERIFY_FLOOR


def test_the_budget_honours_a_ceiling():
    assert ce.verify_budget(100000, ceiling=3600) == 3600


def test_a_ceiling_can_never_cut_below_the_floor():
    """A tiny hard_timeout must not make verification fail instantly on
    every clip."""
    assert ce.verify_budget(600, ceiling=5) == ce.VERIFY_FLOOR


def test_no_ceiling_means_no_ceiling():
    assert ce.verify_budget(600, ceiling=0) == ce.verify_budget(600)


# ── a check that could not be run is not a verdict ─────────────────────

def test_a_timeout_is_raised_not_reported_as_corruption(monkeypatch):
    def slow(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)

    monkeypatch.setattr(ce.subprocess, "run", slow)
    with pytest.raises(ce.Unverifiable):
        ce.verify("clip.mp4", 120)


def test_ffmpeg_not_being_there_is_raised_too(monkeypatch):
    def missing(*_a, **_kw):
        raise OSError("No such file or directory: 'ffmpeg'")

    monkeypatch.setattr(ce.subprocess, "run", missing)
    with pytest.raises(ce.Unverifiable):
        ce.verify("clip.mp4")


def test_a_file_ffmpeg_says_is_broken_still_fails_normally(monkeypatch):
    class Result:
        returncode = 1
        stderr = "moov atom not found"

    monkeypatch.setattr(ce.subprocess, "run", lambda *a, **kw: Result())
    ok, error = ce.verify("clip.mp4")
    assert ok is False
    assert error


def test_a_file_that_decodes_passes(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(ce.subprocess, "run", lambda *a, **kw: Result())
    assert ce.verify("clip.mp4") == (True, None)


# ── what convert() does with each of those outcomes ────────────────────

@pytest.fixture
def fake_encode(monkeypatch):
    """convert(), with everything but the verification step stubbed."""
    from paz_suite import media

    class Recipe:
        duration = 300.0
        notes = ()
        snap_to = 0
        gop = 0
        cfr = False

    state = {"discarded": [], "encodes": 0, "logged": []}
    monkeypatch.setattr(media, "probe", lambda _p: None)
    monkeypatch.setattr(ce, "plan_recipe", lambda *_a: Recipe())
    monkeypatch.setattr(ce, "video_args", lambda _cfg, gpu=False: ["-c:v", "x"])
    monkeypatch.setattr(ce, "build_command", lambda *_a, **_kw: ["ffmpeg"])
    monkeypatch.setattr(ce, "_discard", lambda p: state["discarded"].append(p))

    def encoded(*_a, **_kw):
        state["encodes"] += 1
        return True, None

    monkeypatch.setattr(ce, "run_ffmpeg", encoded)
    return state


def config():
    from paz_suite.config import AppConfig
    cfg = AppConfig()
    cfg.verify_output = True
    cfg.use_gpu = False
    return cfg


def test_an_encode_that_cannot_be_verified_is_kept(fake_encode, monkeypatch):
    """The bug this file is about. The file is on disk and it is almost
    certainly fine; deleting it and re-encoding is the expensive way to
    end up with nothing."""
    def cannot(*_a, **_kw):
        raise ce.Unverifiable("could not decode it within 2m")

    monkeypatch.setattr(ce, "verify", cannot)
    ok, error, _label = ce.convert(
        "in.mp4", "out.mp4", config(),
        log=lambda m, level="info": fake_encode["logged"].append((level, m)))
    assert ok is True
    assert error is None
    assert fake_encode["discarded"] == [], "deleted a good encode"
    assert fake_encode["encodes"] == 1, "re-ran the encode for no reason"
    assert any(level == "warn" and "not verified" in message
               for level, message in fake_encode["logged"]), \
        "kept the file without telling anyone it was unchecked"


def test_an_encode_ffmpeg_calls_broken_is_still_thrown_away(fake_encode,
                                                            monkeypatch):
    monkeypatch.setattr(ce, "verify",
                        lambda *a, **kw: (False, "moov atom not found"))
    ok, error, _label = ce.convert("in.mp4", "out.mp4", config())
    assert ok is False
    assert "verification" in (error or "")
    assert fake_encode["discarded"] == ["out.mp4"]


def test_the_budget_handed_to_verify_comes_from_the_clip(fake_encode,
                                                          monkeypatch):
    seen = {}

    def watched(path, timeout=ce.VERIFY_FLOOR):
        seen["timeout"] = timeout
        return True, None

    monkeypatch.setattr(ce, "verify", watched)
    ce.convert("in.mp4", "out.mp4", config())
    assert seen["timeout"] == ce.verify_budget(300.0, config().hard_timeout)
