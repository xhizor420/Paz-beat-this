"""Clips that still need 4K 60, and the file drop that hands them to Topaz."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.config import AppConfig
from paz_suite.convert_engine import clips_needing_upscale
from paz_suite import filedrag


def test_a_folder_lists_only_clips_that_are_not_4k_60(tmp_path):
    out = tmp_path / "out"
    prem = tmp_path / "prem"
    folders = ["Furry", "MLP", "Furry Futa", "MLP Futa"]
    for folder in folders:
        (out / folder).mkdir(parents=True)
        (prem / folder).mkdir(parents=True)
    (out / "Furry" / "done.mp4").write_bytes(b"x")
    (prem / "Furry" / "done.mp4").write_bytes(b"x")
    (out / "Furry" / "short.mp4").write_bytes(b"x" * 2048)
    (out / "Furry" / "already.mp4").write_bytes(b"x")
    (out / "MLP" / "slow.mp4").write_bytes(b"x")
    cfg = AppConfig(
        output_root=str(out), premium_root=str(prem), subfolders=folders,
        min_height=2160, min_fps=60)
    details = {
        os.path.normcase(str(out / "Furry" / "short.mp4")): (1920, 1080, 30.0, 12.0),
        os.path.normcase(str(out / "Furry" / "already.mp4")): (3840, 2160, 60.0, 8.0),
        os.path.normcase(str(out / "MLP" / "slow.mp4")): (3840, 2160, 24.0, 5.0),
    }
    found = clips_needing_upscale(cfg, details)
    furry = {clip.name: clip for clip in found["Furry"]}
    assert "done.mp4" not in furry
    assert "already.mp4" not in furry
    assert furry["short.mp4"].need == "4K and 60"
    assert furry["short.mp4"].size == 2048
    assert [clip.need for clip in found["MLP"]] == ["60 fps"]
    assert found["Furry Futa"] == []
    assert found["MLP Futa"] == []


def test_a_file_drop_keeps_the_path(tmp_path):
    path = tmp_path / "6767467.mp4"
    path.write_bytes(b"x")
    handle = filedrag.hdrop_alloc([str(path)])
    assert handle
    try:
        assert filedrag.hdrop_paths(handle) == [os.path.abspath(str(path))]
    finally:
        filedrag._release_global(handle)


def test_get_data_hands_over_the_same_files(tmp_path):
    import ctypes
    path = tmp_path / "6767467.mp4"
    path.write_bytes(b"x")
    data = filedrag._Data([str(path)])
    fmt = filedrag.FORMATETC()
    fmt.cfFormat = filedrag.CF_HDROP
    fmt.dwAspect = filedrag.DVASPECT_CONTENT
    fmt.lindex = -1
    fmt.tymed = filedrag.TYMED_HGLOBAL
    medium = filedrag.STGMEDIUM()
    hr = data._get(None, ctypes.pointer(fmt), ctypes.pointer(medium))
    assert hr == 0
    try:
        assert filedrag.hdrop_paths(medium.hGlobal) == [os.path.abspath(str(path))]
    finally:
        filedrag._release_global(medium.hGlobal)



def test_clips_group_by_height_tallest_first_and_sort_by_frame_rate():
    from paz_suite.convert_engine import GapClip
    from paz_suite.upscale_window import grouped
    clips = [
        GapClip("F", "b.mp4", "b", height=1080, fps=30.0),
        GapClip("F", "a.mp4", "a", height=720, fps=60.0),
        GapClip("F", "c.mp4", "c", height=1080, fps=60.0),
        GapClip("F", "d.mp4", "d"),
        GapClip("F", "e.mp4", "e", height=1080, fps=30.0),
    ]
    groups = grouped(clips)
    assert [height for height, _ in groups] == [1080, 720, 0]
    assert [clip.name for clip in groups[0][1]] == ["c.mp4", "b.mp4", "e.mp4"]


def test_measuring_a_clip_labels_it_or_drops_a_finished_one(tmp_path):
    from paz_suite.convert_engine import GapClip, measured
    cfg = AppConfig(min_height=2160, min_fps=60)
    clip = GapClip("F", "x.mp4", "x", size=10)
    assert measured(cfg, clip, 1920, 1080, 30.0, 5.0).need == "4K and 60"
    assert measured(cfg, clip, 3840, 2160, 60.0, 5.0) is None
    blank = measured(cfg, clip, 0, 0, 0, 0)
    assert blank.need == "" and blank.size == 10


def test_a_copy_carries_the_files_as_a_copy_not_a_cut(tmp_path):
    path = tmp_path / "6767467.mp4"
    path.write_bytes(b"x")
    items = filedrag._clipboard_payload([str(path)])
    assert items
    try:
        formats = dict(items)
        assert filedrag.hdrop_paths(formats[filedrag.CF_HDROP]) == [os.path.abspath(str(path))]
        assert len(items) == 3
    finally:
        for _fmt, handle in items:
            filedrag._release_global(handle)


def test_a_released_drag_object_is_forgotten_only_by_the_sweep(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x")
    data = filedrag._Data([str(path)])
    key = next(k for k, v in filedrag._live.items() if v is data)
    data.block.ref = 0
    assert key in filedrag._live
    filedrag._sweep()
    assert key not in filedrag._live


def test_a_portrait_clip_groups_by_its_short_side():
    from paz_suite.convert_engine import GapClip
    from paz_suite.upscale_window import grouped
    clips = [
        GapClip("F", "tall.mp4", "t", width=1294, height=2770, fps=30.0),
        GapClip("F", "wide.mp4", "w", width=3840, height=2160, fps=24.0),
        GapClip("F", "hd.mp4", "h", width=1920, height=1080, fps=60.0),
    ]
    groups = grouped(clips)
    # 1294 is its short side, so it is in the 1080p band, not "2770p".
    assert [height for height, _ in groups] == [2160, 1080]
    assert [c.name for c in groups[1][1]] == ["hd.mp4", "tall.mp4"]


def test_odd_sizes_share_a_band_instead_of_a_group_each():
    from paz_suite.convert_engine import GapClip
    from paz_suite.upscale_window import grouped, _height_label
    clips = [GapClip("F", f"{h}.mp4", str(h), width=h * 16 // 9, height=h, fps=30.0)
             for h in (1072, 1080, 1088, 1200, 714, 720, 360)]
    groups = grouped(clips)
    # 1072 is a cropped 1080 and 714 a cropped 720, not the band below.
    assert [_height_label(h) for h, _ in groups] == ["1080p", "720p", "Under 480p"]
    assert [len(m) for _, m in groups] == [4, 2, 1]


def test_a_drag_with_nothing_to_drag_says_so():
    assert filedrag.drag_files([]) == ""
    assert filedrag.drag_files(["Z:/no/such/file.mp4"]) == ""


def test_clips_split_by_what_topaz_has_to_do_then_resolution():
    from paz_suite.convert_engine import GapClip
    from paz_suite.upscale_window import sections
    clips = [
        GapClip("F", "both.mp4", "a", width=1920, height=1080, fps=30.0, need="4K and 60"),
        GapClip("F", "res.mp4", "b", width=1920, height=1080, fps=60.0, need="4K"),
        GapClip("F", "fps4k.mp4", "c", width=3840, height=2160, fps=24.0, need="60 fps"),
        GapClip("F", "fps4k30.mp4", "d", width=3840, height=2160, fps=30.0, need="60 fps"),
        GapClip("F", "unknown.mp4", "e"),
        GapClip("F", "both720.mp4", "f", width=1280, height=720, fps=24.0, need="4K and 60"),
    ]
    out = sections(clips)
    assert [need for need, _ in out] == ["60 fps", "4K", "4K and 60", ""]
    fps_only = out[0][1]
    assert [c.name for c in fps_only[0][1]] == ["fps4k30.mp4", "fps4k.mp4"]
    both = out[2][1]
    assert [h for h, _ in both] == [1080, 720]
