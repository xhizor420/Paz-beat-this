"""Filesystem helpers shared by both tabs: proxy-folder filtering, post-ID
parsing from filenames, and "open in the OS" actions.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ─────────────────────────────────────────────────────────────────────────
#  Proxy folders
#
#  Resolve keeps editing proxies alongside the masters - a "Proxies" or
#  "mov" folder inside each category. Those are not library clips: counting
#  one as a 4K upscale marks a file done that never was, and indexing them
#  shows every clip twice. Every directory walk in this suite skips them.
# ─────────────────────────────────────────────────────────────────────────

# Deliberately specific. Generic names like "temp" or "tmp" were a mistake:
# a library living anywhere under such a folder had every file excluded.
IGNORE_DIRS = {
    "proxies", "proxy", "proxy media", "proxymedia", "_proxy", ".proxy",
    "mov", "movs", "resolve proxies", "optimized media", "render cache",
    "proxy files", "proxyfiles",
}


def is_ignored_dir(name: str) -> bool:
    """True for proxy / cache folders that must never be treated as media."""
    lowered = os.path.basename(str(name).rstrip("\\/")).strip().lower()
    return lowered in IGNORE_DIRS or lowered.startswith(".")


def prune_dirs(dirs: list) -> list:
    """In-place filter for os.walk's dirnames, so it never descends."""
    dirs[:] = [d for d in dirs if not is_ignored_dir(d)]
    return dirs


def in_ignored_path(path: str, root: str = "") -> bool:
    """
    True when any folder between `root` and `path` is a proxy folder.

    Separators are normalised by hand rather than via os.path, so a Windows
    path is judged correctly even when this runs somewhere else.
    """
    text = str(path).replace("\\", "/")
    base = str(root).replace("\\", "/").rstrip("/")
    if base and text.lower().startswith(base.lower() + "/"):
        parts = text[len(base) + 1:].split("/")[:-1]
    else:
        parts = text.split("/")[-2:-1]
    return any(is_ignored_dir(part) for part in parts
               if part not in ("", ".", ".."))


_PID_EXACT = re.compile(r"^(\d{3,10})$")
_PID_LOOSE = re.compile(r"(\d{5,10})")


def post_id_from(name: str) -> str:
    """Best-effort e621 post ID from a filename. '' when there isn't one."""
    stem = os.path.splitext(os.path.basename(name))[0]
    match = _PID_EXACT.match(stem)
    if match:
        return match.group(1)
    match = _PID_LOOSE.search(stem)
    return match.group(1) if match else ""


def open_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def open_in_explorer(path: str, select: bool = True) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            if select and os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(os.path.dirname(path) if os.path.isfile(path) else path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if select else "", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)
                               if os.path.isfile(path) else path])
    except Exception:
        pass
