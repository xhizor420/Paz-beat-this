"""Clips are read ahead so they open from memory, not the library drive.

The rules that keep that from costing anything: the newest request wins,
a file read recently is not read again, and a read nobody wants any more
is dropped part-way.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import prefetch   # noqa: E402


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(prefetch, "_warm", prefetch.collections.OrderedDict())
    monkeypatch.setattr(prefetch, "_stats", {"files": 0, "bytes": 0, "abandoned": 0})
    prefetch.want([])
    yield
    prefetch.want([])


def wait_for(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def clip(tmp_path, name, size=3 << 20):
    path = tmp_path / name
    path.write_bytes(b"\0" * size)
    return str(path)


def test_asked_for_files_are_read_through(tmp_path):
    a, b = clip(tmp_path, "a.mp4"), clip(tmp_path, "b.mp4")
    prefetch.want([a, b])
    assert wait_for(lambda: prefetch.stats()["files"] == 2)
    assert prefetch.stats()["bytes"] == 2 * (3 << 20)


def test_a_file_read_recently_is_not_read_again(tmp_path):
    a = clip(tmp_path, "a.mp4")
    prefetch.want([a])
    assert wait_for(lambda: prefetch.stats()["files"] == 1)
    prefetch.want([a])
    time.sleep(0.2)
    assert prefetch.stats()["files"] == 1


def test_a_changed_file_is_read_again(tmp_path):
    a = clip(tmp_path, "a.mp4")
    prefetch.want([a])
    assert wait_for(lambda: prefetch.stats()["files"] == 1)
    with open(a, "ab") as fh:
        fh.write(b"more")
    prefetch.want([a])
    assert wait_for(lambda: prefetch.stats()["files"] == 2)


def test_moving_on_abandons_a_read_part_way(tmp_path, monkeypatch):
    """Arrowing past a clip must not finish reading it first."""
    monkeypatch.setattr(prefetch, "CHUNK", 64 << 10)
    big = clip(tmp_path, "big.mp4", 64 << 20)
    small = clip(tmp_path, "small.mp4")
    prefetch.want([big])
    time.sleep(0.01)
    prefetch.want([small])
    assert wait_for(lambda: prefetch.stats()["files"] >= 1)
    assert prefetch.stats()["abandoned"] == 1 or prefetch.stats()["files"] == 2


def test_missing_files_are_skipped_quietly(tmp_path):
    a = clip(tmp_path, "a.mp4")
    prefetch.want([str(tmp_path / "gone.mp4"), "", a])
    assert wait_for(lambda: prefetch.stats()["files"] == 1)
