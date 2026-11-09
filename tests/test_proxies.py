"""Edit proxies for the 4K pool.

What matters: a proxy has exactly its master's frames at its master's
frame rate; one that does not is never put in place; a run never redoes a
proxy already made; and a pool that is being written to while a run goes
on does not end up with a proxy of half a file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import library_db as db           # noqa: E402
from paz_suite import proxy_engine as pe         # noqa: E402

OLD = time.time() - 3600
REAL_ARRIVED = pe.arrived


@pytest.fixture(autouse=True)
def _arrival_is_mtime(monkeypatch):
    # The tests age files with os.utime, which cannot move a creation time.
    monkeypatch.setattr(pe, "arrived", lambda stat: stat.st_mtime)


def test_a_copy_that_kept_an_old_mtime_still_counts_as_new(tmp_path):
    path = touch(str(tmp_path / "a.mp4"))           # mtime an hour ago
    if os.name == "nt":
        assert time.time() - REAL_ARRIVED(os.stat(path)) < 60


def test_a_master_whose_frames_cannot_be_counted_never_passes():
    master = pe.Stream(frames=0, rate="60/1", duration=10.0)
    assert pe.matches(master, pe.Stream(frames=600, rate="60/1", duration=10.0))


def touch(path, data=b"x" * 10, mtime=OLD):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def pool(tmp_path):
    root = tmp_path / "pool"
    for name in ("1.mp4", "2.mp4", "3.mp4", "4.mp4", "5.mp4", "6.mp4", "7.mp4"):
        touch(str(root / "Furry" / name))
    touch(str(root / "Furry" / "notes.txt"))
    touch(str(root / "Furry" / "New folder" / "8.mp4"))      # not a master
    touch(str(root / "MLP" / "1.mp4"))                       # same name, other category
    return root


def row_for(path, status=pe.DONE, proxy_size=10, reason=""):
    stat = os.stat(path)
    return {"master_size": stat.st_size, "master_mtime": int(stat.st_mtime),
            "proxy_size": proxy_size, "status": status, "reason": reason,
            "recipe": pe.RECIPE if status == pe.DONE else ""}


# ── where a proxy goes ───────────────────────────────────────────────────

def test_a_proxy_lives_in_its_categorys_own_proxy_folder():
    master = os.path.join("P:", os.sep, "pool", "Furry", "4182933.mp4")
    assert pe.proxy_path(master) == os.path.join("P:", os.sep, "pool", "Furry", "Proxy",
                                                 "4182933.mov")
    assert pe.partial_path(pe.proxy_path(master)).endswith("4182933.partial.mov")


# ── scanning ─────────────────────────────────────────────────────────────

def test_scan_sorts_every_master_by_where_its_proxy_stands(pool):
    furry = pool / "Furry"
    proxy = furry / "Proxy"
    touch(str(proxy / "2.mov"))                                  # made and remembered
    touch(str(proxy / "3.mov"))                                  # made, 4K changed since
    touch(str(proxy / "4.mov"))                                  # not made by us
    touch(str(proxy / "5.mov"), data=b"")                        # empty
    touch(str(proxy / "9.partial.mov"))                          # an encode cut off
    known = {str(furry / "2.mp4"): row_for(str(furry / "2.mp4")),
             str(furry / "3.mp4"): dict(row_for(str(furry / "3.mp4")), master_size=1),
             str(furry / "6.mp4"): row_for(str(furry / "6.mp4"), pe.FAILED, 0, "boom")}
    touch(str(furry / "7.mp4"), mtime=time.time())               # still arriving
    items, partials, missing = pe.scan(str(pool), ["Furry", "MLP", "Gone"], known)
    state = {(i.category, i.name): i.state for i in items}
    assert state == {
        ("Furry", "1.mp4"): pe.NEEDED, ("Furry", "2.mp4"): pe.DONE,
        ("Furry", "3.mp4"): pe.STALE, ("Furry", "4.mp4"): pe.CHECK,
        ("Furry", "5.mp4"): pe.BAD, ("Furry", "6.mp4"): pe.FAILED,
        ("Furry", "7.mp4"): pe.WAITING, ("MLP", "1.mp4"): pe.NEEDED}
    assert partials == [str(proxy / "9.partial.mov")]
    assert missing == [str(pool / "Gone")]
    mlp = next(i for i in items if i.category == "MLP")
    assert mlp.proxy == str(pool / "MLP" / "Proxy" / "1.mov")


def test_a_remembered_proxy_that_was_replaced_is_checked_again(pool):
    furry = pool / "Furry"
    touch(str(furry / "Proxy" / "1.mov"), data=b"y" * 99)
    known = {str(furry / "1.mp4"): row_for(str(furry / "1.mp4"), proxy_size=10)}
    items, _p, _m = pe.scan(str(pool), ["Furry"], known)
    assert next(i for i in items if i.name == "1.mp4").state == pe.CHECK


def test_scan_never_opens_a_file(pool, monkeypatch):
    monkeypatch.setattr(pe.subprocess, "run", lambda *a, **k: pytest.fail("probed"))
    pe.scan(str(pool), ["Furry", "MLP"], {})


# ── what counts as the same frames ──────────────────────────────────────

def test_matches_wants_the_same_frames_rate_and_length():
    master = pe.Stream(frames=600, rate="60/1", duration=10.0, video_duration=True)
    assert pe.matches(master, pe.Stream(frames=600, rate="60/1", duration=10.0)) == ""
    assert pe.matches(master, pe.Stream(frames=600, rate="60/1", duration=10.02)) == ""
    assert "frame rate" in pe.matches(master, pe.Stream(frames=600, rate="30/1",
                                                         duration=10.0))
    assert "599 frames" in pe.matches(master, pe.Stream(frames=599, rate="60/1",
                                                         duration=10.0))
    assert "long" in pe.matches(master, pe.Stream(frames=600, rate="60/1", duration=10.5))
    assert pe.matches(master, None)


def test_a_file_length_that_counts_the_audio_is_not_held_against_the_video():
    # 4036406: no video length stored, so the file's 5.695s is the audio's;
    # the video - and its proxy - end at 5.617s, 337 frames at 60.
    master = pe.Stream(frames=337, rate="60/1", duration=5.695, video_duration=False)
    assert pe.matches(master, pe.Stream(frames=337, rate="60/1", duration=5.617)) == ""


def test_a_packet_that_never_decodes_is_not_a_missing_frame(monkeypatch, tmp_path):
    # 4006811: 540 packets, 539 of them decode. The proxy has 539.
    rig = Rig(monkeypatch, tmp_path, proxy_frames=539)
    monkeypatch.setattr(pe, "master_stream", lambda path: pe.Stream(
        frames=540, rate="60/1", duration=9.03, indexed=False))
    monkeypatch.setattr(pe, "decoded_frames", lambda path: 539)
    ok, why, _made = pe.make_proxy(rig.item, cfg=None)
    assert ok, why


def test_a_frame_really_missing_still_fails_after_the_recount(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, proxy_frames=538)
    monkeypatch.setattr(pe, "master_stream", lambda path: pe.Stream(
        frames=540, rate="60/1", duration=9.03, indexed=False))
    monkeypatch.setattr(pe, "decoded_frames", lambda path: 539)
    ok, why, _made = pe.make_proxy(rig.item, cfg=None)
    assert not ok and "538 frames" in why
    assert rig.files() == []


# ── the command ──────────────────────────────────────────────────────────

def test_the_command_never_touches_timing_or_frames():
    cmd = pe.build_command("in.mp4", "out.partial.mov",
                           pe.Stream(frames=600, rate="60/1", has_audio=True,
                                     audio_codec="aac"))
    joined = " ".join(cmd)
    assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"
    for flag in ("-r", "-vsync", "-framerate", "-frames:v", "-t", "-ss"):
        assert flag not in cmd
    for filt in ("fps=", "minterpolate", "framestep", "select="):
        assert filt not in joined
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert cmd[cmd.index("-g") + 1] == "30" and cmd[cmd.index("-bf") + 1] == "0"
    assert "-maxrate" in cmd                           # CQ, not NVENC's own ceiling
    assert cmd[-1] == "out.partial.mov"


def test_gop_follows_the_clips_own_rate():
    cmd = pe.build_command("a", "b", pe.Stream(rate="72/1"))
    assert cmd[cmd.index("-g") + 1] == "36"


def test_audio_a_mov_cannot_hold_is_reencoded_and_no_audio_adds_none():
    opus = pe.build_command("a", "b", pe.Stream(rate="60/1", has_audio=True,
                                                audio_codec="opus"))
    assert opus[opus.index("-c:a") + 1] == "aac"
    silent = pe.build_command("a", "b", pe.Stream(rate="60/1"))
    assert "-c:a" not in silent


# ── making one: nothing broken is ever put in place ─────────────────────

class Rig:
    """make_proxy with ffmpeg, ffprobe and the decode check stood in for."""

    def __init__(self, monkeypatch, tmp_path, proxy_frames=600, decodes=True,
                 during=None):
        import paz_suite.convert_engine as ce
        self.master = touch(str(tmp_path / "Furry" / "1.mp4"))
        stat = os.stat(self.master)
        self.item = pe.Item("Furry", self.master, stat.st_size, int(stat.st_mtime),
                            pe.proxy_path(self.master))
        self.ran = []
        master_info = pe.Stream(frames=600, rate="60/1", duration=10.0)
        monkeypatch.setattr(pe, "master_stream", lambda path: master_info)
        monkeypatch.setattr(pe, "probe", lambda path, **k: pe.Stream(
            frames=proxy_frames, rate="60/1", duration=10.0))

        def run_ffmpeg(cmd, duration, cfg, progress_cb=None, cancel=None):
            self.ran.append(cmd)
            touch(cmd[-1], b"proxy")
            if during:
                during(self)
            if cancel is not None and cancel.is_set():
                raise ce.Cancelled()
            return True, None

        monkeypatch.setattr(ce, "run_ffmpeg", run_ffmpeg)
        monkeypatch.setattr(ce, "verify", lambda path, timeout=0: (decodes, "bad data"))

    def files(self):
        folder = os.path.dirname(self.item.proxy)
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def test_a_good_proxy_is_written_under_a_temporary_name_then_moved(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path)
    ok, why, made = pe.make_proxy(rig.item, cfg=None)
    assert ok and why == ""
    assert rig.ran[0][-1].endswith(".partial.mov")
    assert rig.files() == ["1.mov"]


def test_a_proxy_a_frame_short_is_thrown_away(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, proxy_frames=599)
    ok, why, _made = pe.make_proxy(rig.item, cfg=None)
    assert not ok and "599 frames" in why
    assert rig.files() == []


def test_a_proxy_that_does_not_decode_is_thrown_away(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path, decodes=False)
    ok, why, _made = pe.make_proxy(rig.item, cfg=None)
    assert not ok and "decode" in why
    assert rig.files() == []


def test_stopping_leaves_no_file_behind(monkeypatch, tmp_path):
    import paz_suite.convert_engine as ce
    cancel = threading.Event()
    cancel.set()
    rig = Rig(monkeypatch, tmp_path)
    with pytest.raises(ce.Cancelled):
        pe.make_proxy(rig.item, cfg=None, cancel=cancel)
    assert rig.files() == []


def test_a_master_rewritten_during_the_encode_loses_its_proxy(monkeypatch, tmp_path):
    def rewrite(rig):
        touch(rig.master, b"a longer file now", mtime=OLD + 5)

    rig = Rig(monkeypatch, tmp_path, during=rewrite)
    ok, why, _made = pe.make_proxy(rig.item, cfg=None)
    assert not ok and "changed" in why
    assert rig.files() == []


def test_a_master_still_arriving_is_left_for_later(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path)
    os.utime(rig.master, (time.time(), time.time()))
    ok, why, _made = pe.make_proxy(rig.item, cfg=None)
    assert not ok and why == pe.NOT_READY
    assert rig.ran == []


def test_leftover_partials_are_cleaned_and_nothing_else(tmp_path):
    keep = touch(str(tmp_path / "Proxy" / "1.mov"))
    gone = touch(str(tmp_path / "Proxy" / "2.partial.mov"))
    assert pe.clean_partials([keep, gone]) == 1
    assert os.path.exists(keep) and not os.path.exists(gone)


# ── remembering ─────────────────────────────────────────────────────────

def test_the_proxies_table_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "lib.sqlite3"))
    db.db_forget_schema()
    try:
        conn = db.db_connect()
        item = pe.Item("Furry", "/p/Furry/1.mp4", 100, 5, "/p/Furry/Proxy/1.mov")
        db.proxy_record(conn, item.master, pe.record_fields(
            item, pe.FAILED, "no good"))
        row = db.proxy_rows(conn)[item.master]
        conn.close()
    finally:
        db.db_forget_schema()
    assert row["status"] == pe.FAILED and row["reason"] == "no good"
    assert row["master_size"] == 100 and row["master_mtime"] == 5


# ── Resolve gets the proxy that was made for the clip ───────────────────

def test_resolve_is_handed_the_made_proxy_when_there_is_one(tmp_path):
    from paz_suite.library_tab import LibraryTab
    master = touch(str(tmp_path / "Furry" / "1.mp4"))
    rec = type("R", (), {"premium_path": master, "path": "/converted/1.mp4"})()
    assert LibraryTab._resolve_pair(None, rec) == (master, "/converted/1.mp4")
    touch(pe.proxy_path(master))
    assert LibraryTab._resolve_pair(None, rec) == (master, pe.proxy_path(master))


# ── the real thing, where the machine can ───────────────────────────────

def _nvenc() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i",
                              "testsrc2=size=256x144:rate=60:duration=0.2",
                              "-c:v", "h264_nvenc", "-f", "null", "-"],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


@pytest.mark.skipif(not _nvenc(), reason="needs ffmpeg with a working NVENC")
def test_a_real_proxy_has_every_frame(tmp_path):
    from paz_suite.config import AppConfig
    master = str(tmp_path / "Furry" / "1.mp4")
    os.makedirs(os.path.dirname(master))
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=2560x1440:rate=60:duration=2", "-f", "lavfi", "-i",
                    "sine=duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", master], check=True)
    os.utime(master, (OLD, OLD))
    stat = os.stat(master)
    item = pe.Item("Furry", master, stat.st_size, int(stat.st_mtime), pe.proxy_path(master))
    ok, why, made = pe.make_proxy(item, AppConfig())
    assert ok, why
    assert (made.width, made.height, made.rate, made.frames) == (1920, 1080, "60/1", 120)
    assert pe.probe(item.proxy).audio_codec == "aac"
