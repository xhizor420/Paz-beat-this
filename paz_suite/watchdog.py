"""Make a freeze say where it froze.

A hung window is the one failure that tells you nothing. There is no
traceback, because nothing crashed - some call on the thread that draws
the window simply has not returned, and the only thing left to do is kill
the process, which throws away the evidence. On a machine you cannot get
at, that is unfixable by inspection: you are reduced to guessing at
platform differences.

So the app writes its own post-mortem. A heartbeat is scheduled on the UI
thread; a watcher thread notices when the beat stops and dumps the stack
of every thread to a file. The thread that stopped beating is the one
that is stuck, and its top frame is the line to fix.

The same file collects unhandled exceptions, including the ones Tk
swallows out of callbacks, so a crash and a hang leave evidence in the
same place.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback

# How long the UI thread may go without a heartbeat before we call it
# stuck. Generous on purpose: opening a 4K file, building a thumbnail
# sheet or probing a slow drive can legitimately hold the thread for a
# second or two, and a false report is worse than a late one.
STUCK_AFTER = 6.0
BEAT_EVERY_MS = 500
# One dump per hang, not one every tick while it stays hung.
REARM_AFTER = 60.0

# An app does not have to freeze to feel broken. A second of stillness
# here and there - a thumbnail sheet built on the wrong thread, a big
# file written while the window waits - reads as "buggy", and until now
# left no trace at all, because the only thing recorded was death. This
# is the lighter rule: a pause long enough to be felt gets one line
# naming where the thread was, so a session that felt bad can say why.
LAGGY_AFTER = 1.5
LAG_REARM_AFTER = 15.0
MAX_LOG_BYTES = 2 * 1024 * 1024


class Watchdog:
    def __init__(self, root, log_path: str):
        self.root = root
        self.log_path = log_path
        self._beat = time.monotonic()
        self._reported_at = 0.0
        self._lagged_at = 0.0
        self._lags = 0
        self._stop = threading.Event()
        self._thread = None

    # ── wiring ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._trim()
        self._note("started", f"python {sys.version.split()[0]} on {sys.platform}")
        # Native crashes (a bad DLL, a segfault inside an audio or video
        # library) never reach Python, so they get their own channel.
        try:
            self._crash_file = open(self.log_path, "a", buffering=1,
                                    encoding="utf-8", errors="replace")
            faulthandler.enable(file=self._crash_file, all_threads=True)
        except Exception:
            self._crash_file = None
        self._install_hooks()
        self._tick()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _install_hooks(self) -> None:
        previous = sys.excepthook

        def hook(kind, value, tb):
            self._note("uncaught exception",
                       "".join(traceback.format_exception(kind, value, tb)))
            previous(kind, value, tb)

        sys.excepthook = hook

        # Tk prints callback errors to stderr and carries on, which on
        # Windows usually means straight to nowhere.
        def report(kind, value, tb):
            self._note("error in a UI callback",
                       "".join(traceback.format_exception(kind, value, tb)))
        try:
            self.root.report_callback_exception = report
        except Exception:
            pass

    # ── the heartbeat ───────────────────────────────────────────────────

    def _tick(self) -> None:
        self._beat = time.monotonic()
        try:
            self.root.after(BEAT_EVERY_MS, self._tick)
        except Exception:
            pass

    def _watch(self) -> None:
        while not self._stop.wait(0.5):
            quiet = time.monotonic() - self._beat
            if quiet < LAGGY_AFTER:
                continue
            now = time.monotonic()
            if quiet < STUCK_AFTER:
                if now - self._lagged_at >= LAG_REARM_AFTER:
                    self._lagged_at = now
                    self._lag(quiet)
                continue
            if now - self._reported_at < REARM_AFTER:
                continue
            self._reported_at = now
            self._dump(quiet)

    def _main_frames(self, depth: int = 4) -> list:
        """The top of the UI thread's stack - where it is, right now."""
        frame = sys._current_frames().get(threading.main_thread().ident)
        if frame is None:
            return []
        return [piece.rstrip()
                for piece in traceback.format_stack(frame)[-depth:]]

    def _lag(self, quiet: float) -> None:
        """A pause worth noticing, in one line plus where it happened."""
        self._lags += 1
        where = self._main_frames()
        self._note("LAGGY", f"the window paused for {quiet:.1f}s "
                            f"(pause #{self._lags} this session)\n"
                            + "\n".join("    " + line for line in where))

    # ── the report ──────────────────────────────────────────────────────

    def _dump(self, quiet: float) -> None:
        frames = sys._current_frames()
        main_id = threading.main_thread().ident
        lines = [f"the window has not responded for {quiet:.1f}s",
                 "the stuck thread is the one below marked MAIN/UI - its top "
                 "frame is where it is stuck", ""]
        by_id = {t.ident: t for t in threading.enumerate()}
        for ident, frame in frames.items():
            thread = by_id.get(ident)
            name = thread.name if thread else "?"
            tag = "  <-- MAIN/UI" if ident == main_id else ""
            lines.append(f"--- thread {name} ({ident}){tag}")
            lines.extend("    " + piece.rstrip()
                         for piece in traceback.format_stack(frame))
            lines.append("")
        self._note("FROZEN", "\n".join(lines))

    def _note(self, what: str, detail: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8",
                      errors="replace") as handle:
                handle.write(f"\n===== {stamp}  {what} =====\n{detail}\n")
        except Exception:
            pass

    def _trim(self) -> None:
        """Keep the file from growing without bound across sessions.

        Catching everything, not just OSError: a path can fail in ways
        that are not IO errors at all (an embedded null byte raises
        ValueError), and the one thing a post-mortem must never do is be
        the reason there is a post-mortem."""
        try:
            if os.path.getsize(self.log_path) > MAX_LOG_BYTES:
                os.replace(self.log_path, self.log_path + ".old")
        except Exception:
            pass
