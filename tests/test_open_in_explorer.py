"""Show in folder must work for paths with spaces - "Need Upscale",
"Main MP4". Passed as a list, Python quoted the whole /select switch and
Explorer ignored it."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import files  # noqa: E402


@pytest.mark.skipif(os.name != "nt", reason="Explorer is Windows only")
def test_the_path_is_quoted_on_its_own(tmp_path, monkeypatch):
    clip = tmp_path / "Need Upscale" / "6689836.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(files.subprocess, "Popen", lambda cmd, *a, **k: seen.append(cmd))
    files.open_in_explorer(str(clip))
    assert seen == [f'explorer /select,"{os.path.normpath(str(clip))}"']
