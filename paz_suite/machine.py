"""Answers about this machine that cost more to find than to remember.

Some questions the app asks at startup are not about the user's library
or settings at all - they are about what is installed on the machine.
Which encoders does this ffmpeg build have. Where is libVLC. Both are
answered by running a subprocess or walking Program Files, and neither
answer changes between launches unless the tool itself does.

Asked every launch they were the last two stutters left in the freeze
log - 215ms and 236ms of the window not answering, because a cold or
busy disk makes a subprocess and a directory walk slow, and this app's
user has a busy disk by definition. Remembered, they cost one small
file read.

Each answer is stored with a stamp: whatever cheaply identifies the
install it came from. When the stamp still matches, the stored answer
stands; when it does not, the caller finds out again. That is what keeps
this from being a cache that lies after an upgrade.
"""

from __future__ import annotations

import json
import os
import threading

from .config import CONFIG_DIR

CACHE_PATH = os.path.join(CONFIG_DIR, "machine.json")

_lock = threading.Lock()
_data: dict | None = None


def _read() -> dict:
    """The store, read once per process. Call holding _lock."""
    global _data
    if _data is None:
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except Exception:
            loaded = {}
        _data = loaded if isinstance(loaded, dict) else {}
    return _data


def remembered(name: str, stamp: str):
    """The stored answer for `name`, or None.

    None means "ask again" - either nothing was stored, or it was stored
    for a different install than the one here now.
    """
    with _lock:
        held = _read().get(name)
        if isinstance(held, dict) and held.get("stamp") == str(stamp):
            return held.get("value")
        return None


def remember(name: str, stamp: str, value) -> None:
    """Store an answer against the install it describes.

    Never raises: this is a shortcut, and a machine whose config folder
    cannot be written to should be slow, not broken.
    """
    with _lock:
        store = _read()
        store[name] = {"stamp": str(stamp), "value": value}
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(store, fh)
            os.replace(tmp, CACHE_PATH)
        except Exception:
            # Everything, not just OSError: a path can fail in ways that
            # are not IO errors at all (an embedded null byte raises
            # ValueError), and a value that will not serialise raises
            # TypeError. This is a shortcut - it must never be the reason
            # the app stops working.
            pass


def forget() -> None:
    """Drop everything stored, in memory and on disk."""
    global _data
    with _lock:
        _data = {}
        try:
            os.remove(CACHE_PATH)
        except Exception:
            pass


def tool_stamp(path: str) -> str:
    """What identifies an installed executable, cheaply: where it is and
    what it is. An upgrade changes the size or the timestamp, so the
    stamp changes with it and whatever was remembered is asked again."""
    if not path:
        return "none"
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return "none"
