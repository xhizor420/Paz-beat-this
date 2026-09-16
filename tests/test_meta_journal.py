"""The tag cache's crash-safety, and the cost of providing it.

Tagging a ten-thousand-clip library is hours of API calls, so the cache
has to survive being interrupted. It used to buy that by rewriting the
whole file every ten posts - correct, but seven and a half megabytes and
an eighth of a second each time, a thousand times a run. Now the ten new
posts are appended to a journal and folded in at the end. These tests
are about the part that must not break in exchange: nothing fetched is
ever lost, and the journal never outlives the rewrite that absorbs it.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import e621                                    # noqa: E402


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """E621Meta pointed at a scratch file, with no network anywhere."""
    main = tmp_path / "e621_meta.json"
    monkeypatch.setattr(e621, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(e621, "E621_META_PATH", str(main))
    monkeypatch.setattr(e621, "E621_META_LOG", str(main) + ".log")

    def make():
        return e621.E621Meta()

    make.main = main
    make.log = tmp_path / "e621_meta.json.log"
    return make


def stash(meta, pid, tags="wolf canine"):
    """What fetch() does to the cache, without the API call."""
    with meta._lock:
        meta._data[pid] = {"tags": tags, "score": 7, "fetched_at": 1.0}
        meta._pending.add(pid)
        meta._dirty = True


# ── the journal carries what the rewrite has not ────────────────────────

def test_a_checkpoint_writes_only_what_is_new(cache):
    meta = cache()
    for pid in ("100", "101", "102"):
        stash(meta, pid)
    meta.save()                             # the three are now in the file
    stash(meta, "200")
    meta.checkpoint()
    lines = cache.log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["pid"] == "200"


def test_a_checkpoint_does_not_touch_the_main_file(cache):
    meta = cache()
    stash(meta, "100")
    meta.save()
    before = cache.main.read_bytes()
    stash(meta, "200")
    meta.checkpoint()
    assert cache.main.read_bytes() == before


def test_two_checkpoints_append_rather_than_replace(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    stash(meta, "2")
    meta.checkpoint()
    lines = cache.log.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["pid"] for line in lines] == ["1", "2"]


def test_a_checkpoint_with_nothing_new_writes_nothing(cache):
    meta = cache()
    meta.checkpoint()
    assert not cache.log.exists()


def test_a_checkpoint_clears_what_it_banked(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    meta.checkpoint()
    assert len(cache.log.read_text(encoding="utf-8").strip().splitlines()) == 1


# ── an interrupted run loses nothing ────────────────────────────────────

def test_a_run_killed_after_a_checkpoint_comes_back(cache):
    meta = cache()
    stash(meta, "old")
    meta.save()
    stash(meta, "fetched-then-crashed", tags="fox vulpine")
    meta.checkpoint()
    del meta                                # the process dies here

    again = cache()
    assert again.get("old") is not None
    assert again.get("fetched-then-crashed")["tags"] == "fox vulpine"


def test_the_journal_is_absorbed_on_the_way_back_in(cache):
    meta = cache()
    stash(meta, "a")
    meta.checkpoint()
    del meta

    cache()
    assert not cache.log.exists()           # folded in and cleaned up
    assert json.loads(cache.main.read_text(encoding="utf-8"))["a"]["score"] == 7


def test_a_half_written_last_line_does_not_lose_the_rest(cache):
    """The process can die in the middle of a write. The complete lines
    before it are still good and must survive."""
    meta = cache()
    stash(meta, "1")
    stash(meta, "2")
    meta.checkpoint()
    with open(cache.log, "a", encoding="utf-8") as fh:
        fh.write('{"pid": "3", "rec": {"tags": "tor')
    del meta

    again = cache()
    assert again.get("1") is not None
    assert again.get("2") is not None
    assert again.get("3") is None


def test_the_journal_wins_over_a_stale_main_file(cache):
    """A record re-fetched during the interrupted run is the newer one."""
    meta = cache()
    stash(meta, "5", tags="old tags")
    meta.save()
    stash(meta, "5", tags="new tags")
    meta.checkpoint()
    del meta

    assert cache().get("5")["tags"] == "new tags"


def test_a_journal_with_no_main_file_still_loads(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    del meta
    if cache.main.exists():
        os.remove(cache.main)

    assert cache().get("1") is not None


def test_rubbish_in_the_journal_is_skipped_not_fatal(cache):
    meta = cache()
    stash(meta, "good")
    meta.checkpoint()
    with open(cache.log, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('{"pid": 99, "rec": {}}\n')        # pid must be a string
        fh.write('{"pid": "x", "rec": "not a dict"}\n')
    del meta

    again = cache()
    assert again.get("good") is not None
    assert again.get("x") is None


# ── the full save still does its job ────────────────────────────────────

def test_a_full_save_removes_the_journal(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    assert cache.log.exists()
    meta.save()
    assert not cache.log.exists()


def test_a_full_save_after_a_checkpoint_keeps_both(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    stash(meta, "2")
    meta.save()
    written = json.loads(cache.main.read_text(encoding="utf-8"))
    assert set(written) == {"1", "2"}


def test_a_clean_save_is_still_a_no_op(cache):
    meta = cache()
    stash(meta, "1")
    meta.save()
    stamp = cache.main.stat().st_mtime_ns
    meta.save()
    assert cache.main.stat().st_mtime_ns == stamp


def test_loading_a_cache_with_no_files_at_all_is_empty(cache):
    meta = cache()
    assert meta._data == {}
    assert not cache.log.exists()
