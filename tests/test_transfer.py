"""Sorting into the pools never leaves a short clip under its real name."""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import convert_engine as ce  # noqa: E402


@pytest.mark.parametrize("mode", ["copy", "move", "hardlink"])
def test_each_mode_puts_the_whole_file_in_place(tmp_path, mode):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1000)
    dest = tmp_path / "out" / "a.mp4"
    dest.parent.mkdir()
    ce.transfer(str(src), str(dest), mode)
    assert dest.read_bytes() == b"x" * 1000
    assert not os.path.exists(str(dest) + ".partial")


def test_a_copy_cut_off_halfway_leaves_nothing_under_the_real_name(tmp_path, monkeypatch):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1000)
    dest = tmp_path / "b.mp4"

    def dies(a, b):
        with open(b, "wb") as fh:
            fh.write(b"x" * 10)
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", dies)
    with pytest.raises(OSError):
        ce.transfer(str(src), str(dest), "copy")
    assert not dest.exists()
    assert not os.path.exists(str(dest) + ".partial")
    assert src.exists()
