"""Library index: the SQLite schema, the in-memory record shape, and the
e621-style search parser. Pure logic — no widgets — so the sync worker and
the search box can both be tested without a running GUI.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import sqlite3
import time
from dataclasses import dataclass, field

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    name     TEXT,
    folder   TEXT,
    pid      TEXT,
    size     INTEGER,
    mtime    INTEGER,
    duration REAL,
    width    INTEGER,
    height   INTEGER,
    fps      REAL
);
CREATE TABLE IF NOT EXISTS vault_projects (
    name       TEXT PRIMARY KEY,
    color      TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS vault_marks (
    path      TEXT NOT NULL,
    project   TEXT NOT NULL,
    marked_at INTEGER,
    PRIMARY KEY (path, project)
);
"""


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_pid ON files(pid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_marks_path ON vault_marks(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_marks_project ON vault_marks(project)")
    return conn


@dataclass
class Rec:
    path: str
    name: str
    folder: str
    pid: str
    size: int
    mtime: int
    duration: float
    width: int
    height: int
    fps: float
    # filled from the e621 cache at load time
    artists: list = field(default_factory=list)
    characters: list = field(default_factory=list)
    species: list = field(default_factory=list)
    copyrights: list = field(default_factory=list)
    lore: list = field(default_factory=list)
    rating: str = ""
    score: int = 0
    tags: set = field(default_factory=set)
    url: str = ""
    premium: bool = False        # a 4K/60+ copy exists (or this IS 4K)
    # Full path to that 4K/60+ copy under premium_root, when this record
    # itself isn't already one - filled in at load time alongside premium,
    # so the player can default playback to it. Empty when this record IS
    # the premium copy (nothing to switch to) or none exists.
    premium_path: str = ""
    # artist/character/species/copyright/lore names, union'd once at load
    # time instead of on every tag-panel and detail-panel render - the
    # difference is real once a library runs into five figures of clips.
    named: frozenset = field(default_factory=frozenset)
    # Vault marks: which project(s) this clip has been used in, filled in
    # at load time from vault_marks. used_color is the most-recently-marked
    # project's colour (a clip can be marked in more than one), used for
    # the gallery border - the rest are still listed in used_projects.
    used_projects: list = field(default_factory=list)
    used_color: str = ""

    def compute_named(self) -> None:
        self.named = frozenset(self.artists) | frozenset(self.characters) \
            | frozenset(self.species) | frozenset(self.copyrights) | frozenset(self.lore)

    @property
    def orientation(self) -> str:
        """
        "portrait" / "widescreen" / "square", derived from width x height.

        Not an e621 tag - almost nobody tags aspect ratio - but a real,
        fast way to browse "phone footage" vs. "widescreen footage" when
        picking clips for an edit with a fixed output orientation.
        """
        if not self.width or not self.height:
            return ""
        ratio = self.width / self.height
        if ratio <= 0.85:
            return "portrait"
        if ratio >= 1.2:
            return "widescreen"
        return "square"


def parse_query(text: str) -> tuple:
    """
    Split an e621-style query into include / exclude term lists.

    Mostly plain whitespace splitting, except a value can be quoted
    ("used:\"summer pmv\"") to hold spaces - project names in particular
    are free text, not tag-shaped. shlex handles that; a stray unmatched
    quote just falls back to plain splitting instead of erroring out.
    """
    try:
        raw_tokens = shlex.split(text)
    except ValueError:
        raw_tokens = text.split()
    includes, excludes = [], []
    for token in raw_tokens:
        target = includes
        if token.startswith("-") and len(token) > 1:
            target = excludes
            token = token[1:]
        token = token.lower()
        if ":" in token:
            kind, _, value = token.partition(":")
            if kind in ("artist", "character", "species", "copyright",
                        "series", "lore", "rating", "folder", "id",
                        "is", "used") and value:
                target.append((kind, value))
                continue
        target.append(("tag", token))
    return includes, excludes


def term_hits(rec: Rec, kind: str, value: str) -> bool:
    if kind == "is":
        if value in ("untagged", "notags"):
            return not rec.tags
        if value == "tagged":
            return bool(rec.tags)
        if value in ("noid", "unknown"):
            return not rec.pid
        if value == "silent":
            return rec.duration <= 0
        if value in ("4k", "premium"):
            return rec.premium
        if value in ("no4k", "sd"):
            return not rec.premium
        if value in ("portrait", "phone", "vertical"):
            return rec.orientation == "portrait"
        if value in ("widescreen", "landscape", "horizontal"):
            return rec.orientation == "widescreen"
        if value == "square":
            return rec.orientation == "square"
        return False
    if kind == "artist":
        return any(value == a or fnmatch.fnmatch(a, value) for a in rec.artists)
    if kind == "character":
        return any(value == c or fnmatch.fnmatch(c, value) for c in rec.characters)
    if kind == "species":
        return any(value == s or fnmatch.fnmatch(s, value) for s in rec.species)
    if kind in ("copyright", "series"):
        return any(value == c or fnmatch.fnmatch(c, value) for c in rec.copyrights)
    if kind == "lore":
        return any(value == l or fnmatch.fnmatch(l, value) for l in rec.lore)
    if kind == "rating":
        return rec.rating == value[:1]
    if kind == "folder":
        return value in rec.folder.lower()
    if kind == "id":
        return rec.pid == value
    if kind == "used":
        if value in ("", "any"):
            return bool(rec.used_projects)
        return any(value == p.lower() for p in rec.used_projects)
    # plain tag term
    if "*" in value:
        return any(fnmatch.fnmatch(t, value) for t in rec.tags)
    if value in rec.tags:
        return True
    # substring fallback: tags, filename, post id
    if value in rec.name.lower() or value in rec.pid:
        return True
    return any(value in t for t in rec.tags)


def rec_matches(rec: Rec, includes: list, excludes: list) -> bool:
    for kind, value in includes:
        if not term_hits(rec, kind, value):
            return False
    for kind, value in excludes:
        if term_hits(rec, kind, value):
            return False
    return True


SORTS = {
    "Newest":  lambda r: -r.mtime,
    "Name":    lambda r: r.name.lower(),
    "Longest": lambda r: -r.duration,
    "Largest": lambda r: -r.size,
    "Score":   lambda r: (-r.score, r.name.lower()),
}


# ── Vault: "used in project X" marks ────────────────────────────────────
#
# A clip can be marked as used in any number of named projects - reused
# footage across separate PMVs is normal, so this is deliberately a
# many-to-many relationship (vault_marks) rather than one field on the
# clip. Colour assignment lives with the caller (the Vault tab), not
# here, so this module stays UI-free.

def vault_ensure_project(conn: sqlite3.Connection, name: str, color: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO vault_projects (name, color, created_at) VALUES (?,?,?)",
        (name, color, int(time.time())))
    conn.commit()


def vault_mark(conn: sqlite3.Connection, paths, project: str) -> None:
    now = int(time.time())
    conn.executemany(
        "INSERT OR REPLACE INTO vault_marks (path, project, marked_at) VALUES (?,?,?)",
        [(path, project, now) for path in paths])
    conn.commit()


def vault_unmark(conn: sqlite3.Connection, path: str, project: str) -> None:
    conn.execute("DELETE FROM vault_marks WHERE path=? AND project=?", (path, project))
    conn.commit()


def vault_clear_project(conn: sqlite3.Connection, project: str) -> None:
    conn.execute("DELETE FROM vault_marks WHERE project=?", (project,))
    conn.execute("DELETE FROM vault_projects WHERE name=?", (project,))
    conn.commit()


def vault_rename_project(conn: sqlite3.Connection, old: str, new: str) -> None:
    if old == new or not new:
        return
    existing = conn.execute(
        "SELECT color FROM vault_projects WHERE name=?", (new,)).fetchone()
    if existing is None:
        conn.execute("UPDATE vault_projects SET name=? WHERE name=?", (new, old))
    else:
        # Renaming onto an existing project merges into it instead of
        # colliding on the (path, project) primary key.
        conn.execute("DELETE FROM vault_projects WHERE name=?", (old,))
    conn.execute(
        "INSERT OR IGNORE INTO vault_marks (path, project, marked_at) "
        "SELECT path, ?, marked_at FROM vault_marks WHERE project=?", (new, old))
    conn.execute("DELETE FROM vault_marks WHERE project=?", (old,))
    conn.commit()


def vault_projects_list(conn: sqlite3.Connection) -> list:
    """[(name, color, clip_count, created_at), ...], newest project first."""
    rows = conn.execute(
        "SELECT p.name, p.color, p.created_at, COUNT(m.path) "
        "FROM vault_projects p LEFT JOIN vault_marks m ON m.project = p.name "
        "GROUP BY p.name ORDER BY p.created_at DESC").fetchall()
    return [(name, color, count, created_at) for name, color, created_at, count in rows]


def vault_marks_by_path(conn: sqlite3.Connection) -> dict:
    """path -> [(project, color, marked_at), ...], most-recent mark first."""
    rows = conn.execute(
        "SELECT m.path, m.project, p.color, m.marked_at FROM vault_marks m "
        "JOIN vault_projects p ON p.name = m.project "
        "ORDER BY m.marked_at DESC").fetchall()
    by_path: dict = {}
    for path, project, color, marked_at in rows:
        by_path.setdefault(path, []).append((project, color, marked_at))
    return by_path
