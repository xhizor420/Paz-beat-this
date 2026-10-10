"""Start Grabber on the search it already has open.

Grabber keeps the filename rules, the four category folders, the login,
and the list of files already downloaded. This only asks it to run that
search and save what is new. Nothing about where a file lands is decided
here.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

GRABBER_DIR = r"C:\Program Files\Grabber"
GRABBER_COM = os.path.join(GRABBER_DIR, "Grabber.com")
# e621 will not return more than this on one page.
MAX_PER_PAGE = 320

_running: subprocess.Popen | None = None
# Set by stop(), so the end of the run can say it was stopped rather
# than that Grabber failed.
_stopped = False


def grabber_com() -> str:
    """Grabber's command-line program, wherever it was installed: the
    64-bit or 32-bit Program Files, or a per-user install. The usual
    place when none of them has it, so the error names somewhere real."""
    places = (os.environ.get("ProgramFiles", ""),
              os.environ.get("ProgramFiles(x86)", ""),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"))
    for base in places:
        if base:
            path = os.path.join(base, "Grabber", "Grabber.com")
            if os.path.isfile(path):
                return path
    return GRABBER_COM


def _logs_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Bionus", "Grabber", "logs")


def parse_progress(line: str) -> tuple[str, str] | None:
    """One log line into ('new'|'skip', filename). URLs are never returned."""
    if "Loading and saving image from" in line:
        return ("new", _file_after(line, " in `"))
    if "File already exists:" in line:
        return ("skip", _file_after(line, "File already exists: `"))
    if "blacklisted tags" in line:
        return ("skip", "")
    return None


def _path_after(line: str, marker: str) -> str:
    start = line.find(marker)
    if start < 0:
        return ""
    rest = line[start + len(marker):]
    return rest.split("`", 1)[0].replace("/", "\\")


def _file_after(line: str, marker: str) -> str:
    return os.path.basename(_path_after(line, marker))


def saved_folder(line: str) -> str:
    """The folder Grabber put a new file in - "MLP Futa" - from its save
    line, or "" for any other line. Grabber's own folder rules decided
    it; this only reads the answer back."""
    if "Loading and saving image from" not in line:
        return ""
    path = _path_after(line, " in `")
    return os.path.basename(os.path.dirname(path)) if path else ""


def folder_counts(saved: list) -> list[tuple[str, int]]:
    """[(folder, how many)] from (file, folder) pairs, most first, then
    by name - so "MLP Futa 2 · Furry 1" reads the same every time."""
    counts: dict[str, int] = {}
    for _name, folder in saved:
        counts[folder or "?"] = counts.get(folder or "?", 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def where_text(saved: list) -> str:
    """ "MLP Futa 2 · Furry 1" - where the new files went."""
    return " · ".join(f"{folder} {n}" for folder, n in folder_counts(saved))


def status_text(event: dict) -> dict:
    """Words and a 0–1 fill for the grab bar. Filenames only, never URLs."""
    new = int(event.get("new") or 0)
    skipped = int(event.get("skipped") or 0)
    total = max(int(event.get("total") or 1), 1)
    seen = new + skipped
    name = str(event.get("name") or "")
    recent = [n for n in (event.get("recent") or []) if n][-3:]
    if event.get("done") and event.get("stopped"):
        detail = f"{new} saved before it stopped" if new else "Nothing was saved"
        return {"title": "Stopped", "detail": detail, "count": str(new),
                "fraction": min(seen / total, 1.0), "color": "dim"}
    if event.get("done") and event.get("failed") and not new and not skipped:
        return {"title": "Grabber stopped", "detail": "Nothing was saved",
                "count": "0", "fraction": 0.0, "color": "dim"}
    if event.get("done") and event.get("logged") is False:
        # No log means no count - which is not the same as nothing new,
        # and must not read like it.
        return {"title": "Grabber finished",
                "detail": "Its log wasn't found, so the count is unknown",
                "count": "?", "fraction": 1.0, "color": "accent"}
    if event.get("done"):
        title = f"Grabbed {new} new" if new else "Nothing new"
        where = where_text(event.get("saved") or [])
        if skipped:
            detail = f"{skipped} already had"
        elif new:
            detail = "All of them were new"
        else:
            detail = "This page was empty"
        if where:
            title = f"{title}  ·  {where}"
        return {"title": title, "detail": detail, "count": str(new),
                "fraction": 1.0, "color": "ok" if new else "dim"}
    if recent:
        detail = f"{seen} of {total} · " + " · ".join(recent)
        where = where_text(event.get("saved") or [])
        if where:
            detail += f"   ({where})"
    elif name:
        detail = f"{seen} of {total} · {name}"
    else:
        detail = f"0 of {total}"
    return {"title": "Pulling new videos", "detail": detail, "count": str(new),
            "fraction": min(seen / total, 1.0), "color": "accent"}


def _tabs_path() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Bionus", "Grabber", "tabs.json")


def saved_search(path: str | None = None) -> tuple[str, str, int, int]:
    """The search Grabber currently has open: tags, source, page, page size.

    Page and page size are whatever that tab is showing, so Grab new
    pulls the same page the Grabber window would. Falls back to the
    first page of the favorites search when the file is missing.
    """
    tags, source, page, per_page = "fav:Xhizor", "e621.net", 1, 250
    try:
        with open(path or _tabs_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        tab = data["tabs"][int(data.get("current") or 0)]
        found = " ".join(t for t in tab.get("tags") or [] if t).strip()
        sites = " ".join(s for s in tab.get("sites") or [] if s).strip()
        if found:
            tags = found
        if sites:
            source = sites
        page = max(int(tab.get("page") or page), 1)
        per_page = int(tab.get("perpage") or per_page)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    per_page = min(max(per_page, 1), MAX_PER_PAGE)
    return tags, source, page, per_page


def command(tags: str, source: str, page: int, per_page: int) -> list[str]:
    """Grabber's own downloader, for the one page it has open.

    -m is the page size, not the whole search, so this is "get this page"
    and not "get every favorite ever." -f and -l are left off on purpose.
    Passing either would replace the conditional folders. Omitting them
    makes Grabber use Save/filename and Save/path from its settings, and
    the same MD5 list the window uses.
    """
    # Grabber.com is already the command-line program. Passing -c makes
    # it refuse to start ("Unknown option 'c'"). Grabber.exe is the window.
    return [
        grabber_com(),
        "-s", source,
        "-t", tags,
        "-p", str(page),
        "-i", str(per_page),
        "-m", str(per_page),
        "--download",
    ]


def start(on_progress=None) -> str | None:
    """Download with no console. `on_progress` is called from a worker.

    Each call is a dict: new, skipped, total, name, recent, done, and at
    the end failed, stopped and logged. `name` is the file just handled,
    never a URL. There is always a final call with done=True, whatever
    goes wrong while watching.
    """
    global _running, _stopped
    if running():
        return "Grabber is already downloading."
    program = grabber_com()
    if not os.path.isfile(program):
        return f"Grabber is not installed in {os.path.dirname(program)}."
    tags, source, page, per_page = saved_search()
    if not tags or not source:
        return "Grabber has no search saved."
    before = _log_names()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            command(tags, source, page, per_page),
            cwd=os.path.dirname(program),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return str(exc)
    _running = proc
    _stopped = False
    threading.Thread(
        target=_watch, args=(proc, per_page, before, on_progress),
        daemon=True, name="grabber").start()
    return None


def running() -> bool:
    """Whether a download started here is still going."""
    return _running is not None and _running.poll() is None


def stop() -> None:
    """End a running download. Files already saved stay saved."""
    global _stopped
    proc = _running
    if proc is None or proc.poll() is not None:
        return
    _stopped = True
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


def _log_names() -> set[str]:
    try:
        return set(os.listdir(_logs_dir()))
    except OSError:
        return set()


def _newest_log(before: set[str]) -> str | None:
    fresh = _log_names() - before
    folder = _logs_dir()
    newest, newest_at = None, -1.0
    for name in fresh:
        path = os.path.join(folder, name)
        try:
            at = os.path.getmtime(path)
        except OSError:
            continue            # rotated away between the listing and now
        if at > newest_at:
            newest, newest_at = path, at
    return newest


class _Tally:
    """What the log has said so far."""

    def __init__(self):
        self.new = 0
        self.skipped = 0
        self.name = ""
        self.recent: list[str] = []
        # (file, folder) for every new one, in the order saved.
        self.saved: list[tuple[str, str]] = []

    def take(self, line: str) -> bool:
        parsed = parse_progress(line)
        if parsed is None:
            return False
        kind, found = parsed
        if kind == "new":
            self.new += 1
            self.saved.append((found, saved_folder(line)))
            if found:
                self.recent.append(found)
                del self.recent[:-3]
        else:
            self.skipped += 1
        if found:
            self.name = found
        return True


def _watch(proc: subprocess.Popen, total: int, before: set[str], on_progress) -> None:
    """Read Grabber's log as it writes it and report saves and skips.

    The last report (done=True) is in a finally: a watcher that died on
    some surprise would otherwise leave the button saying "Grabbing…"
    until the app was restarted."""
    tally = _Tally()
    log_path = None

    def emit(done: bool = False) -> None:
        if on_progress is None:
            return
        event = {"new": tally.new, "skipped": tally.skipped, "total": total,
                 "name": tally.name, "recent": list(tally.recent),
                 "saved": list(tally.saved), "done": done}
        if done:
            event["failed"] = proc.returncode not in (0, None)
            event["stopped"] = _stopped and proc is _running
            event["logged"] = log_path is not None
        try:
            on_progress(event)
        except Exception:
            pass

    try:
        for _ in range(80):
            log_path = _newest_log(before)
            if log_path or proc.poll() is not None:
                break
            time.sleep(0.1)
        offset = 0
        pending = ""
        while True:
            exited = proc.poll() is not None
            if log_path is None:
                log_path = _newest_log(before)
            if log_path:
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                        offset = fh.tell()
                except OSError:
                    chunk = ""
                if chunk:
                    pending += chunk
                # A trailing fragment stays buffered until the next read,
                # unless Grabber has already exited - then it is the last line.
                lines = []
                if pending and (chunk or exited):
                    lines = pending.splitlines(keepends=True)
                    if lines and not exited and not lines[-1].endswith(("\n", "\r")):
                        pending = lines.pop()
                    else:
                        pending = ""
                changed = False
                for line in lines:
                    changed = tally.take(line) or changed
                if changed and not exited:
                    emit()
            if exited:
                return
            time.sleep(0.25)
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        emit(done=True)
