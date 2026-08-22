"""The Convert tab: batch-encode a source library to MP4 (GPU with CPU
fallback) and sort the results by resolution/frame rate. Scan, start, stop,
watch mode, duplicate finder, upscale-gap finder, promote-to-pool.
"""

from __future__ import annotations

import csv
import os
import shutil
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tkinter import messagebox, ttk

import customtkinter as ctk

from .theme import T, font, CONVERT_LABELS
from .format import fmt_time, fmt_clock, fmt_size
from .files import is_ignored_dir, in_ignored_path, post_id_from, open_file, open_in_explorer
from .media import check_dependencies, available_encoders, probe
from .convert_engine import (
    Task, GPU_ENCODERS, classify, plan_recipe, convert, transfer,
    Cancelled, _discard,
)
from .convert_widgets import (
    QueueTable, STATE_LABELS, ScrubPreview, ContactSheet, DuplicateWindow,
)
from .widgets import Card, Bar, StatTile, JobPanel, LogView, LibraryBar
from . import uithread


class ConvertTab(ctk.CTkFrame):

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.app = app
        self.root = app.root
        self.cfg = app.cfg
        self.cache = app.cache
        self.emeta = app.emeta
        self.toaster = app.toaster
        self.peek = app.peek
        self.tasks: dict = {}

        self.processing = False
        self.busy_tool = False           # promotion / other library jobs
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.watch_flag = threading.Event()
        self._watch_sizes: dict = {}
        self._active_folders: list = list(self.cfg.subfolders)
        self.started_at: float | None = None
        self.counts = {"done": 0, "failed": 0, "sorted": 0, "skipped": 0,
                       "gaps_found": 0, "gaps_copied": 0}
        self.bytes_done = 0
        self._lock = threading.Lock()
        self._throttle: dict = {}
        self._peek_after = None
        self._peek_token = 0
        self._peek_iid: str | None = None
        self._peek_busy = False
        self._peek_pending = None

        self.grid_columnconfigure(0, weight=5, uniform="cols")
        self.grid_columnconfigure(1, weight=3, uniform="cols")
        self.grid_rowconfigure(1, weight=1)
        self.root.filmstrip_frames = self.cfg.filmstrip_frames

        self._build()
        self.table.on_peek = self._peek
        self.table.on_peek_hide = self._peek_hide
        self.preview.on_grid = self._grid
        self._relabel()
        self._apply_brand()
        self.after(1500, self._refresh_gap_badge)
        self._check_environment()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        if self.cfg.watch and self.cfg.watch_resume:
            self.watch_switch.select()
            self.watch_flag.set()
            self.log("Watch mode re-armed from last session "
                     "(Settings to change)", "warn")

    # ── thread-safe UI ──────────────────────────────────────────────────────

    def ui(self, fn, *args, **kwargs):
        uithread.post(fn, *args, **kwargs)

    def log(self, message: str, level: str = "info"):
        self.ui(self.logview.write, message, level)

    # ── copy ─────────────────────────────────────────────────────────────

    def F(self, key: str, **fmt) -> str:
        text = CONVERT_LABELS[key]
        return text.format(**fmt) if fmt else text

    # ── layout ──────────────────────────────────────────────────────────────

    def _build(self):
        self._build_topbar()
        self._build_left()
        self._build_right()

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0, height=58)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=20, pady=10)

        self.brand_name = ctk.CTkLabel(left, text="PAZ", font=font(19, "bold"),
                                        text_color=T.ACCENT)
        self.brand_name.pack(side="left")
        self.brand_kind = ctk.CTkLabel(left, text="Studio", font=font(19),
                                        text_color=T.TEXT)
        self.brand_kind.pack(side="left", padx=(5, 0))
        self.brand_sub = ctk.CTkLabel(left, text="", font=font(10, mono=True),
                                       text_color=T.FAINT)
        self.brand_sub.pack(side="left", padx=(12, 0), pady=(6, 0))

        self.pill = ctk.CTkFrame(left, fg_color=T.INPUT, corner_radius=11,
                                  border_width=1, border_color=T.ACCENT_DEEP, height=22)
        self.pill.pack(side="left", padx=(18, 0))
        self.pill_dot = ctk.CTkFrame(self.pill, width=7, height=7, corner_radius=4,
                                      fg_color=T.FAINT)
        self.pill_dot.pack(side="left", padx=(9, 6), pady=7)
        self.pill_text = ctk.CTkLabel(self.pill, text="Idle", font=font(10),
                                       text_color=T.DIM)
        self.pill_text.pack(side="left", padx=(0, 11))

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=20)

        self.readout = ctk.CTkLabel(right, text="", font=font(11, mono=True),
                                     text_color=T.DIM)
        self.readout.pack(side="left", padx=(0, 18))

        ctk.CTkButton(right, text="Settings", width=88, height=30, corner_radius=7,
                      font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._open_settings).pack(side="left")

    def _build_left(self):
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)

        controls = Card(panel, border_color=T.ACCENT_DEEP)
        controls.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        controls.grid_columnconfigure(3, weight=1)

        picker = ctk.CTkFrame(controls, fg_color="transparent")
        picker.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=13)
        ctk.CTkLabel(picker, text="CATEGORY", font=font(9, "bold"),
                     text_color=T.FAINT).pack(anchor="w", pady=(0, 5))
        self.folder_menu = ctk.CTkOptionMenu(
            picker, values=["All categories"] + self.cfg.subfolders, width=170,
            height=32, font=font(11), corner_radius=7, fg_color=T.INPUT,
            button_color=T.LINE, button_hover_color=T.BTN_HOV,
            dropdown_fg_color=T.ELEVATED, text_color=T.TEXT,
            command=lambda _v: self._scan())
        self.folder_menu.set("All categories")
        self.folder_menu.pack(anchor="w")

        toggles = ctk.CTkFrame(controls, fg_color="transparent")
        toggles.grid(row=0, column=1, sticky="w", padx=18, pady=13)
        self.auto_preview = ctk.CTkSwitch(
            toggles, text="Preview follows the encoder", font=font(11), text_color=T.DIM,
            progress_color=T.ACCENT, button_color=T.TEXT, height=20,
            command=self._save_toggles)
        self.auto_preview.select() if self.cfg.auto_preview else self.auto_preview.deselect()
        self.auto_preview.pack(anchor="w", pady=(2, 6))
        self.reencode = ctk.CTkSwitch(
            toggles, text="Re-encode files that already exist", font=font(11),
            text_color=T.DIM, progress_color=T.ACCENT, button_color=T.TEXT,
            height=20, command=self._scan)
        self.reencode.pack(anchor="w")

        watch_col = ctk.CTkFrame(controls, fg_color="transparent")
        watch_col.grid(row=0, column=2, sticky="w", padx=0, pady=13)
        self.watch_switch = ctk.CTkSwitch(
            watch_col, text="Watch mode", font=font(11), text_color=T.DIM,
            progress_color=T.OK, button_color=T.TEXT, height=20,
            command=self._toggle_watch)
        self.watch_switch.pack(anchor="w", pady=(2, 2))
        ctk.CTkLabel(watch_col, text="Converts new files as they\nfinish downloading",
                     font=font(9), text_color=T.FAINT, justify="left"
                     ).pack(anchor="w")

        buttons = ctk.CTkFrame(controls, fg_color="transparent")
        buttons.grid(row=0, column=3, sticky="e", padx=(0, 14), pady=13)

        self.scan_btn = ctk.CTkButton(
            buttons, text="Scan folders", width=110, height=38, corner_radius=8,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.DIM, command=self._scan)
        self.scan_btn.pack(side="left", padx=(0, 8))

        self.pause_btn = ctk.CTkButton(
            buttons, text="Pause", width=84, height=38, corner_radius=8,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.DIM, state="disabled", command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            buttons, text="Stop", width=84, height=38, corner_radius=8,
            font=font(11, "bold"), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.DIM, state="disabled", command=self._stop)
        self.stop_btn.pack(side="left", padx=(0, 8))

        self.start_btn = ctk.CTkButton(
            buttons, text="Start", width=132, height=38, corner_radius=8,
            font=font(12, "bold"), fg_color=T.BTN_GO, hover_color=T.BTN_GO_H,
            text_color="#FFFFFF", command=self._start)
        self.start_btn.pack(side="left")

        tiles = ctk.CTkFrame(panel, fg_color="transparent")
        tiles.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        for i in range(6):
            tiles.grid_columnconfigure(i, weight=1)
        self.tiles = {}
        for i, (key, label, colour) in enumerate([
                ("queued", "In queue", T.TEXT),
                ("done", "Converted", T.OK),
                ("failed", "Failed", T.FAIL),
                ("sorted", "To pool", T.ACCENT),
                ("written", "Written", T.ACCENT2),
                ("eta", "Remaining", T.DIM)]):
            tile = StatTile(tiles, label, colour)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            self.tiles[key] = tile

        self.census = LibraryBar(panel)
        self.census.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8))

        self.overall = Bar(panel, height=4)
        self.overall.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.jobs = JobPanel(panel)
        self.jobs.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 8))

        self.table = QueueTable(panel, on_select=self._on_select,
                                 on_activate=self._on_activate)
        self.table.on_menu = self._build_menu
        self.table.grid(row=5, column=0, sticky="nsew", padx=14, pady=(0, 8))

        # Three clusters, thin dividers between them so the row reads as
        # groups instead of one undifferentiated run of buttons: what the
        # selected row can do -> queue-wide maintenance -> whole-library
        # tools (already right-aligned, furthest from the queue they don't
        # directly act on).
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=6, column=0, sticky="ew", padx=14, pady=(0, 14))

        def divider(parent):
            ctk.CTkFrame(parent, fg_color=T.LINE, width=1, height=22
                        ).pack(side="left", padx=8)

        row_cluster = ctk.CTkFrame(actions, fg_color="transparent")
        row_cluster.pack(side="left")
        self.row_buttons = []
        for text, command in (("Play file", self._play),
                              ("Show in folder", self._reveal)):
            button = ctk.CTkButton(
                row_cluster, text=text, height=30, corner_radius=7, font=font(11),
                fg_color=T.BTN, hover_color=T.BTN_HOV, text_color=T.DIM,
                width=110, command=command, state="disabled")
            button.pack(side="left", padx=(0, 7))
            self.row_buttons.append(button)

        divider(actions)

        maintenance = ctk.CTkFrame(actions, fg_color="transparent")
        maintenance.pack(side="left")
        ctk.CTkButton(maintenance, text="Retry failures", height=30, corner_radius=7,
                     font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.DIM, width=110, command=self._retry
                     ).pack(side="left", padx=(0, 7))
        ctk.CTkButton(maintenance, text="Export report", height=30, corner_radius=7,
                     font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.DIM, width=110, command=self._export
                     ).pack(side="left", padx=(0, 7))
        self.e621_btn = ctk.CTkButton(
            maintenance, text="Fetch e621 tags", height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.ACCENT, width=130, command=self._fetch_e621)
        self.e621_btn.pack(side="left")

        self.dupes_btn = ctk.CTkButton(
            actions, text="Find duplicates", height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.ACCENT2, width=140, command=self._duplicates)
        self.dupes_btn.pack(side="right", padx=(7, 0))
        self.promote_btn = ctk.CTkButton(
            actions, text="Promote upscales", height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.ACCENT2, width=150, command=self._promote)
        self.promote_btn.pack(side="right", padx=(7, 0))
        self.gaps_btn = ctk.CTkButton(
            actions, text="Find upscale gaps", height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.WARN, width=150, command=self._find_gaps)
        self.gaps_btn.pack(side="right", padx=(7, 0))
        self.gaps_btn.bind("<Button-3>", lambda e: self._list_gaps())

    def _build_right(self):
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=1, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=3)
        panel.grid_rowconfigure(3, weight=2)

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
        head.grid_columnconfigure(0, weight=1)
        self.inspector_head = ctk.CTkLabel(head, text="INSPECTOR",
                                            font=font(10, "bold"),
                                            text_color=T.FAINT)
        self.inspector_head.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text="click timeline · ←→ step · Space play/pause · G grid",
                     font=font(10), text_color=T.FAINT
                     ).grid(row=0, column=1, sticky="e")

        self.preview = ScrubPreview(panel, self.cache, self.cfg)
        self.preview.configure(border_color=T.ACCENT_DEEP)
        self.preview.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))

        self.logview = LogView(panel)
        self.logview.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 14))

    # ── keyboard handlers (dispatched centrally by the app) ────────────────

    @staticmethod
    def is_typing(event) -> bool:
        return isinstance(event.widget, (ctk.CTkEntry, tk.Entry, tk.Text,
                                          ttk.Entry, tk.Spinbox))

    def key_scan(self, event=None):
        self._scan()

    def key_start(self, event=None):
        self._start()

    def key_stop(self, event=None):
        self._stop()

    def key_find_search(self, event=None):
        self.table.search.focus_set()

    def key_scrub(self, event, seconds):
        if self.is_typing(event):
            return
        self.preview.step(seconds)

    def key_space(self, event):
        if self.is_typing(event):
            return
        self.preview.toggle_play()
        return "break"

    def key_grid(self, event):
        if self.is_typing(event):
            return
        self._grid()
        return "break"

    def key_peek_toggle(self, event):
        if self.is_typing(event):
            return
        self.cfg.hover_peek = not self.cfg.hover_peek
        self.cfg.save()
        self._peek_hide()
        self.log("Hover peek " + ("on" if self.cfg.hover_peek else "off"), "info")
        return "break"

    def _check_environment(self):
        self.logview.write("PAZ Suite — Convert", "head")
        self.logview.write("Keys: Space play/pause · ←→ step · G grid · "
                           "H hover peek · Ctrl+Enter start · Esc stop", "info")
        missing = check_dependencies()
        if missing:
            self.logview.write(f"Missing on PATH: {', '.join(missing)}. "
                               "Install FFmpeg before starting.", "fail")
            self._set_status("FFmpeg not found", T.FAIL)
            return
        have = available_encoders()
        gpu = GPU_ENCODERS.get(self.cfg.codec, "")
        if have and gpu in have:
            self.logview.write(f"GPU encoder ready: {gpu}", "ok")
        else:
            self.logview.write(f"{gpu} is not in this ffmpeg build. "
                               "Encoding will run on the CPU.", "warn")
        self._scan()

    # ── status ──────────────────────────────────────────────────────────────

    def _set_status(self, text: str, colour: str = T.FAINT):
        self.pill_dot.configure(fg_color=colour)
        self.pill_text.configure(text=text, text_color=colour)

    def _save_toggles(self):
        self.cfg.auto_preview = bool(self.auto_preview.get())
        self.cfg.save()

    def _apply_brand(self):
        self.brand_sub.configure(text=f"{self.F('tagline')} · Convert")

    def _relabel(self):
        self.scan_btn.configure(text=self.F("scan"))
        self.start_btn.configure(text=self.F("start"))
        self.stop_btn.configure(text=self.F("stop"))
        if not self.processing:
            self.pause_btn.configure(text=self.F("pause"))
        self.watch_switch.configure(text=self.F("watch"))
        self.promote_btn.configure(text=self.F("promote"))
        self.dupes_btn.configure(text=self.F("dupes"))
        self.gaps_btn.configure(text=self.F("gaps"))
        self.e621_btn.configure(text=self.F("fetch_tags"))
        self.inspector_head.configure(text=self.F("inspector"))
        self.logview.header_label.configure(text=self.F("log"))

    # ── hover peek ──────────────────────────────────────────────────────────

    def _peek(self, iid: str, frac: float, x_root: int, y_root: int):
        if not self.cfg.hover_peek:
            return
        task = self.tasks.get(iid)
        if not task:
            self._peek_hide()
            return
        if self._peek_after is not None:
            try:
                self.after_cancel(self._peek_after)
            except ValueError:
                pass
        self._peek_iid = iid
        self._peek_after = self.after(90, lambda: self._peek_fetch(task, frac, x_root, y_root))

    def _peek_fetch(self, task: Task, frac: float, x_root: int, y_root: int):
        self._peek_after = None
        if self._peek_iid != task.iid:
            return
        path = task.target if (task.state == "done"
                               and os.path.exists(task.target)) else task.source
        info = task.info
        if info is None or not info.duration:
            self.peek.show_text("reading file", task.name, x_root, y_root)
            return
        moment = max(0.0, min(frac * info.duration, info.duration - 0.05))
        self._peek_token += 1
        token = self._peek_token
        request = (task, path, moment, info.duration, token, x_root, y_root)
        if self._peek_busy:
            # One extraction in flight at a time - only the latest hover
            # position matters, so it replaces whatever was pending
            # instead of piling up overlapping ffmpeg calls.
            self._peek_pending = request
            return
        self._peek_busy = True
        self._peek_run(request)

    def _peek_run(self, request) -> None:
        task, path, moment, duration, token, x_root, y_root = request
        frac = moment / duration

        def work():
            # A pre-built sprite sheet crop instead of an ffmpeg spawn per
            # hover - see ThumbCache.hover_frame() in media.py.
            data = self.cache.hover_frame(path, duration, frac)
            self.ui(self._peek_done, task, data, moment, token, x_root, y_root)

        threading.Thread(target=work, daemon=True).start()

    def _peek_done(self, task: Task, data, moment, token, x_root, y_root) -> None:
        self._peek_busy = False
        if token == self._peek_token:
            self._peek_show(task, data, moment, token, x_root, y_root)
        pending = self._peek_pending
        self._peek_pending = None
        if pending is not None:
            self._peek_busy = True
            self._peek_run(pending)

    def _peek_show(self, task: Task, data, moment, token, x_root, y_root):
        if token != self._peek_token or self._peek_iid != task.iid:
            return
        title = task.name
        record = self.emeta.get(task.pid) if task.pid else None
        if record and record.get("artist"):
            title = f"{record['artist'][0]} · #{task.pid}"
        duration = task.info.duration if task.info else 0
        fraction = (moment / duration) if duration else None
        self.peek.show_frame(data, title, fmt_clock(moment), x_root, y_root,
                             fraction=fraction)

    def _peek_hide(self):
        self._peek_iid = None
        self._peek_token += 1
        self._peek_pending = None
        if self._peek_after is not None:
            try:
                self.after_cancel(self._peek_after)
            except ValueError:
                pass
            self._peek_after = None
        self.peek.hide()

    # ── contact sheet ───────────────────────────────────────────────────────

    def _grid(self):
        path = self.preview.active_path
        if not path:
            iid = self.table.selected
            task = self.tasks.get(iid) if iid else None
            if task:
                path = task.target if os.path.exists(task.target) else task.source
        if not path or not os.path.exists(path):
            self.log("Select a clip first, then press G for its contact sheet.", "info")
            return
        ContactSheet(self.root, self.cache, path, os.path.basename(path),
                     on_jump=self.preview.seek)

    # ── e621 lookup ─────────────────────────────────────────────────────────

    def _fetch_e621(self):
        if self.processing or self.busy_tool:
            self.log("Busy - try the tag fetch again when the current job "
                     "finishes.", "info")
            return
        if not self.cfg.e621_enabled:
            self.log("e621 lookup is turned off in Settings.", "info")
            return
        todo = [t for t in self.tasks.values()
               if t.pid and self.emeta.get(t.pid) is None]
        already = sum(1 for t in self.tasks.values()
                     if t.pid and self.emeta.get(t.pid) is not None)
        no_id = sum(1 for t in self.tasks.values() if not t.pid)
        if not todo:
            bits = [f"{already} already cached"] if already else []
            if no_id:
                bits.append(f"{no_id} without a post ID in the name")
            self.log("Nothing to fetch" + (f" ({', '.join(bits)})" if bits else "") + ".",
                     "info")
            return

        self.busy_tool = True
        self.e621_btn.configure(state="disabled")
        self._set_status(self.F("fetching"), T.ACCENT2)
        delay = max(float(self.cfg.e621_fetch_delay), 0.5)
        self.log(f"{self.F('fetching')} · {len(todo)} posts "
                 f"(~{fmt_time(len(todo) * (delay + 0.1))})", "head")

        def work():
            hits = miss = 0
            try:
                for index, task in enumerate(todo):
                    record = self.emeta.fetch(task.pid, self.cfg.e621_user, self.cfg.e621_key)
                    if record.get("error") and not record.get("missing"):
                        miss += 1
                        self.log(f"  #{task.pid}: {record['error']}", "warn")
                    elif record.get("missing"):
                        miss += 1
                        self.ui(self._apply_meta, task)
                    else:
                        hits += 1
                        self.ui(self._apply_meta, task)
                    if index % 5 == 0:
                        self.ui(self._set_status,
                                f"{self.F('fetching')} {index + 1}/{len(todo)}", T.ACCENT2)
                    if index % 10 == 9:
                        self.emeta.save()
                    if index + 1 < len(todo):
                        time.sleep(delay)
            finally:
                self.emeta.save()
                self.busy_tool = False
                self.ui(self.e621_btn.configure, state="normal")
                self.ui(self._set_status,
                        self.F("watching") if self.watch_flag.is_set() else self.F("idle"),
                        T.ACCENT if self.watch_flag.is_set() else T.FAINT)
                message = self.F("fetch_done", n=hits, m=miss)
                self.log(message, "ok" if hits else "warn")
                self.ui(self.toaster.show, message, "ok" if hits else "warn")
                self.ui(self.table.refresh_filter)

        threading.Thread(target=work, daemon=True).start()

    def _apply_meta(self, task: Task):
        record = self.emeta.get(task.pid) if task.pid else None
        if not record:
            return
        if record.get("missing"):
            self.table.set_row(task.iid, artist="?", rating="--")
            return
        self.table.set_row(
            task.iid,
            artist=", ".join(record.get("artist") or []) or "--",
            rating=(record.get("rating") or "").upper() or "--",
            _tags=record.get("tags", ""))

    def _open_post(self, task: Task):
        import webbrowser
        from .e621 import E621_POST
        record = self.emeta.get(task.pid) if task.pid else None
        url = (record or {}).get("url") or E621_POST.format(pid=task.pid)
        try:
            webbrowser.open(url)
        except Exception:
            self._copy(url)
            self.log("Could not open a browser - the post URL is on the "
                     "clipboard instead.", "warn")

    # ── watch mode ──────────────────────────────────────────────────────────

    def _toggle_watch(self):
        enabled = bool(self.watch_switch.get())
        self.cfg.watch = enabled
        self.cfg.save()
        if enabled:
            self._watch_sizes.clear()
            self.watch_flag.set()
            self._set_status(self.F("watching"), T.ACCENT)
            self.log("Watch mode on. New files convert once their size "
                     "stops changing.", "ok")
        else:
            self.watch_flag.clear()
            if not self.processing:
                self._set_status(self.F("idle"), T.FAINT)
            self.log("Watch mode off", "info")

    def _watch_candidates(self) -> list:
        extensions = self.cfg.source_ext_set
        found = []
        for folder in list(self._active_folders):
            source_dir = os.path.join(self.cfg.source_root, folder)
            target_dir = os.path.join(self.cfg.output_root, folder)
            if not os.path.isdir(source_dir):
                continue
            try:
                names = [n for n in os.listdir(source_dir) if not is_ignored_dir(n)]
            except OSError:
                continue
            for name in names:
                stem, ext = os.path.splitext(name)
                if ext.lower() not in extensions:
                    continue
                target = os.path.join(target_dir, stem + ".mp4")
                if os.path.exists(target) and os.path.getsize(target) > 0:
                    continue
                found.append(os.path.join(source_dir, name))
        return found

    def _watch_loop(self):
        while True:
            self.watch_flag.wait()
            time.sleep(5)
            if not self.watch_flag.is_set() or self.processing or self.busy_tool:
                continue
            try:
                candidates = self._watch_candidates()
            except Exception:
                continue
            if not candidates:
                self._watch_sizes.clear()
                continue
            all_stable = True
            for path in candidates:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    all_stable = False
                    continue
                if self._watch_sizes.get(path) != size:
                    all_stable = False
                self._watch_sizes[path] = size
            if all_stable:
                self._watch_sizes.clear()
                self.ui(self._watch_fire, len(candidates))

    def _watch_fire(self, count: int):
        if self.processing or self.busy_tool or not self.watch_flag.is_set():
            return
        message = self.F("watch_new", n=count, s="s" if count != 1 else "")
        self.log(message, "head")
        self.toaster.show(message, "accent")
        self._scan()
        if any(t.state == "queued" for t in self.tasks.values()):
            self._start()

    # ── library census ──────────────────────────────────────────────────────

    def refresh_census(self):
        def work():
            def survey(root_dir):
                count = total = 0
                stems = set()
                for folder in self.cfg.subfolders:
                    directory = os.path.join(root_dir, folder)
                    if not os.path.isdir(directory):
                        continue
                    try:
                        names = os.listdir(directory)
                    except OSError:
                        continue
                    for name in names:
                        if not name.lower().endswith(".mp4"):
                            continue
                        stems.add(os.path.splitext(name)[0])
                        count += 1
                        try:
                            total += os.path.getsize(os.path.join(directory, name))
                        except OSError:
                            pass
                return count, total, stems

            pool_n, pool_b, pool_s = survey(self.cfg.premium_root)
            wait_n, wait_b, wait_s = survey(self.cfg.upscale_root)
            out_n, _out_b, out_s = survey(self.cfg.output_root)
            unsorted_n = len(out_s - pool_s - wait_s)
            self.ui(self.census.set, pool_n, pool_b, wait_n, wait_b, unsorted_n)

        threading.Thread(target=work, daemon=True).start()

    # ── promotion / upscale-gap tools ────────────────────────────────────────

    def _gap_scan(self) -> dict:
        found = {}
        for folder in self.cfg.subfolders:
            source_dir = os.path.join(self.cfg.output_root, folder)
            premium_dir = os.path.join(self.cfg.premium_root, folder)
            if not os.path.isdir(source_dir):
                continue

            def stems(directory):
                try:
                    with os.scandir(directory) as entries:
                        return {os.path.splitext(e.name)[0].lower()
                               for e in entries if e.is_file()
                               and os.path.splitext(e.name)[1].lower() == ".mp4"
                               and not in_ignored_path(e.path)}
                except OSError:
                    return set()

            have = stems(premium_dir)
            names = []
            try:
                with os.scandir(source_dir) as entries:
                    for entry in entries:
                        if not entry.is_file():
                            continue
                        if os.path.splitext(entry.name)[1].lower() != ".mp4":
                            continue
                        if os.path.splitext(entry.name)[0].lower() not in have:
                            names.append(entry.name)
            except OSError:
                continue
            if names:
                found[folder] = sorted(names)
        return found

    def _refresh_gap_badge(self):
        try:
            total = sum(len(v) for v in self._gap_scan().values())
        except Exception:
            return
        if total:
            self.gaps_btn.configure(text=f"{self.F('gaps')} ({total})",
                                    fg_color=T.WARN_DEEP, text_color=T.WARN)
        else:
            self.gaps_btn.configure(text=self.F("gaps"), fg_color=T.BTN, text_color=T.FAINT)

    def _list_gaps(self):
        if self.busy_tool:
            return

        def work():
            found = self._gap_scan()
            self.log("-" * 46, "head")
            if not found:
                self.log("No gaps - every converted clip has a 4K 60+ version.", "ok")
            else:
                total = sum(len(v) for v in found.values())
                self.log(f"{total} clips have no 4K 60+ version. Nothing was "
                         f"copied - left-click to queue them.", "head")
                for folder, names in found.items():
                    self.log(f"  {folder}: {len(names)}", "warn")
                    for name in names[:10]:
                        self.log(f"    {name}", "info")
                    if len(names) > 10:
                        self.log(f"    ... and {len(names) - 10} more", "info")
            self.ui(self._refresh_gap_badge)

        threading.Thread(target=work, daemon=True).start()

    def _gap_sweep(self) -> dict:
        copied = already = missing_total = skipped_low = 0
        bytes_copied = 0
        for folder in self.cfg.subfolders:
            if self.cancel.is_set():
                break
            source_dir = os.path.join(self.cfg.output_root, folder)
            premium_dir = os.path.join(self.cfg.premium_root, folder)
            todo_dir = os.path.join(self.cfg.upscale_root, folder)
            if not os.path.isdir(source_dir):
                continue

            def stems(directory):
                try:
                    with os.scandir(directory) as entries:
                        return {os.path.splitext(e.name)[0].lower()
                               for e in entries if e.is_file()
                               and os.path.splitext(e.name)[1].lower() == ".mp4"
                               and not in_ignored_path(e.path)}
                except OSError:
                    return set()

            have = stems(premium_dir)
            queued = stems(todo_dir)

            try:
                with os.scandir(source_dir) as entries:
                    files = sorted((e for e in entries if e.is_file()),
                                   key=lambda e: e.name.lower())
            except OSError:
                continue

            for entry in files:
                if self.cancel.is_set():
                    break
                stem = os.path.splitext(entry.name)[0].lower()
                if os.path.splitext(entry.name)[1].lower() != ".mp4":
                    continue
                if stem in have:
                    continue
                missing_total += 1
                if stem in queued:
                    already += 1
                    continue

                info = probe(entry.path)
                if info and info.width >= 3800 and info.fps >= 50:
                    skipped_low += 1
                    self.log(f"  {entry.name} is already 4K60 - use "
                             f"Promote, not the upscaler", "warn")
                    continue

                dest = os.path.join(todo_dir, entry.name)
                try:
                    os.makedirs(todo_dir, exist_ok=True)
                    shutil.copy2(entry.path, dest)
                    copied += 1
                    bytes_copied += entry.stat().st_size
                    detail = f"{info.resolution} @ {info.fps_text}" if info else ""
                    self.log(f"Needs upscale  {folder}/{entry.name}   {detail}", "ok")
                except (OSError, shutil.Error) as exc:
                    self.log(f"Could not copy {entry.name}: {exc}", "warn")

        return {"missing": missing_total, "copied": copied,
               "already": already, "skipped_low": skipped_low,
               "bytes_copied": bytes_copied}

    def _report_gap_sweep(self, stats: dict):
        if stats["missing"] == 0:
            self.log("No gaps - every converted clip has a 4K 60+ version.", "ok")
        else:
            self.log(f"{stats['missing']} clips have no 4K 60+ version: "
                     f"{stats['copied']} copied to the needs-work folder"
                     + (f", {stats['already']} already queued" if stats['already'] else "")
                     + (f", {stats['skipped_low']} already 4K60" if stats['skipped_low'] else ""),
                     "head")
            if stats["copied"]:
                self.log(f"Copied {fmt_size(stats['bytes_copied'])} into "
                         f"{self.cfg.upscale_root}", "ok")
                self.ui(self.toaster.show, f"{stats['copied']} clips need upscaling", "accent")

    def _find_gaps(self):
        if self.processing or self.busy_tool:
            return
        self.busy_tool = True
        self._set_status(self.F("gapping"), T.ACCENT2)
        self.log("Comparing the converted library against the 4K 60+ pool", "head")

        def work():
            try:
                stats = self._gap_sweep()
                self._report_gap_sweep(stats)
            finally:
                self.busy_tool = False
                self.ui(self._set_status, self.F("idle"), T.FAINT)
                self.refresh_census()
                self.ui(self._refresh_gap_badge)

        threading.Thread(target=work, daemon=True).start()

    def _gap_phase(self):
        self.ui(self._set_status, self.F("gapping"), T.ACCENT2)
        self.log("Double-checking: every converted clip against the 4K 60+ pool", "head")
        stats = self._gap_sweep()
        self._report_gap_sweep(stats)
        self.ui(self._refresh_gap_badge)
        with self._lock:
            self.counts["gaps_found"] = stats["missing"]
            self.counts["gaps_copied"] = stats["copied"]

    def _promote(self):
        if self.processing or self.busy_tool:
            return
        self.busy_tool = True
        self._set_status(self.F("checkup"), T.ACCENT2)
        self.log("Re-checking the needs-work folder for finished upscales", "head")

        def work():
            promoted = skipped = examined = 0
            try:
                for folder in self.cfg.subfolders:
                    directory = os.path.join(self.cfg.upscale_root, folder)
                    if not os.path.isdir(directory):
                        continue
                    for name in sorted(os.listdir(directory)):
                        if not name.lower().endswith(".mp4"):
                            continue
                        path = os.path.join(directory, name)
                        examined += 1
                        info = probe(path)
                        if not info or not info.width:
                            continue
                        root_dir, label = classify(self.cfg, info.width, info.height, info.fps)
                        if root_dir != self.cfg.premium_root:
                            continue
                        dest_dir = os.path.join(self.cfg.premium_root, folder)
                        dest = os.path.join(dest_dir, name)
                        if os.path.exists(dest):
                            skipped += 1
                            continue
                        try:
                            os.makedirs(dest_dir, exist_ok=True)
                            shutil.move(path, dest)
                            promoted += 1
                            self.log(f"Promoted  {name}   {info.resolution} @ "
                                     f"{info.fps_text}", "ok")
                        except (OSError, shutil.Error) as exc:
                            self.log(f"Could not promote {name}: {exc}", "warn")
            finally:
                self.busy_tool = False
                if promoted:
                    self.log(f"Promoted {promoted} of {examined} checked "
                             f"into the edit pool", "ok")
                else:
                    self.log(f"Checked {examined} files; none meet the bar yet", "info")
                if skipped:
                    self.log(f"{skipped} skipped: a file with that name is "
                             f"already in the edit pool", "warn")
                self.ui(self._set_status, self.F("idle"), T.FAINT)
                self.refresh_census()

        threading.Thread(target=work, daemon=True).start()

    def _duplicates(self):
        DuplicateWindow(self.root, self)

    def _open_settings(self):
        self.app.open_settings(initial_tab="Encoding")

    def after_settings_saved(self):
        self.root.filmstrip_frames = self.cfg.filmstrip_frames
        self.folder_menu.configure(values=["All categories"] + self.cfg.subfolders)
        self.folder_menu.set("All categories")
        self._relabel()
        self._apply_brand()
        self.logview.write("Settings saved", "ok")
        self._scan()

    # ── scanning ────────────────────────────────────────────────────────────

    def _folders(self) -> list:
        choice = self.folder_menu.get()
        picked = list(self.cfg.subfolders) if choice == "All categories" else [choice]
        self._active_folders = picked
        return picked

    def _scan(self):
        if self.processing:
            return
        self._peek_hide()
        self.table.clear()
        self.tasks.clear()
        self.counts = {"done": 0, "failed": 0, "sorted": 0, "skipped": 0,
                       "gaps_found": 0, "gaps_copied": 0}
        self.bytes_done = 0
        self.preview.clear(self.F("no_selection"))
        self.overall.reset()

        extensions = self.cfg.source_ext_set
        overwrite = bool(self.reencode.get())
        found = skipped = 0
        missing_dirs = []

        for folder in self._folders():
            source_dir = os.path.join(self.cfg.source_root, folder)
            target_dir = os.path.join(self.cfg.output_root, folder)
            if not os.path.isdir(source_dir):
                missing_dirs.append(source_dir)
                continue
            try:
                names = sorted(os.listdir(source_dir))
            except OSError:
                missing_dirs.append(source_dir)
                continue

            for name in names:
                stem, ext = os.path.splitext(name)
                if ext.lower() not in extensions:
                    continue
                source = os.path.join(source_dir, name)
                if not os.path.isfile(source):
                    continue
                target = os.path.join(target_dir, stem + ".mp4")
                exists = os.path.exists(target) and os.path.getsize(target) > 0
                if exists and not overwrite:
                    skipped += 1
                    continue

                iid = f"t{len(self.tasks)}"
                task = Task(iid=iid, source=source, target=target,
                           folder=folder, name=name, pid=post_id_from(name))
                self.tasks[iid] = task
                cells = {}
                record = self.emeta.get(task.pid) if task.pid else None
                if record and not record.get("missing"):
                    cells = {
                        "artist": ", ".join(record.get("artist") or []) or "--",
                        "rating": (record.get("rating") or "").upper() or "--",
                        "_tags": record.get("tags", ""),
                    }
                self.table.add(iid, name, "queued", **cells)
                found += 1

        for path in missing_dirs:
            self.logview.write(f"Folder not found: {path}", "warn")

        self.counts["skipped"] = skipped
        self.tiles["queued"].set(str(found))
        self.tiles["done"].set("0")
        self.tiles["failed"].set("0")
        self.tiles["sorted"].set("0")
        self.tiles["written"].set("--")
        self.tiles["eta"].set("--")
        self.readout.configure(text=f"{found} queued  ·  {skipped} already converted")

        self.refresh_census()
        if found:
            self.logview.write(f"Scanned {len(self._folders())} categories: "
                               f"{found} to convert, {skipped} already done", "info")
            self._probe_queue()
        else:
            self.table.empty_hint(self.F("empty"))
            self.logview.write(self.F("nothing_msg"), "info")

    def _probe_queue(self):
        """
        Fill in resolution/length/size for every queued file.

        Probing is I/O-bound (each call spawns ffprobe), so a handful run
        in parallel instead of one at a time - a queue of a few hundred
        files fills in noticeably faster on any multi-core machine.
        """
        pending = [t for t in self.tasks.values() if t.info is None]
        if not pending:
            return
        workers = min(8, max(2, (os.cpu_count() or 4)))

        def probe_one(task):
            return task, probe(task.source)

        def work():
            total_seconds = 0.0
            total_bytes = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(probe_one, task) for task in pending]
                for future in as_completed(futures):
                    task, info = future.result()
                    if info is None:
                        continue
                    total_seconds += info.duration
                    total_bytes += info.size
                    task.info = info
                    task.seconds = info.duration
                    _root, label = classify(self.cfg, info.width, info.height, info.fps)
                    fps_text = info.fps_text + (" ~" if info.vfr else "")
                    dur_text = fmt_clock(info.duration)
                    if self.cfg.loop_short and 0 < info.duration < self.cfg.loop_min:
                        import math as _math
                        copies = int(_math.ceil(self.cfg.loop_min / info.duration))
                        dur_text += f" ×{copies}"
                    self.ui(self.table.set_row, task.iid,
                            res=info.resolution, fps=fps_text, dur=dur_text,
                            size=fmt_size(info.size), dest="→ " + label,
                            _res=info.width * info.height, _fps=info.fps,
                            _dur=info.duration, _size=info.size)
            if not self.processing and total_seconds:
                self.ui(self.readout.configure,
                        text=f"{len(pending)} queued · "
                             f"{fmt_time(total_seconds)} of footage · "
                             f"{fmt_size(total_bytes)}")

        threading.Thread(target=work, daemon=True).start()

    # ── run ─────────────────────────────────────────────────────────────────

    def _start(self):
        if self.processing:
            return
        missing = check_dependencies()
        if missing:
            messagebox.showerror(
                "FFmpeg not found",
                f"Cannot find {', '.join(missing)} on your PATH.\n\n"
                "Install FFmpeg and reopen the app.", parent=self)
            return

        pending = [t for t in self.tasks.values() if t.state == "queued"]
        if not pending and not self.cfg.sort_enabled and not self.cfg.gap_check_enabled:
            self.logview.write("Nothing queued and Sort/Gap-check are both "
                               "off. Scan first, or turn one of those on.", "warn")
            return

        self.processing = True
        self.cancel.clear()
        self.pause.clear()
        self.started_at = time.time()
        self.counts["done"] = self.counts["failed"] = self.counts["sorted"] = 0
        self.counts["gaps_found"] = self.counts["gaps_copied"] = 0
        self.bytes_done = 0
        self._peek_hide()

        self.start_btn.configure(state="disabled", fg_color=T.BTN, text_color=T.FAINT)
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal", fg_color=T.BTN_STOP,
                                hover_color=T.BTN_STOP_H, text_color="#FFFFFF")
        self.pause_btn.configure(state="normal", text=self.F("pause"))
        self._set_status(self.F("encoding"), T.ACCENT)

        self.logview.write("─" * 46, "head")
        if pending:
            self.logview.write(
                self.F("run_start", n=len(pending), w=self.cfg.workers,
                       s="s" if self.cfg.workers > 1 else ""), "head")
        else:
            steps = []
            if self.cfg.sort_enabled:
                steps.append("sort")
            if self.cfg.gap_check_enabled:
                steps.append("check for upscale gaps")
            self.logview.write(
                "Nothing queued to convert - running " + " and ".join(steps)
                + " only.", "head")

        self._tick()
        threading.Thread(target=self._run, args=(pending,), daemon=True).start()

    def _stop(self):
        if not self.processing:
            return
        self.cancel.set()
        self.pause.clear()
        self._set_status(self.F("stopping"), T.WARN)
        self.logview.write("Stop requested. Killing the current encode.", "warn")

    def _toggle_pause(self):
        if not self.processing:
            return
        if self.pause.is_set():
            self.pause.clear()
            self.pause_btn.configure(text=self.F("pause"), fg_color=T.BTN, text_color=T.DIM)
            self._set_status(self.F("encoding"), T.ACCENT)
        else:
            self.pause.set()
            self.pause_btn.configure(text=self.F("resume"), fg_color=T.WARN_DEEP,
                                     text_color=T.WARN)
            self._set_status(self.F("paused"), T.WARN)

    def _run(self, pending: list):
        try:
            workers = max(1, min(int(self.cfg.workers), 8))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._encode, task) for task in pending]
                for future in as_completed(futures):
                    future.result()

            if self.cfg.sort_enabled and not self.cancel.is_set():
                self._sort_phase()

            if self.cfg.gap_check_enabled and not self.cancel.is_set():
                self._gap_phase()

            self._report(pending)
        except Exception as exc:
            self.log(f"Run failed: {exc}", "fail")
        finally:
            self.ui(self._finish)

    def _encode(self, task: Task):
        while self.pause.is_set() and not self.cancel.is_set():
            time.sleep(0.2)
        if self.cancel.is_set():
            task.state = "cancelled"
            self.ui(self.table.set_row, task.iid, state="cancelled")
            return

        os.makedirs(os.path.dirname(task.target), exist_ok=True)

        task.state = "running"
        self.ui(self.table.set_row, task.iid, state="running")
        self.ui(self.jobs.start, task.iid, task.name)
        if self.cfg.auto_preview:
            self.ui(self._follow, task)

        started = time.time()

        def on_progress(snapshot):
            now = time.time()
            if now - self._throttle.get(task.iid, 0) < 0.12 and not snapshot.get("done"):
                return
            self._throttle[task.iid] = now
            stage = snapshot.get("stage")
            if stage:
                self.ui(self.jobs.set_progress, task.iid, 0, f"{stage} encode")
                return
            bits = [f"{snapshot['fraction'] * 100:3.0f}%"]
            if snapshot.get("speed"):
                bits.append(snapshot["speed"])
            if snapshot.get("eta"):
                bits.append(fmt_time(snapshot["eta"]))
            self.ui(self.jobs.set_progress, task.iid, snapshot["fraction"], "  ".join(bits))

        try:
            ok, error, encoder = convert(
                task.source, task.target, self.cfg,
                progress_cb=on_progress, cancel=self.cancel,
                log=lambda m, l="info": self.log(f"  {task.name}: {m}", l))
        except Cancelled:
            task.state = "cancelled"
            self.ui(self.table.set_row, task.iid, state="cancelled")
            self.ui(self.jobs.finish, task.iid)
            self.log(f"Stopped  {task.name}", "warn")
            return
        except Exception as exc:
            ok, error, encoder = False, str(exc), ""

        elapsed = time.time() - started
        task.encoder = encoder

        if ok:
            task.state = "done"
            info = probe(task.target)
            task.info = info
            cells = {"state": "done"}
            if info:
                cells.update(res=info.resolution,
                            fps=info.fps_text + (" ~" if info.vfr else ""),
                            dur=fmt_clock(info.duration), size=fmt_size(info.size),
                            _res=info.width * info.height, _fps=info.fps,
                            _dur=info.duration, _size=info.size)
            self.ui(self.table.set_row, task.iid, **cells)
            self.log(f"Done  {task.name}   {encoder} · {fmt_time(elapsed)}", "ok")
        else:
            task.state = "failed"
            task.error = error
            self.ui(self.table.set_row, task.iid, state="failed", dest="--")
            self.log(f"Failed  {task.name}", "fail")
            for line in (error or "").splitlines()[:4]:
                self.log(f"        {line}", "fail")

        self.ui(self.jobs.finish, task.iid)
        with self._lock:
            self.counts["done" if ok else "failed"] += 1
            if ok and task.info:
                self.bytes_done += task.info.size
        self.ui(self._refresh_counts)

    def _follow(self, task: Task):
        current = self.table.selected
        if not current or current == task.iid:
            self.preview.load(task.source)

    # ── sorting ─────────────────────────────────────────────────────────────

    def _sort_phase(self):
        self.ui(self._set_status, self.F("sorting"), T.ACCENT2)
        self.log("Sorting converted files", "head")

        candidates = []
        for task in self.tasks.values():
            if task.state == "done":
                candidates.append((task, task.target))

        if self.cfg.sort_existing:
            known = {os.path.normcase(t.target) for t in self.tasks.values()}
            for folder in self._folders():
                out_dir = os.path.join(self.cfg.output_root, folder)
                if not os.path.isdir(out_dir):
                    continue
                for name in sorted(os.listdir(out_dir)):
                    if not name.lower().endswith(".mp4"):
                        continue
                    path = os.path.join(out_dir, name)
                    if os.path.normcase(path) not in known:
                        candidates.append((None, path))

        moved = 0
        for task, path in candidates:
            if self.cancel.is_set():
                break
            info = probe(path)
            if not info or not info.width:
                continue
            root_dir, label = classify(self.cfg, info.width, info.height, info.fps)
            folder = task.folder if task else os.path.basename(os.path.dirname(path))
            dest_dir = os.path.join(root_dir, folder)
            dest = os.path.join(dest_dir, os.path.basename(path))
            if os.path.exists(dest):
                if task:
                    task.dest = label
                    self.ui(self.table.set_row, task.iid, dest=label)
                continue
            try:
                os.makedirs(dest_dir, exist_ok=True)
                transfer(path, dest, self.cfg.transfer_mode)
            except (OSError, shutil.Error) as exc:
                self.log(f"Could not sort {os.path.basename(path)}: {exc}", "warn")
                continue
            moved += 1
            if task:
                task.dest = label
                self.ui(self.table.set_row, task.iid, dest=label)

        with self._lock:
            self.counts["sorted"] = moved
        self.ui(self._refresh_counts)
        self.log(f"Sorted {moved} files", "ok")
        self.refresh_census()

    # ── finishing ───────────────────────────────────────────────────────────

    def _report(self, pending: list):
        elapsed = time.time() - (self.started_at or time.time())
        done = self.counts["done"]
        failed = self.counts["failed"]
        total_bytes = sum(t.info.size for t in self.tasks.values()
                          if t.state == "done" and t.info)

        self.log("─" * 46, "head")
        self.log(f"Finished in {fmt_time(elapsed)}", "head")
        gaps_line = ""
        if self.cfg.gap_check_enabled:
            found = self.counts.get("gaps_found", 0)
            copied = self.counts.get("gaps_copied", 0)
            gaps_line = ("   0 upscale gaps" if found == 0 else
                        f"   {found} upscale gap{'s' if found != 1 else ''} "
                        f"found, {copied} queued")
        self.log(f"  {done} converted   {failed} failed   "
                 f"{self.counts['sorted']} sorted{gaps_line}",
                 "ok" if not failed else "warn")
        if done:
            self.log(f"  {fmt_size(total_bytes)} written   "
                     f"{fmt_time(elapsed / max(done, 1))} average per file", "info")

        failures = [t for t in self.tasks.values() if t.state == "failed"]
        if failures:
            self._write_failure_report(failures)

    def _write_failure_report(self, failures: list):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(self.cfg.log_dir, f"conversion_failures_{stamp}.txt")
        try:
            os.makedirs(self.cfg.log_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"Conversion failures - {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                fh.write("=" * 60 + "\n\n")
                for task in failures:
                    fh.write(f"File:   {task.name}\n")
                    fh.write(f"Source: {task.source}\n")
                    fh.write(f"Error:  {task.error}\n")
                    fh.write("-" * 60 + "\n\n")
            self.log(f"Failure report: {path}", "info")
        except OSError as exc:
            self.log(f"Could not write the failure report: {exc}", "warn")

    def _finish(self):
        self.processing = False
        self._peek_hide()
        self.jobs.clear()
        self.start_btn.configure(state="normal", fg_color=T.BTN_GO, text_color="#FFFFFF")
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled", fg_color=T.BTN, text_color=T.DIM)
        self.pause_btn.configure(state="disabled", text=self.F("pause"),
                                 fg_color=T.BTN, text_color=T.DIM)
        stopped = self.cancel.is_set()
        if self.watch_flag.is_set():
            self._set_status(self.F("watching"), T.ACCENT)
        else:
            self._set_status(self.F("stopped") if stopped else self.F("finished"),
                             T.WARN if stopped else T.OK)
        self.tiles["eta"].set("--")
        self.overall.set(1.0 if not stopped else self.overall_fraction(),
                         T.OK if not stopped else T.WARN)
        summary = self.F("run_done", d=self.counts["done"], f=self.counts["failed"],
                         srt=self.counts["sorted"])
        if stopped:
            summary = "Stopped early · " + summary
        self.toaster.show(summary, "warn" if (stopped or self.counts["failed"]) else "ok")

    def overall_fraction(self) -> float:
        total = len(self.tasks) or 1
        return (self.counts["done"] + self.counts["failed"]) / total

    def _refresh_counts(self):
        queued = sum(1 for t in self.tasks.values() if t.state in ("queued", "running"))
        self.tiles["queued"].set(str(queued))
        self.tiles["done"].set(str(self.counts["done"]))
        self.tiles["failed"].set(str(self.counts["failed"]))
        self.tiles["sorted"].set(str(self.counts["sorted"]))
        self.tiles["written"].set(fmt_size(self.bytes_done) if self.bytes_done else "--")
        self.overall.set(self.overall_fraction())

    def _tick(self):
        if not self.processing or not self.started_at:
            return
        elapsed = time.time() - self.started_at
        finished = self.counts["done"] + self.counts["failed"]
        total = len(self.tasks)
        if finished:
            remaining = (total - finished) * (elapsed / finished)
            self.tiles["eta"].set(fmt_time(remaining))
        self.readout.configure(
            text=f"{finished}/{total} files  ·  {fmt_time(elapsed)} elapsed")
        self.after(1000, self._tick)

    # ── row interaction ─────────────────────────────────────────────────────

    def _on_select(self, iid: str):
        task = self.tasks.get(iid)
        if not task:
            return
        has_output = os.path.exists(task.target)
        self.row_buttons[0].configure(
            state="normal" if has_output or os.path.exists(task.source) else "disabled")
        self.row_buttons[1].configure(state="normal")
        prefer = "Output" if has_output else "Source"
        self.preview.load(task.source, task.target if has_output else None, prefer=prefer)
        if task.error:
            self.preview.set_note(task.error.splitlines()[0])
            return
        if task.state == "queued" and task.info:
            notes = plan_recipe(self.cfg, task.info).notes
            if notes:
                self.preview.set_note("Will be " + " · ".join(notes), T.ACCENT)
                return
        record = self.emeta.get(task.pid) if task.pid else None
        if record and not record.get("missing"):
            bits = []
            if record.get("artist"):
                bits.append(", ".join(record["artist"][:2]))
            rating = record.get("rating")
            if rating:
                bits.append({"e": "Explicit", "q": "Questionable",
                            "s": "Safe"}.get(rating, rating.upper()))
            bits.append(f"e621 #{task.pid}")
            self.preview.set_note("  ·  ".join(bits), T.ACCENT2)

    def _on_activate(self, iid: str):
        task = self.tasks.get(iid)
        if task:
            open_file(task.target if os.path.exists(task.target) else task.source)

    def _play(self):
        iid = self.table.selected
        if iid:
            self._on_activate(iid)

    def _reveal(self):
        iid = self.table.selected
        task = self.tasks.get(iid) if iid else None
        if task:
            open_in_explorer(task.target if os.path.exists(task.target) else task.source)

    def _build_menu(self, iid: str, menu: tk.Menu):
        task = self.tasks.get(iid)
        if not task:
            return
        has_output = os.path.exists(task.target)
        menu.add_command(label="Play converted file",
                         state="normal" if has_output else "disabled",
                         command=lambda: open_file(task.target))
        menu.add_command(label="Play original", command=lambda: open_file(task.source))
        menu.add_command(label="Show in folder",
                         command=lambda: open_in_explorer(task.target if has_output
                                                           else task.source))
        menu.add_separator()
        menu.add_command(label="Copy source path", command=lambda: self._copy(task.source))
        if task.pid:
            menu.add_command(label=f"Open e621 post #{task.pid}",
                             command=lambda: self._open_post(task))
            record = self.emeta.get(task.pid)
            if record and record.get("tags"):
                menu.add_command(label="Copy tags", command=lambda: self._copy(record["tags"]))
        if task.error:
            menu.add_command(label="Copy error", command=lambda: self._copy(task.error))
        menu.add_separator()
        menu.add_command(label="Queue again",
                         state="disabled" if self.processing else "normal",
                         command=lambda: self._requeue([task]))
        if has_output:
            menu.add_command(label="Delete converted file",
                             command=lambda: self._delete_output(task))

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _delete_output(self, task: Task):
        if not messagebox.askyesno("Delete file",
                                   f"Delete the converted copy of {task.name}?", parent=self):
            return
        _discard(task.target)
        task.state = "queued"
        self.table.set_row(task.iid, state="queued", dest="--")
        self.logview.write(f"Deleted the converted copy of {task.name}", "warn")

    def _requeue(self, tasks: list):
        for task in tasks:
            task.state = "queued"
            task.error = None
            self.table.set_row(task.iid, state="queued", dest="--")
        self._refresh_counts()

    def _retry(self):
        if self.processing:
            return
        failures = [t for t in self.tasks.values() if t.state in ("failed", "cancelled")]
        if not failures:
            self.logview.write("No failures to retry.", "info")
            return
        self._requeue(failures)
        self.logview.write(f"Re-queued {len(failures)} files", "info")
        self._start()

    def _export(self):
        from tkinter import filedialog
        if not self.tasks:
            self.logview.write("Nothing to export yet.", "info")
            return
        path = filedialog.asksaveasfilename(
            title="Save report", defaultextension=".csv",
            initialfile=f"video_report_{datetime.now():%Y-%m-%d_%H%M}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["File", "Post ID", "Artist", "Rating", "Category",
                                 "Status", "Resolution", "FPS", "Length", "Size",
                                 "Destination", "Encoder", "Source", "Output", "Tags",
                                 "Error"])
                for task in self.tasks.values():
                    info = task.info
                    record = (self.emeta.get(task.pid) or {}) if task.pid else {}
                    writer.writerow([
                        task.name, task.pid, ", ".join(record.get("artist") or []),
                        (record.get("rating") or "").upper(), task.folder,
                        STATE_LABELS.get(task.state, ""),
                        info.resolution if info else "", info.fps_text if info else "",
                        fmt_clock(info.duration) if info else "",
                        info.size if info else "",
                        task.dest, task.encoder, task.source, task.target,
                        record.get("tags", ""), (task.error or "").replace("\n", " | "),
                    ])
            self.logview.write(f"Report saved: {path}", "ok")
        except OSError as exc:
            self.logview.write(f"Could not save the report: {exc}", "fail")

    def on_app_close(self) -> bool:
        """Return True if it is safe to close (called by the app shell)."""
        if self.processing:
            if not messagebox.askyesno(
                    "Quit", "Encoding is still running. Stop and quit?", parent=self):
                return False
            self.cancel.set()
            time.sleep(0.3)
        return True
