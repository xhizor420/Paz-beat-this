"""Automatic copies of the library database - see paz_suite/backups.py.

The Vault marks in that database are the one thing a rescan cannot bring
back, so the copies have to be real, readable databases, taken once a
day, pruned, and never able to take the app down when they fail.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                               # noqa: E402

from paz_suite import backups, config, library_db           # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "paz_config.json"))
    db = str(tmp_path / "paz_library.sqlite3")
    monkeypatch.setattr(library_db, "DB_PATH", db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE vault_marks (path TEXT, project TEXT)")
    conn.executemany("INSERT INTO vault_marks VALUES (?, ?)",
                     [(f"/lib/{i}.mp4", "Summer PMV") for i in range(500)])
    conn.commit()
    conn.close()
    (tmp_path / "paz_config.json").write_text('{"library_root": "D:/Clips"}',
                                              encoding="utf-8")
    return tmp_path


def marks_in(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM vault_marks").fetchone()[0]
    finally:
        conn.close()


def test_a_backup_is_a_complete_readable_database(home):
    path = backups.backup("daily")
    assert path and os.path.exists(path)
    assert marks_in(path) == 500
    settings = [n for n in os.listdir(backups.backup_dir()) if n.endswith(".json")]
    assert len(settings) == 1


def test_only_one_daily_copy_a_day(home):
    assert backups.backup("daily")
    assert backups.backup("daily") is None


def test_a_copy_before_a_rebuild_is_always_taken(home):
    assert backups.backup("before-rebuild", force=True)


def test_a_copy_taken_while_the_app_writes_is_still_consistent(home):
    """WAL mode, a writer mid-flight: the backup API copies a consistent
    snapshot rather than a half-written file."""
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(library_db.DB_PATH, timeout=15)
        n = 0
        while not stop.is_set():
            conn.execute("INSERT INTO vault_marks VALUES (?, ?)", (f"/x/{n}", "p"))
            conn.commit()
            n += 1
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        path = backups.backup("before-rebuild", force=True)
    finally:
        stop.set()
        thread.join()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert marks_in(path) >= 500
    conn.close()


def test_old_copies_are_pruned(home):
    folder = backups.backup_dir()
    os.makedirs(folder, exist_ok=True)
    for day in range(1, 12):
        for name in (f"paz_library-2026-01-{day:02d}-daily.sqlite3",
                     f"paz_config-2026-01-{day:02d}-daily.json"):
            open(os.path.join(folder, name), "wb").close()
    backups.backup("daily")                       # today's, then prune
    daily = sorted(n for n in os.listdir(folder) if n.endswith("-daily.sqlite3"))
    assert len(daily) == backups.KEEP_DAILY
    assert "paz_library-2026-01-01-daily.sqlite3" not in daily
    configs = [n for n in os.listdir(folder) if n.endswith("-daily.json")]
    assert len(configs) == backups.KEEP_DAILY


def test_no_database_yet_is_nothing_to_do(home, monkeypatch):
    monkeypatch.setattr(library_db, "DB_PATH", str(home / "missing.sqlite3"))
    assert backups.backup("daily") is None


def test_a_failure_returns_none_and_leaves_no_partial_file(home, monkeypatch):
    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(backups.sqlite3, "connect", boom)
    assert backups.backup("daily") is None
    folder = backups.backup_dir()
    assert not os.path.isdir(folder) or not any(n.endswith(".partial")
                                                for n in os.listdir(folder))
