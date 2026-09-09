"""A cover picture per Vault project.

This is the one picture slot that is an asset rather than decoration: the
still you upload the finished PMV with, kept alongside the project it
belongs to instead of loose in a folder. It lives in the database, not the
config, because there is one per project.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import library_db as db                    # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "lib.sqlite3"))
    connection = db.db_connect()
    db.vault_ensure_project(connection, "Phonk PMV 01", "#4DA3FF")
    db.vault_ensure_project(connection, "Techno set", "#FFB84D")
    yield connection
    connection.close()


def test_a_project_starts_with_no_cover(conn):
    assert db.vault_cover(conn, "Phonk PMV 01") == {
        "path": "", "zoom": 1.0, "fx": 0.5, "fy": 0.5}


def test_a_cover_is_kept_with_its_project(conn):
    db.vault_set_cover(conn, "Phonk PMV 01", "/pics/thumb.png", 1.4, 0.3, 0.7)
    assert db.vault_cover(conn, "Phonk PMV 01") == {
        "path": "/pics/thumb.png", "zoom": 1.4, "fx": 0.3, "fy": 0.7}
    assert db.vault_cover(conn, "Techno set")["path"] == ""


def test_a_cover_survives_reopening_the_database(conn, tmp_path):
    db.vault_set_cover(conn, "Phonk PMV 01", "/pics/thumb.png", 1.4, 0.3, 0.7)
    conn.close()
    again = db.db_connect()
    try:
        assert again.execute(
            "SELECT cover_path FROM vault_projects WHERE name=?",
            ("Phonk PMV 01",)).fetchone()[0] == "/pics/thumb.png"
    finally:
        again.close()


def test_a_cover_can_be_cleared(conn):
    db.vault_set_cover(conn, "Phonk PMV 01", "/pics/thumb.png", 1.4, 0.3, 0.7)
    db.vault_set_cover(conn, "Phonk PMV 01", "")
    assert db.vault_cover(conn, "Phonk PMV 01")["path"] == ""


def test_asking_for_a_project_that_is_gone(conn):
    assert db.vault_cover(conn, "Never existed")["path"] == ""


def test_covers_lists_only_the_projects_that_have_one(conn):
    db.vault_set_cover(conn, "Techno set", "/pics/t.png", 1.0, 0.5, 0.5)
    covers = db.vault_covers(conn)
    assert set(covers) == {"Techno set"}
    assert covers["Techno set"]["path"] == "/pics/t.png"


def test_clearing_a_cover_takes_it_out_of_the_listing(conn):
    db.vault_set_cover(conn, "Techno set", "/pics/t.png")
    db.vault_set_cover(conn, "Techno set", "")
    assert db.vault_covers(conn) == {}


# ── renaming ────────────────────────────────────────────────────────────

def test_renaming_carries_the_cover_across(conn):
    db.vault_set_cover(conn, "Phonk PMV 01", "/pics/thumb.png", 1.4, 0.3, 0.7)
    db.vault_rename_project(conn, "Phonk PMV 01", "Phonk PMV 01 final")
    assert db.vault_cover(conn, "Phonk PMV 01 final") == {
        "path": "/pics/thumb.png", "zoom": 1.4, "fx": 0.3, "fy": 0.7}


def test_merging_into_a_project_without_a_cover_keeps_the_incoming_one(conn):
    db.vault_set_cover(conn, "Phonk PMV 01", "/pics/thumb.png", 1.2, 0.4, 0.6)
    db.vault_rename_project(conn, "Phonk PMV 01", "Techno set")
    assert db.vault_cover(conn, "Techno set")["path"] == "/pics/thumb.png"


def test_merging_does_not_overwrite_a_cover_that_is_already_there(conn):
    db.vault_set_cover(conn, "Phonk PMV 01", "/pics/incoming.png")
    db.vault_set_cover(conn, "Techno set", "/pics/keep.png")
    db.vault_rename_project(conn, "Phonk PMV 01", "Techno set")
    assert db.vault_cover(conn, "Techno set")["path"] == "/pics/keep.png"


def test_clearing_a_project_takes_its_cover_with_it(conn):
    db.vault_set_cover(conn, "Techno set", "/pics/t.png")
    db.vault_clear_project(conn, "Techno set")
    assert db.vault_covers(conn) == {}


# ── the migration ───────────────────────────────────────────────────────

def test_a_database_made_before_covers_existed_gains_the_columns(tmp_path, monkeypatch):
    """CREATE TABLE IF NOT EXISTS does nothing to a table that is already
    there, so a column added later only ever appears on a fresh database
    unless something goes and adds it."""
    path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE files (path TEXT PRIMARY KEY, name TEXT, folder TEXT,"
        " pid TEXT, size INTEGER, mtime INTEGER, duration REAL, width INTEGER,"
        " height INTEGER, fps REAL);"
        "CREATE TABLE vault_projects (name TEXT PRIMARY KEY, color TEXT,"
        " created_at INTEGER);"
        "CREATE TABLE vault_marks (path TEXT NOT NULL, project TEXT NOT NULL,"
        " marked_at INTEGER, PRIMARY KEY (path, project));")
    old.execute("INSERT INTO vault_projects VALUES ('Old one', '#fff', 1)")
    old.commit()
    old.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.db_connect()
    try:
        columns = {row[1] for row in
                   conn.execute("PRAGMA table_info(vault_projects)").fetchall()}
        assert {"cover_path", "cover_zoom", "cover_fx", "cover_fy"} <= columns
        # and the project that was already there is untouched and usable
        assert db.vault_cover(conn, "Old one")["path"] == ""
        db.vault_set_cover(conn, "Old one", "/pics/x.png", 1.1, 0.2, 0.3)
        assert db.vault_cover(conn, "Old one")["zoom"] == 1.1
    finally:
        conn.close()


def test_opening_twice_does_not_try_to_add_the_columns_again(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "twice.sqlite3"))
    db.db_connect().close()
    db.db_connect().close()          # would raise "duplicate column name"
