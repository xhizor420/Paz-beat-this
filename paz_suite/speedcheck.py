"""Speed check: time the everyday actions on this machine, with this library.

Every number used to tune this app was measured on a test display on
another operating system. Those say what got faster; only the machine it
actually runs on can say how fast it is. This runs the things you do all
day - turning a page, clicking a clip, searching, sorting, the sidebar,
theater, switching tabs - a few times each, puts everything back the way
it was, and writes the timings to ~/.video_tool/speedcheck.txt.

Each action is timed from the call to the moment Tk has laid out and
drawn the result (update_idletasks), one at a time with a pause between,
so one action's leftovers are not billed to the next.

Nothing here changes the library: no clip is marked, tagged or moved,
and the search, sort, page, selection, sidebar and theater are restored
at the end.
"""

from __future__ import annotations

import os
import platform
import statistics
import sys
import time

from .config import CONFIG_DIR

REPORT_PATH = os.path.join(CONFIG_DIR, "speedcheck.txt")
PAUSE_MS = 300


class SpeedCheck:
    def __init__(self, app, library, done=None):
        self.app = app
        self.lib = library
        self.root = app.root
        self.done = done
        self.results: list = []
        self.steps: list = []
        self._saved: dict = {}

    # ── the plan ─────────────────────────────────────────────────────────

    def _plan(self) -> list:
        lib, app = self.lib, self.app
        clips = list(lib.page_recs())[:8]
        pages = max((len(lib.filtered) + lib.cfg.page_size - 1)
                    // max(lib.cfg.page_size, 1), 1)
        steps: list = []

        def add(label, fn, runs):
            steps.extend((label, fn) for _ in range(runs))

        if pages > 1:
            forward = [1, 1, 1, -1, -1, -1]
            if lib.page + 3 >= pages:
                forward = [-x for x in forward]
            for delta in forward:
                steps.append(("turn a page", lambda d=delta: lib.turn_page(d)))
        for rec in clips:
            steps.append(("click a clip", lambda r=rec: lib._select(r)))

        def search(text):
            def go():
                lib.search.delete(0, "end")
                if text:
                    lib.search.insert(0, text)
                lib.run_search()
            return go

        word = self._a_common_word()
        for text in (word[:1], word[:3], word, ""):
            steps.append(("a search", search(text)))

        def sort(name):
            def go():
                lib.sort_menu.set(name)
                lib.run_search()
            return go

        for name in ("Name", "Newest", "Score", "Name"):
            steps.append(("sort", sort(name)))
        add("open/close the sidebar", lib.toggle_sidebar, 4)
        add("theater on/off", lib.toggle_theater, 4)
        for _ in range(3):
            steps.append(("switch tab", lambda: app._select_tab("Vault")))
            steps.append(("switch tab", lambda: app._select_tab("Library")))
        return steps

    def _a_common_word(self) -> str:
        """A tag most of the library has, so the search does real work."""
        counts: dict = {}
        for rec in self.lib.records[:2000]:
            for tag in rec.tags:
                counts[tag] = counts.get(tag, 0) + 1
        if not counts:
            return "a"
        return max(counts, key=counts.get)

    # ── running ──────────────────────────────────────────────────────────

    def start(self) -> None:
        lib = self.lib
        self._saved = {
            "search": lib.search.get(), "sort": lib.sort_menu.get(),
            "page": lib.page, "selected": lib.selected,
            "sidebar": lib.cfg.sidebar_open, "theater": lib.cfg.theater,
        }
        self.steps = self._plan()
        self.started = time.perf_counter()
        self.root.after(PAUSE_MS, self._next)

    def _next(self) -> None:
        if not self.steps:
            self._restore()
            return
        label, fn = self.steps.pop(0)
        began = time.perf_counter()
        try:
            fn()
            self.root.update_idletasks()
        except Exception as exc:                      # report, never crash
            self.results.append((label, None, f"{type(exc).__name__}: {exc}"))
        else:
            self.results.append((label, (time.perf_counter() - began) * 1000, ""))
        self.root.after(PAUSE_MS, self._next)

    def _restore(self) -> None:
        lib, saved = self.lib, self._saved
        try:
            self.app._select_tab("Library")
            if lib.cfg.theater != saved["theater"]:
                lib.toggle_theater()
            if lib.cfg.sidebar_open != saved["sidebar"]:
                lib.toggle_sidebar(force=saved["sidebar"])
            lib.sort_menu.set(saved["sort"])
            lib.search.delete(0, "end")
            lib.search.insert(0, saved["search"])
            lib.run_search()
            lib.page = saved["page"]
            lib.render_page()
            if saved["selected"] is not None:
                lib._select(saved["selected"])
        except Exception:
            pass
        path = self.write_report()
        if self.done is not None:
            self.done(path, self.summary())

    # ── the report ───────────────────────────────────────────────────────

    def rows(self) -> list:
        """(label, runs, median, worst, errors) per action, in order."""
        order: list = []
        times: dict = {}
        errors: dict = {}
        for label, ms, error in self.results:
            if label not in times:
                order.append(label)
                times[label], errors[label] = [], []
            if ms is None:
                errors[label].append(error)
            else:
                times[label].append(ms)
        out = []
        for label in order:
            got = times[label]
            out.append((label, len(got),
                        statistics.median(got) if got else None,
                        max(got) if got else None, errors[label]))
        return out

    def summary(self) -> str:
        worst = [(w, label) for label, _n, _m, w, _e in self.rows() if w is not None]
        if not worst:
            return "Speed check could not time anything."
        ms, label = max(worst)
        return f"Speed check done - slowest was {label} at {ms:.0f} ms."

    def report_text(self) -> str:
        lib = self.lib
        from .theme import T
        lines = [
            "PAZ Suite speed check",
            time.strftime("%Y-%m-%d %H:%M"),
            "",
            f"System   {platform.platform()}",
            f"Python   {sys.version.split()[0]}   CPUs {os.cpu_count()}",
            f"Library  {len(lib.records):,} clips, {lib.cfg.page_size} per page, "
            f"display scale {T.SCALE:.2f}",
            "",
            f"{'action':26} {'runs':>4} {'median':>9} {'worst':>9}",
        ]
        for label, runs, median, worst, errors in self.rows():
            if median is None:
                lines.append(f"{label:26} {runs:>4} {'-':>9} {'-':>9}")
            else:
                flag = "   <- over 100 ms" if worst > 100 else (
                    "   <- over two frames" if worst > 33 else "")
                lines.append(f"{label:26} {runs:>4} {median:>7.1f}ms {worst:>7.1f}ms{flag}")
            for error in errors:
                lines.append(f"    failed: {error}")
        lines += ["", "Times are from the action to the window having drawn it.",
                  "One frame at 60 Hz is 16.7 ms; under 100 ms reads as instant."]
        return "\n".join(lines) + "\n"

    def write_report(self) -> str:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(REPORT_PATH, "w", encoding="utf-8") as fh:
                fh.write(self.report_text())
            return REPORT_PATH
        except OSError:
            return ""
