"""Windows-only behaviour, and the library walk that leans on Windows.

The sync's walk now takes each file's size and date from the directory
listing (free on Windows) instead of asking again per file. It has to
find exactly the files, sizes and dates the os.walk version found - the
paths are the library's keys - so the old walk is kept here verbatim.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import winsys                                # noqa: E402
from paz_suite.files import in_ignored_path, media_files, prune_dirs   # noqa: E402

EXT = {".mp4", ".webm"}


def old_walk(directory, recursive):
    on_disk = {}

    def take(path, root=""):
        if os.path.splitext(path)[1].lower() not in EXT:
            return
        if in_ignored_path(path, root):
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        on_disk[path] = (st.st_size, int(st.st_mtime))

    if recursive:
        for base, dirs, names in os.walk(directory):
            prune_dirs(dirs)
            for name in names:
                take(os.path.join(base, name), directory)
    else:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file():
                        take(entry.path)
        except OSError:
            pass
    return on_disk


def build(root):
    def put(rel, size=3):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    put("a.mp4", 5)
    put("B.MP4", 7)
    put("notes.txt")
    put("wolves/1.webm", 11)
    put("wolves/deep/2.mp4", 13)
    put("wolves/Proxies/1.mp4")               # a proxy - never indexed
    put("wolves/deep/mov/3.mp4")              # a proxy folder, deeper
    put(".hidden/4.mp4")
    (root / "folder.mp4").mkdir()             # a folder with a media name
    if hasattr(os, "symlink"):
        try:
            os.symlink(root / "wolves", root / "linked_dir", target_is_directory=True)
            os.symlink(root / "a.mp4", root / "link.mp4")
            os.symlink(root / "gone.mp4", root / "broken.mp4")
        except (OSError, NotImplementedError):
            pass                              # Windows without the privilege


@pytest.mark.parametrize("recursive", [True, False])
def test_the_walk_finds_what_os_walk_found(tmp_path, recursive):
    build(tmp_path)
    new = {p: (s, m) for p, s, m in media_files(str(tmp_path), EXT, recursive)}
    assert new == old_walk(str(tmp_path), recursive)


def test_what_it_finds(tmp_path):
    build(tmp_path)
    found = {os.path.relpath(p, tmp_path).replace("\\", "/"): s
             for p, s, _m in media_files(str(tmp_path), EXT, True)}
    assert found["a.mp4"] == 5 and found["B.MP4"] == 7
    assert found["wolves/deep/2.mp4"] == 13
    assert not any("Proxies" in p or "/mov/" in p or ".hidden" in p for p in found)
    assert "linked_dir/1.webm" not in found, "followed a symlinked folder"


def test_a_missing_folder_is_nothing_not_an_error(tmp_path):
    assert list(media_files(str(tmp_path / "nope"), EXT, True)) == []


# ── keeping the PC awake ───────────────────────────────────────────────

def test_keep_awake_pings_while_held_and_stops_after():
    pings = []
    keep = winsys.KeepAwake(ping=lambda: pings.append(time.monotonic()))
    keep.PING_SECONDS = 0.02
    keep.hold("convert")
    keep.hold("sync")
    time.sleep(0.12)
    assert keep.held and len(pings) >= 3
    keep.release("convert")
    assert keep.held, "one job ending let go of another's hold"
    keep.release("sync")
    time.sleep(0.08)
    assert not keep.held
    assert keep._thread is None, "the pinging thread outlived the last job"
    settled = len(pings)
    time.sleep(0.08)
    assert len(pings) == settled


def test_a_job_can_hold_the_same_reason_twice():
    keep = winsys.KeepAwake(ping=lambda: None)
    keep.hold("tag fetch")
    keep.hold("tag fetch")
    keep.release("tag fetch")
    assert keep.held
    keep.release("tag fetch")
    assert not keep.held


def test_releasing_from_another_thread_works():
    keep = winsys.KeepAwake(ping=lambda: None)
    keep.hold("sync")
    worker = threading.Thread(target=keep.release, args=("sync",))
    worker.start()
    worker.join()
    assert not keep.held


@pytest.mark.skipif(os.name == "nt", reason="checks the non-Windows no-ops")
def test_the_windows_calls_do_nothing_elsewhere():
    assert winsys.set_app_id() is False
    assert winsys.sharpen_timers() is False
    assert winsys.raise_ui_thread() is False
    winsys.restore_timers()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_the_windows_calls_succeed_on_windows():
    assert winsys.set_app_id() is True
    assert winsys.sharpen_timers() is True
    winsys.restore_timers()
    assert winsys.raise_ui_thread() is True
