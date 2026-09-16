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
# Fast enough to resolve a stutter. The heartbeat is one empty callback,
# so ten a second costs nothing measurable, and at 500ms a quarter-second
# stall was indistinguishable from a healthy gap between beats.
BEAT_EVERY_MS = 100
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

# And below that again: the stutters. Everything the user has ever called
# laggy in this app measured between 50 and 250 milliseconds - a page
# flip, a clip selected, a picture rebuilt on the wrong thread. Not one
# of them was ever close to LAGGY_AFTER, so the log recorded nothing at
# all about the only problem being reported. Two hundred milliseconds is
# about where a click stops feeling connected to what follows it.
#
# A stutter is not written out on its own line - a bad minute would be a
# hundred of them. They are counted by where the thread was, and the
# tally goes out on one summary when the app closes. That turns "it still
# feels slow" into a list of places, which is the whole point.
STUTTER_AFTER = 0.2
STUTTER_TOP = 8

# Construction that has to happen on the UI thread and cannot be broken
# up - building a tab's several hundred widgets - is announced here while
# it runs, so it is not counted as a stutter. Not to flatter the numbers:
# the report samples where the thread is when it notices a stall, and the
# innermost frame during a tab build is whichever button happened to be
# under construction. Three launches produced three different culprits,
# all of them innocent. Excluding known construction is what keeps the
# entries that remain worth reading.
_building = threading.Event()


def building(flag: bool) -> None:
    """Announce (or end) a stretch of unavoidable UI-thread building."""
    if flag:
        _building.set()
    else:
        _building.clear()


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
        # Stutters: how many, how bad, and where. Keyed by the line the
        # UI thread was on, so repeats of one slow thing add up into one
        # entry instead of a hundred log lines.
        self._stutters = 0
        self._worst = 0.0
        self._where: dict = {}
        self._counted_at = 0.0
        self._armed = False
        self._beats = 0

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
        self.summarise()

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
        now = time.monotonic()
        # Counting stutters only makes sense once there is an event loop
        # to stutter. Building the window takes over a second with no
        # loop running at all, which to the watcher is indistinguishable
        # from a one-second freeze - and it filled the report with
        # whichever button happened to be under construction. Two beats
        # close together mean the loop is alive; until then, nothing is
        # counted.
        # Two beats, not one: start() calls this itself before the loop
        # exists, and that first call would otherwise arm on the gap
        # since __init__ - which is microseconds.
        self._beats += 1
        if self._beats > 1 and now - self._beat <= STUTTER_AFTER:
            self._armed = True
        self._beat = now
        try:
            self.root.after(BEAT_EVERY_MS, self._tick)
        except Exception:
            pass

    def _watch(self) -> None:
        while not self._stop.wait(0.05):
            quiet = time.monotonic() - self._beat
            if quiet < STUTTER_AFTER:
                continue
            if quiet < LAGGY_AFTER:
                self._count_stutter(quiet)
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

    # ── stutters ────────────────────────────────────────────────────────

    def _count_stutter(self, quiet: float) -> None:
        """Tally a stall too short to log and too long to feel right.

        The thread is still inside the slow call while this runs, so its
        stack is the answer - which is why this is sampled here rather
        than worked out from the gap after the fact.
        """
        if not self._armed or _building.is_set():
            return                  # the window is still being built
        now = time.monotonic()
        # One sample per stall, not one per poll while it lasts.
        if now - self._counted_at < STUTTER_AFTER:
            return
        self._counted_at = now
        self._stutters += 1
        self._worst = max(self._worst, quiet)
        where = self._stutter_site()
        seen, total = self._where.get(where, (0, 0.0))
        self._where[where] = (seen + 1, total + quiet)

    def _stutter_site(self) -> str:
        """One line naming where the UI thread is: the innermost frame
        that belongs to this app, since the deepest frame is usually
        inside tkinter or PIL and every stall looks the same there."""
        frame = sys._current_frames().get(threading.main_thread().ident)
        best = "somewhere outside the app"
        while frame is not None:
            code = frame.f_code
            name = code.co_filename.replace("\\", "/")
            if "/paz_suite/" in name:
                short = name.rsplit("/paz_suite/", 1)[1]
                best = f"{short}:{frame.f_lineno} in {code.co_name}()"
                break
            frame = frame.f_back
        return best

    def summarise(self) -> None:
        """What this session felt like, in one entry. Written on the way
        out, so a session that stuttered says so even though no single
        stutter was worth a line of its own."""
        if not self._stutters:
            return
        lines = [f"{self._stutters} pauses over {int(STUTTER_AFTER * 1000)}ms "
                 f"this session, worst {self._worst * 1000:.0f}ms",
                 "where the window was, most often first:"]
        ranked = sorted(self._where.items(), key=lambda kv: -kv[1][0])
        for site, (seen, total) in ranked[:STUTTER_TOP]:
            lines.append(f"    {seen:5}x  avg {total / seen * 1000:5.0f}ms   {site}")
        self._note("STUTTERS", "\n".join(lines))

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
