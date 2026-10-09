"""The tag cache's storage: crash-safe, incremental, and off the UI thread.

Tagging a ten-thousand-clip library is hours of API calls, so the cache
has to survive being interrupted. It used to be one JSON file rewritten
whole on every save, plus a journal of what was fetched since. It is now
SQLite, one row per post, and a save writes only the posts that changed.
These pin what must hold either way: nothing fetched is ever lost, nothing
is rewritten that did not change, the old files come across once, and the
read never lands on the thread drawing the window.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import e621                                    # noqa: E402


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """E621Meta pointed at scratch files, with no network anywhere."""
    db = tmp_path / "e621_meta.sqlite3"
    old = tmp_path / "e621_meta.json"
    monkeypatch.setattr(e621, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(e621, "E621_DB_PATH", str(db))
    monkeypatch.setattr(e621, "E621_META_PATH", str(old))
    monkeypatch.setattr(e621, "E621_META_LOG", str(old) + ".log")

    def make():
        meta = e621.E621Meta()
        assert meta.wait_until_read(10.0), "the cache never finished loading"
        return meta

    make.db = db
    make.old = old
    make.log = tmp_path / "e621_meta.json.log"
    return make


def stash(meta, pid, tags="wolf canine"):
    """What fetch() does to the cache, without the API call."""
    with meta._lock:
        meta._data[pid] = {"tags": tags, "score": 7, "fetched_at": 1.0}
        meta._pending.add(pid)
        meta._dirty = True


def stored(cache) -> dict:
    conn = sqlite3.connect(cache.db)
    try:
        return {pid: json.loads(rec) for pid, rec in
                conn.execute("SELECT pid, rec FROM posts")}
    finally:
        conn.close()


# ── a save writes what changed, and only that ───────────────────────────

def test_a_checkpoint_writes_only_what_is_new(cache, monkeypatch):
    meta = cache()
    for pid in ("100", "101", "102"):
        stash(meta, pid)
    meta.save()
    written = []
    real = meta._connect

    def watched():
        conn = real()

        class Spy:
            def __enter__(self):
                return conn.__enter__()

            def __exit__(self, *a):
                return conn.__exit__(*a)

            def executemany(self, sql, rows):
                rows = list(rows)
                written.extend(pid for pid, _ in rows)
                return conn.executemany(sql, rows)

            def close(self):
                conn.close()
        return Spy()

    monkeypatch.setattr(meta, "_connect", watched)
    stash(meta, "200")
    meta.checkpoint()
    assert written == ["200"], "a checkpoint rewrote posts that had not changed"


def test_a_checkpoint_with_nothing_new_writes_nothing(cache):
    meta = cache()
    meta.checkpoint()
    assert stored(cache) == {}


def test_a_checkpoint_clears_what_it_banked(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    assert meta._pending == set()


def test_a_save_writes_every_pending_post(cache):
    meta = cache()
    stash(meta, "1")
    meta.checkpoint()
    stash(meta, "2")
    meta.save()
    assert set(stored(cache)) == {"1", "2"}


def test_a_changed_post_replaces_its_row(cache):
    meta = cache()
    stash(meta, "5", tags="old tags")
    meta.save()
    stash(meta, "5", tags="new tags")
    meta.save()
    assert stored(cache)["5"]["tags"] == "new tags"


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


def test_loading_with_no_files_at_all_is_empty(cache):
    meta = cache()
    assert meta._data == {}


# ── the old JSON cache comes across once ────────────────────────────────

def test_the_old_json_cache_is_imported(cache):
    cache.old.write_text(json.dumps({"1": {"tags": "wolf", "score": 3}}),
                         encoding="utf-8")
    meta = cache()
    assert meta.get("1")["tags"] == "wolf"
    assert stored(cache)["1"]["score"] == 3


def test_its_journal_comes_with_it_and_wins(cache):
    """Posts re-fetched during an interrupted run are the newer ones."""
    cache.old.write_text(json.dumps({"5": {"tags": "old tags"}}), encoding="utf-8")
    with open(cache.log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": "5", "rec": {"tags": "new tags"}}) + "\n")
        fh.write(json.dumps({"pid": "6", "rec": {"tags": "only here"}}) + "\n")
    meta = cache()
    assert meta.get("5")["tags"] == "new tags"
    assert meta.get("6")["tags"] == "only here"
    assert not cache.log.exists(), "the journal was folded in and should go"


def test_a_half_written_journal_line_does_not_lose_the_rest(cache):
    with open(cache.log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": "1", "rec": {"tags": "a"}}) + "\n")
        fh.write('{"pid": "3", "rec": {"tags": "tor')
    meta = cache()
    assert meta.get("1") is not None
    assert meta.get("3") is None


def test_rubbish_in_the_journal_is_skipped_not_fatal(cache):
    with open(cache.log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": "good", "rec": {"tags": "a"}}) + "\n")
        fh.write("not json at all\n")
        fh.write('{"pid": 99, "rec": {}}\n')
        fh.write('{"pid": "x", "rec": "not a dict"}\n')
    meta = cache()
    assert meta.get("good") is not None
    assert meta.get("x") is None


def test_rubbish_in_the_old_file_is_an_empty_cache_not_a_crash(cache):
    cache.old.write_text("[1, 2, 3]", encoding="utf-8")
    meta = cache()
    assert meta.snapshot() == {}


def test_the_import_happens_once(cache):
    """After the first run the database is the cache; the JSON is a
    leftover backup and must not be read over newer data."""
    cache.old.write_text(json.dumps({"1": {"tags": "from json"}}), encoding="utf-8")
    meta = cache()
    stash(meta, "1", tags="fetched since")
    meta.save()
    del meta
    assert cache().get("1")["tags"] == "fetched since"


def test_the_old_json_is_left_alone(cache):
    cache.old.write_text(json.dumps({"1": {"tags": "wolf"}}), encoding="utf-8")
    before = cache.old.read_bytes()
    meta = cache()
    stash(meta, "2")
    meta.save()
    assert cache.old.read_bytes() == before


# ── the read is off the UI thread ───────────────────────────────────────

def test_the_constructor_does_not_read_the_cache(cache, monkeypatch):
    """Nothing about the load may happen on the calling thread."""
    import threading
    cache.old.write_text(json.dumps({"1": {"tags": "wolf"}}), encoding="utf-8")
    here = threading.get_ident()
    read_on = []
    real = e621.E621Meta._read_cache

    def watched(self):
        read_on.append(threading.get_ident())
        return real(self)

    monkeypatch.setattr(e621.E621Meta, "_read_cache", watched)
    meta = e621.E621Meta()
    assert meta.wait_until_read(10.0)
    assert read_on and here not in read_on


def test_a_post_fetched_before_the_read_lands_is_not_lost(cache):
    """fetch() deliberately does not wait for the read, so a record can be
    in memory before the stored ones. What was just fetched is newer."""
    cache.old.write_text(json.dumps({"1": {"tags": "old"}}), encoding="utf-8")
    meta = e621.E621Meta()
    with meta._lock:
        meta._data["2"] = {"tags": "just fetched"}
    assert meta.wait_until_read(10.0)
    held = meta.snapshot()
    assert held["1"]["tags"] == "old"
    assert held["2"]["tags"] == "just fetched"


def test_a_save_waits_for_the_import(cache):
    """Writing before the first run's import would leave the table
    non-empty, and the import would then never happen."""
    cache.old.write_text(json.dumps({"1": {"tags": "wolf"}}), encoding="utf-8")
    meta = e621.E621Meta()
    stash(meta, "2")
    meta.save()
    assert set(stored(cache)) == {"1", "2"}


def test_the_cache_writes_only_where_it_was_pointed(cache, monkeypatch):
    """A test's cache once wrote itself over the real one: the thread
    looked up the module globals after the test had put them back."""
    meta = cache()
    elsewhere = str(cache.db) + ".WRONG"
    monkeypatch.setattr(e621, "E621_DB_PATH", elsewhere)
    stash(meta, "2")
    meta.save()
    assert not os.path.exists(elsewhere)
    assert "2" in stored(cache)
