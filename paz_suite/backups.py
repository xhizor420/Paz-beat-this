"""Copies of the library database and the settings, kept automatically.

The database holds the one thing in this app that cannot be rebuilt: the
Vault marks - which clips went into which project. Clips can be rescanned,
thumbnails remade and tags fetched again; months of "used in this PMV"
cannot. A bad disk sector, a sync gone wrong or a stray delete would take
them with nothing to go back to.

So once a day, a moment after the library has loaded, a copy is taken
into ~/.video_tool/backups, and the last KEEP_DAILY days are kept. A copy
is also taken right before "Rebuild everything", which clears the index.
Copies use SQLite's own backup API, which produces a consistent database
even while the app is writing to it - a plain file copy of a live
database can catch it half-written.

To restore: close the app, copy the backup over paz_library.sqlite3 (and
the matching paz_config file over paz_config.json if wanted), start it.
"""

from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import threading
import time

from . import config

KEEP_DAILY = 7
KEEP_BEFORE_REBUILD = 3


def backup_dir() -> str:
    return os.path.join(config.CONFIG_DIR, "backups")


def _db_path() -> str:
    from . import library_db
    return library_db.DB_PATH


def backup(reason: str = "daily", force: bool = False) -> str | None:
    """Take a copy now. Returns its path, or None when there was nothing
    to do (a daily copy already exists for today, or no database yet) or
    the copy failed. Any thread; never raises."""
    source = _db_path()
    if not os.path.exists(source):
        return None
    folder = backup_dir()
    day = time.strftime("%Y-%m-%d")
    stamp = day if reason == "daily" else time.strftime("%Y-%m-%d_%H%M%S")
    target = os.path.join(folder, f"paz_library-{stamp}-{reason}.sqlite3")
    if not force and os.path.exists(target):
        return None
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = target + ".partial"
        src = sqlite3.connect(source, timeout=15.0)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, target)
    except (OSError, sqlite3.Error):
        try:
            os.remove(target + ".partial")
        except OSError:
            pass
        return None
    try:
        if os.path.exists(config.CONFIG_PATH):
            shutil.copy2(config.CONFIG_PATH,
                         os.path.join(folder, f"paz_config-{stamp}-{reason}.json"))
    except OSError:
        pass
    prune()
    return target


def prune() -> None:
    """Keep the newest copies of each kind, and their settings files."""
    folder = backup_dir()
    for reason, keep in (("daily", KEEP_DAILY), ("before-rebuild", KEEP_BEFORE_REBUILD)):
        for pattern in (f"paz_library-*-{reason}.sqlite3", f"paz_config-*-{reason}.json"):
            found = sorted(glob.glob(os.path.join(folder, pattern)))
            for old in found[:-keep] if len(found) > keep else []:
                try:
                    os.remove(old)
                except OSError:
                    pass


def daily_soon() -> None:
    """Today's copy, on a worker - the database is only a few megabytes,
    but it is not the window's job to wait for a disk."""
    threading.Thread(target=backup, args=("daily",), daemon=True,
                     name="daily-backup").start()
