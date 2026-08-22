"""The Beat This tab: pick a song, run the CPJKU "Beat This!" beat tracker
on it, and turn the result into markers a video editor can use - either a
CMX3600 EDL that DaVinci Resolve imports natively (Timeline > Import >
Timeline Markers from EDL), or, best-effort, markers dropped straight into
Resolve's currently open timeline if Resolve happens to be running right
here with scripting enabled.

All the actual analysis and file-writing lives in beat_engine.py (no UI);
this module is the CustomTkinter shell around it, plus the background
thread so a 3-4 minute song doesn't freeze the window while the model runs.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import customtkinter as ctk

from .theme import T, font, BEAT_LABELS
from .format import fmt_clock, fmt_len
from . import beat_engine as be
from . import uithread
from .widgets import Card, StatTile, JobPanel, LogView


class BeatTab(ctk.CTkFrame):

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.app = app
        self.root = app.root
        self.cfg = app.cfg

        self._audio_path = ""
        self._result = None
        self._busy = False
        self._rows: dict = {}
        self._missing: list = []
        self._announced_setup = False

        self.grid_columnconfigure(0, weight=1, uniform="cols")
        self.grid_columnconfigure(1, weight=1, uniform="cols")
        self.grid_rowconfigure(1, weight=1)

        self._build()
        self._check_deps()
        self.set_status(self.F("idle"), T.FAINT)

    @property
    def checkpoint(self) -> str:
        """The model to run. Settings can override it; anything unknown
        (a hand-edited config, a name dropped from a later version) falls
        back to the recommended one rather than failing at analysis time."""
        return be.normalize_checkpoint(self.cfg.beat_checkpoint)

    # ── copy ─────────────────────────────────────────────────────────────

    def F(self, key: str, **fmt) -> str:
        text = BEAT_LABELS[key]
        return text.format(**fmt) if fmt else text

    # ── thread-safe UI ──────────────────────────────────────────────────

    def ui(self, fn, *args, **kwargs) -> None:
        uithread.post(fn, *args, **kwargs)

    # ── layout ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._build_topbar()
        self._build_left()
        self._build_right()

    def _build_topbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0, height=58)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=20, pady=10)
        ctk.CTkLabel(left, text="PAZ", font=font(19, "bold"),
                     text_color=T.ACCENT4).pack(side="left")
        ctk.CTkLabel(left, text="Beat This", font=font(19), text_color=T.TEXT
                     ).pack(side="left", padx=(5, 0))
        ctk.CTkLabel(left, text=self.F("tagline"), font=font(10, mono=True),
                     text_color=T.FAINT).pack(side="left", padx=(12, 0), pady=(6, 0))

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=20)
        ctk.CTkButton(right, text="?", width=30, height=30, corner_radius=7,
                     font=font(12, "bold"), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.FAINT, command=self._open_help).pack(side="left")

    # ── left: song + run controls ────────────────────────────────────────

    def _build_left(self) -> None:
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=14)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)

        self._build_setup(panel)

        song = Card(panel, title="Song")
        song.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        song.grid_columnconfigure(0, weight=1)
        row = ctk.CTkFrame(song, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 12))
        row.grid_columnconfigure(0, weight=1)
        self.file_entry = ctk.CTkEntry(row, height=32, font=font(11, mono=True),
                                       fg_color=T.INPUT, border_color=T.ACCENT4_DEEP,
                                       border_width=1, text_color=T.TEXT,
                                       placeholder_text="No song chosen")
        self.file_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(row, text="Browse…", width=90, height=32, corner_radius=7,
                     font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.DIM, command=self._browse_audio
                     ).grid(row=0, column=1, padx=(8, 0))

        # No model picker here on purpose. There is one right answer -
        # the ensemble of all three main checkpoints - and every other
        # choice beat_this ships is either the same model with a different
        # random seed or a smaller, less accurate one. Making that a
        # decision on the way to pressing Analyze only invites picking
        # something worse. The overrides still exist for anyone who wants
        # them, in Settings > e621 & App > Beat This.

        run_row = ctk.CTkFrame(panel, fg_color="transparent")
        run_row.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.analyze_btn = ctk.CTkButton(
            run_row, text="Analyze", width=120, height=34, corner_radius=7,
            font=font(12, "bold"), fg_color=T.ACCENT4_DEEP, hover_color=T.BTN_HOV,
            text_color=T.ACCENT4, command=self._analyze)
        self.analyze_btn.pack(side="left")
        self.status_label = ctk.CTkLabel(run_row, text="", font=font(10),
                                         text_color=T.DIM, anchor="w", justify="left",
                                         wraplength=440)
        self.status_label.pack(side="left", padx=(12, 0))

        self.jobs = JobPanel(panel)
        self.jobs.grid(row=4, column=0, sticky="ew", pady=(0, 10))

        self.logview = LogView(panel)
        self.logview.grid(row=5, column=0, sticky="nsew")

    # ── setup panel ──────────────────────────────────────────────────────
    #
    # The beat tracker's dependencies are heavy, platform-specific and not
    # part of the suite's own requirements.txt, and the model checkpoint is
    # fetched on first use. Both are invisible failure modes - an "install
    # these" line in a README doesn't help when the button just doesn't
    # work - so the tab states what's missing and can fix it in place.

    def _build_setup(self, panel) -> None:
        card = Card(panel, title="Setup")
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        card.grid_columnconfigure(0, weight=1)

        self.setup_label = ctk.CTkLabel(
            card, text="Checking…", font=font(10, mono=True), text_color=T.DIM,
            anchor="w", justify="left", wraplength=560)
        self.setup_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 0))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=14, pady=(8, 12))
        self.install_btn = ctk.CTkButton(
            row, text="Install dependencies", width=150, height=30, corner_radius=7,
            font=font(11, "bold"), fg_color=T.ACCENT4_DEEP, hover_color=T.BTN_HOV,
            text_color=T.ACCENT4, command=self._install_deps)
        self.install_btn.pack(side="left")
        self.download_btn = ctk.CTkButton(
            row, text="Download model", width=130, height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV, text_color=T.DIM,
            command=self._download_checkpoint)
        self.download_btn.pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="Re-check", width=90, height=30, corner_radius=7,
                     font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.DIM, command=self._refresh_setup
                     ).pack(side="left", padx=(8, 0))

    def _refresh_setup(self) -> None:
        """Kick off a dependency probe. Off the UI thread on purpose:
        importing torch takes seconds, and this runs while the app window
        is still coming up."""
        self.setup_label.configure(text="Checking…", text_color=T.DIM)
        for btn in (self.install_btn, self.download_btn):
            btn.configure(state="disabled")
        threading.Thread(target=self._probe_setup, args=(self.checkpoint,),
                         daemon=True).start()

    def _probe_setup(self, checkpoint: str) -> None:
        rows = be.dependency_status()
        self.ui(self._apply_setup, rows, be.has_ffmpeg(),
                be.missing_checkpoints(checkpoint), checkpoint)

    def _apply_setup(self, rows: list, ffmpeg_ok: bool, missing_ckpt: list,
                      checkpoint: str) -> None:
        cached = not missing_ckpt
        missing = [pkg for pkg, _p, ok, _d in rows if not ok]
        self._missing = missing
        bits = []
        for package, _purpose, ok, detail in rows:
            mark = "✓" if ok else "✗"
            version = f" {detail}" if ok and detail else ""
            bits.append(f"{mark} {package}{version}")
        bits.append("✓ ffmpeg" if ffmpeg_ok else "✗ ffmpeg (needed to read audio)")
        # For the ensemble, "not cached" can mean one of three files is
        # missing - worth saying, since that's a much shorter download.
        total = len(be.checkpoint_parts(checkpoint))
        if cached:
            have = f"✓ model {checkpoint}"
        elif total > 1:
            have = (f"· model {checkpoint} ({total - len(missing_ckpt)} of "
                    f"{total} downloaded)")
        else:
            have = f"· model {checkpoint} (downloads on first run)"
        bits.append(have)

        self.setup_label.configure(text="   ".join(bits),
                                   text_color=T.OK if (not missing and ffmpeg_ok) else T.WARN)
        self.install_btn.configure(
            state="disabled" if (self._busy or not missing) else "normal",
            text="Install dependencies" if missing else "All installed")
        self.download_btn.configure(
            state="disabled" if (self._busy or missing or cached) else "normal",
            text="Downloaded" if cached else "Download model")
        self.analyze_btn.configure(
            state="disabled" if (self._busy or missing) else "normal")

        if not self._announced_setup:
            self._announced_setup = True
            if missing:
                self.logview.write(self.F("missing_deps"), "warn")
                self.logview.write("Missing: " + ", ".join(missing), "info")
            elif not ffmpeg_ok:
                self.logview.write("ffmpeg isn't on PATH - it's needed to read "
                                    "audio files. Install it and press Re-check.", "warn")

    def _install_deps(self) -> None:
        if self._busy:
            return
        missing = list(self._missing)
        if not missing:
            self._refresh_setup()
            return
        self._busy = True
        self._refresh_setup()
        self.set_status("Installing " + ", ".join(missing) + "…", T.DIM)
        self.logview.write("Installing: " + ", ".join(missing), "head")
        self.jobs.start("install", "pip install")
        self.jobs.set_progress("install", 0.5, "running")
        threading.Thread(target=self._run_install, args=(missing,), daemon=True).start()

    def _run_install(self, packages: list) -> None:
        ok, msg = be.install_dependencies(
            packages, progress_cb=lambda line: self.ui(self.logview.write, line, "info"))
        self.ui(self._install_done, ok, msg)

    def _install_done(self, ok: bool, msg: str) -> None:
        self._busy = False
        self.jobs.finish("install")
        self.logview.write(msg, "ok" if ok else "fail")
        self.set_status(msg, T.OK if ok else T.FAIL)
        self._refresh_setup()

    def _download_checkpoint(self) -> None:
        if self._busy:
            return
        checkpoint = self.checkpoint
        self._busy = True
        self._refresh_setup()
        self.set_status(f"Downloading '{checkpoint}'…", T.DIM)
        self.jobs.start("download", f"checkpoint {checkpoint}")
        self.jobs.set_progress("download", 0.5, "downloading")
        threading.Thread(target=self._run_download, args=(checkpoint,), daemon=True).start()

    def _run_download(self, checkpoint: str) -> None:
        ok, msg = be.download_checkpoint(
            checkpoint, progress_cb=lambda line: self.ui(self.logview.write, line, "info"))
        self.ui(self._download_done, ok, msg)

    def _download_done(self, ok: bool, msg: str) -> None:
        self._busy = False
        self.jobs.finish("download")
        self.logview.write(msg, "ok" if ok else "fail")
        self.set_status(msg, T.OK if ok else T.FAIL)
        self._refresh_setup()

    # ── right: results + export ──────────────────────────────────────────

    def _build_right(self) -> None:
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=14)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        stats = ctk.CTkFrame(panel, fg_color="transparent")
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for c in range(4):
            stats.grid_columnconfigure(c, weight=1, uniform="stats")
        self.tile_bpm = StatTile(stats, "BPM", T.ACCENT4)
        self.tile_bpm.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.tile_beats = StatTile(stats, "Beats", T.TEXT)
        self.tile_beats.grid(row=0, column=1, sticky="ew", padx=6)
        self.tile_downbeats = StatTile(stats, "Downbeats", T.TEXT)
        self.tile_downbeats.grid(row=0, column=2, sticky="ew", padx=6)
        self.tile_duration = StatTile(stats, "Length", T.TEXT)
        self.tile_duration.grid(row=0, column=3, sticky="ew", padx=(6, 0))

        ctk.CTkLabel(panel, text="BEATS", font=font(9, "bold"), text_color=T.FAINT,
                     anchor="w").grid(row=1, column=0, sticky="w", pady=(0, 4))

        self._build_style()
        tree_wrap = ctk.CTkFrame(panel, fg_color=T.SURFACE, corner_radius=12,
                                 border_width=1, border_color=T.ACCENT4_DEEP)
        tree_wrap.grid(row=2, column=0, sticky="nsew")
        tree_wrap.grid_columnconfigure(0, weight=1)
        tree_wrap.grid_rowconfigure(0, weight=1)

        columns = ("time", "beat", "kind")
        self.tree = ttk.Treeview(tree_wrap, style="B.Treeview", columns=columns,
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in (
                ("time", "Time", 90, "e"), ("beat", "Beat", 60, "center"),
                ("kind", "", 110, "w")):
            self.tree.column(key, width=width, minwidth=40, anchor=anchor)
            self.tree.heading(key, text=title)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = ctk.CTkScrollbar(tree_wrap, command=self.tree.yview, width=12,
                                  button_color=T.LINE, button_hover_color=T.FAINT)
        scroll.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=8)
        self.tree.configure(yscrollcommand=scroll.set)

        export = Card(panel, title="Export markers")
        export.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        export.grid_columnconfigure(0, weight=1)
        body = ctk.CTkFrame(export, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 4))
        for c in (1, 3, 5):
            body.grid_columnconfigure(c, weight=1)

        ctk.CTkLabel(body, text="Frame rate", font=font(11), text_color=T.DIM
                     ).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.fps_box = ctk.CTkComboBox(
            body, width=90, height=30, corner_radius=7, font=font(11),
            fg_color=T.INPUT, border_color=T.ACCENT4_DEEP, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED, dropdown_hover_color=T.ACCENT4_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11),
            text_color=T.TEXT, values=[str(f) for f in be.FRAME_RATES], state="readonly")
        self.fps_box.set(str(self.cfg.beat_fps) if self.cfg.beat_fps in be.FRAME_RATES
                         else "30.0")
        self.fps_box.grid(row=0, column=1, sticky="w", pady=4)

        ctk.CTkLabel(body, text="Beat colour", font=font(11), text_color=T.DIM
                     ).grid(row=0, column=2, sticky="w", padx=(16, 6), pady=4)
        self.beat_color_box = ctk.CTkComboBox(
            body, width=100, height=30, corner_radius=7, font=font(11),
            fg_color=T.INPUT, border_color=T.ACCENT4_DEEP, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED, dropdown_hover_color=T.ACCENT4_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11),
            text_color=T.TEXT, values=list(be.MARKER_COLORS), state="readonly")
        self.beat_color_box.set(self.cfg.beat_beat_color if self.cfg.beat_beat_color
                                in be.MARKER_COLORS else "Blue")
        self.beat_color_box.grid(row=0, column=3, sticky="w", pady=4)

        ctk.CTkLabel(body, text="Downbeat colour", font=font(11), text_color=T.DIM
                     ).grid(row=0, column=4, sticky="w", padx=(16, 6), pady=4)
        self.down_color_box = ctk.CTkComboBox(
            body, width=100, height=30, corner_radius=7, font=font(11),
            fg_color=T.INPUT, border_color=T.ACCENT4_DEEP, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED, dropdown_hover_color=T.ACCENT4_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11),
            text_color=T.TEXT, values=list(be.MARKER_COLORS), state="readonly")
        self.down_color_box.set(self.cfg.beat_downbeat_color
                                if self.cfg.beat_downbeat_color in be.MARKER_COLORS
                                else "Red")
        self.down_color_box.grid(row=0, column=5, sticky="w", pady=4)

        ctk.CTkLabel(body, text="Timeline starts at", font=font(11), text_color=T.DIM
                     ).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(10, 4))
        self.start_tc_box = ctk.CTkComboBox(
            body, width=120, height=30, corner_radius=7, font=font(11, mono=True),
            fg_color=T.INPUT, border_color=T.ACCENT4_DEEP, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED,
            dropdown_hover_color=T.ACCENT4_DEEP, dropdown_text_color=T.TEXT,
            dropdown_font=font(11), text_color=T.TEXT,
            values=list(be.START_CHOICES))
        self.start_tc_box.set(self.cfg.beat_start_tc or be.DEFAULT_START_TC)
        self.start_tc_box.grid(row=1, column=1, sticky="w", pady=(10, 4))

        self.downbeats_only_switch = ctk.CTkSwitch(
            body, text="Downbeats only", font=font(11), text_color=T.DIM,
            progress_color=T.ACCENT4, button_color=T.TEXT)
        (self.downbeats_only_switch.select() if self.cfg.beat_downbeats_only
         else self.downbeats_only_switch.deselect())
        self.downbeats_only_switch.grid(row=1, column=2, columnspan=2, sticky="w",
                                        padx=(16, 0), pady=(10, 4))

        btn_row = ctk.CTkFrame(export, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 12))
        self.save_tsv_btn = ctk.CTkButton(
            btn_row, text="Save .beats", width=110, height=32, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV, text_color=T.DIM,
            state="disabled", command=self._save_tsv)
        self.save_tsv_btn.pack(side="left")
        self.save_edl_btn = ctk.CTkButton(
            btn_row, text="Save EDL for Resolve", width=160, height=32, corner_radius=7,
            font=font(11, "bold"), fg_color=T.ACCENT4_DEEP, hover_color=T.BTN_HOV,
            text_color=T.ACCENT4, state="disabled", command=self._save_edl)
        self.save_edl_btn.pack(side="left", padx=(8, 0))
        self.send_resolve_btn = ctk.CTkButton(
            btn_row, text="Send to Resolve now", width=160, height=32, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV, text_color=T.DIM,
            state="disabled", command=self._send_resolve)
        self.send_resolve_btn.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(export, text=(
            "EDL markers land at absolute record timecode, so 'Timeline starts "
            "at' has to match your timeline's own start - Resolve uses "
            "01:00:00:00 for a new one, which is why an EDL written from "
            "00:00:00:00 imports and leaves you with nothing. Put the song at "
            "the very start of the timeline, then Timeline > Import > Timeline "
            "Markers from EDL. 'Send to Resolve now' needs none of that, but "
            "does need Resolve open here with Preferences > System > General > "
            "'External scripting using' set to Local."),
            font=font(10), text_color=T.FAINT, wraplength=560, justify="left"
            ).grid(row=3, column=0, sticky="w", padx=14, pady=(0, 12))

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("B.Treeview", background=T.ROW, fieldbackground=T.ROW,
                        foreground=T.TEXT, rowheight=26, borderwidth=0, font=(T.UI, 10))
        style.configure("B.Treeview.Heading", background=T.ELEVATED, foreground=T.FAINT,
                        relief="flat", borderwidth=0, font=(T.UI, 9, "bold"), padding=(8, 7))
        style.map("B.Treeview.Heading", background=[("active", T.BTN_HOV)])
        style.map("B.Treeview", background=[("selected", T.ROW_SEL)],
                  foreground=[("selected", T.TEXT)])

    # ── status ──────────────────────────────────────────────────────────

    def set_status(self, text: str, colour: str = T.DIM) -> None:
        self.status_label.configure(text=text, text_color=colour)

    def _open_help(self) -> None:
        win = ctk.CTkToplevel(self.root)
        win.title("Beat This help")
        win.geometry("560x420")
        win.configure(fg_color=T.BG)
        text = ctk.CTkTextbox(win, fg_color=T.SURFACE, text_color=T.TEXT,
                              font=font(11), wrap="word")
        text.pack(fill="both", expand=True, padx=14, pady=14)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

    # ── dependency check ──────────────────────────────────────────────────

    def _check_deps(self) -> None:
        self._refresh_setup()

    # ── song picking ──────────────────────────────────────────────────────

    AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.aiff *.wma"),
                   ("All files", "*.*")]

    def _browse_audio(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a song", parent=self.root,
            initialdir=self.cfg.beat_last_audio_dir or os.path.expanduser("~"),
            filetypes=self.AUDIO_TYPES)
        if path:
            self._set_audio(path)

    def _set_audio(self, path: str) -> None:
        self._audio_path = path
        self.file_entry.delete(0, tk.END)
        self.file_entry.insert(0, path)
        self.cfg.beat_last_audio_dir = os.path.dirname(path)
        self.cfg.save()
        self._clear_results()
        self.set_status(f"Ready to analyze: {os.path.basename(path)}", T.DIM)

    # ── analysis ──────────────────────────────────────────────────────────

    def key_analyze(self) -> None:
        self._analyze()

    def _analyze(self) -> None:
        if self._busy:
            return
        if not self._audio_path:
            self.set_status(self.F("no_file"), T.WARN)
            return
        if self._missing:
            self.set_status(self.F("missing_deps"), T.WARN)
            self.logview.write("Missing: " + ", ".join(self._missing), "fail")
            return

        checkpoint = self.checkpoint
        device_choice = self.cfg.beat_device
        dbn = bool(self.cfg.beat_dbn)
        float16 = bool(self.cfg.beat_float16)

        self._busy = True
        self._refresh_setup()
        self._clear_results()
        name = os.path.basename(self._audio_path)
        self.jobs.start("analyze", name)
        self.jobs.set_progress("analyze", 0.05, "starting")
        self.logview.write(f"Analyzing {name}…", "head")
        threading.Thread(target=self._run_analyze,
                         args=(self._audio_path, checkpoint, device_choice, dbn, float16),
                         daemon=True).start()

    def _run_analyze(self, path: str, checkpoint: str, device_choice: str,
                      dbn: bool, float16: bool) -> None:
        try:
            device = be.resolve_device(device_choice)
        except Exception as exc:
            self.ui(self._analyze_failed, str(exc))
            return

        def progress(msg: str) -> None:
            fraction = 0.25 if msg.lower().startswith("loading") else 0.65
            self.ui(self.jobs.set_progress, "analyze", fraction, msg)
            self.ui(self.logview.write, msg, "info")

        try:
            result = be.analyze(path, checkpoint=checkpoint, device=device,
                                dbn=dbn, float16=float16, progress_cb=progress)
        except Exception as exc:
            self.ui(self._analyze_failed, str(exc))
            return
        self.ui(self._analyze_done, result)

    def _analyze_failed(self, message: str) -> None:
        self._busy = False
        self._refresh_setup()
        self.jobs.finish("analyze")
        self.logview.write(f"Analysis failed: {message}", "fail")
        self.set_status("Analysis failed - see log.", T.FAIL)

    def _analyze_done(self, result) -> None:
        self._busy = False
        self._refresh_setup()
        self.jobs.set_progress("analyze", 1.0, "done", T.OK)
        self.jobs.finish("analyze")
        self._result = result
        self._fill_results(result)
        msg = self.F("analyze_done", n=len(result.beats), d=len(result.downbeats),
                     bpm=result.bpm)
        self.logview.write(msg, "ok")
        self.set_status(msg, T.OK)
        for btn in (self.save_tsv_btn, self.save_edl_btn, self.send_resolve_btn):
            btn.configure(state="normal")

    # ── results table ──────────────────────────────────────────────────────

    def _clear_results(self) -> None:
        self._result = None
        self.tree.delete(*self.tree.get_children())
        self.tile_bpm.set("--")
        self.tile_beats.set("--")
        self.tile_downbeats.set("--")
        self.tile_duration.set("--")
        for btn in (self.save_tsv_btn, self.save_edl_btn, self.send_resolve_btn):
            btn.configure(state="disabled")

    def _fill_results(self, result) -> None:
        self.tile_bpm.set(f"{result.bpm:.1f}" if result.bpm else "--")
        self.tile_beats.set(str(len(result.beats)))
        self.tile_downbeats.set(str(len(result.downbeats)))
        self.tile_duration.set(fmt_len(result.duration))

        self.tree.delete(*self.tree.get_children())
        for index, (time, number, is_down) in enumerate(
                zip(result.beats, result.beat_numbers, result.is_downbeat)):
            iid = f"b{index}"
            self.tree.insert("", "end", iid=iid, values=(
                fmt_clock(float(time)), int(number), "Downbeat" if is_down else "Beat"))

    # ── export ──────────────────────────────────────────────────────────

    def _save_tsv(self) -> None:
        if not self._result:
            self.set_status(self.F("no_result"), T.WARN)
            return
        stem = os.path.splitext(os.path.basename(self._result.audio_path))[0]
        path = filedialog.asksaveasfilename(
            title="Save .beats file", parent=self.root, defaultextension=".beats",
            initialdir=self.cfg.beat_last_export_dir or None,
            initialfile=f"{stem}.beats", filetypes=[("Beats", "*.beats"), ("All files", "*.*")])
        if not path:
            return
        try:
            be.save_beats_tsv(self._result, path)
        except OSError as exc:
            self.set_status(f"Couldn't save: {exc}", T.FAIL)
            return
        self.cfg.beat_last_export_dir = os.path.dirname(path)
        self.cfg.save()
        self.set_status(self.F("saved_tsv", path=path), T.OK)
        self.logview.write(f"Saved {path}", "ok")

    def _save_edl(self) -> None:
        if not self._result:
            self.set_status(self.F("no_result"), T.WARN)
            return
        fps, beat_color, down_color, downbeats_only, start_tc = self._export_settings()
        stem = os.path.splitext(os.path.basename(self._result.audio_path))[0]
        path = filedialog.asksaveasfilename(
            title="Save EDL for Resolve", parent=self.root, defaultextension=".edl",
            initialdir=self.cfg.beat_last_export_dir or None,
            initialfile=f"{stem}_markers.edl", filetypes=[("EDL", "*.edl"), ("All files", "*.*")])
        if not path:
            return
        try:
            be.save_edl(self._result, path, fps=fps, beat_color=beat_color,
                       downbeat_color=down_color, downbeats_only=downbeats_only,
                       start_tc=start_tc)
        except OSError as exc:
            self.set_status(f"Couldn't save: {exc}", T.FAIL)
            return
        self.cfg.beat_last_export_dir = os.path.dirname(path)
        self.cfg.save()
        self.set_status(self.F("saved_edl", path=path), T.OK)
        self.logview.write(f"Saved {path}", "ok")

    def _send_resolve(self) -> None:
        if not self._result:
            self.set_status(self.F("no_result"), T.WARN)
            return
        _fps, beat_color, down_color, only, _start = self._export_settings()
        self.send_resolve_btn.configure(state="disabled")
        self.set_status("Sending to Resolve…", T.DIM)
        threading.Thread(target=self._run_send_resolve,
                         args=(beat_color, down_color, only), daemon=True).start()

    def _run_send_resolve(self, beat_color: str, down_color: str, only: bool) -> None:
        ok, msg = be.send_to_resolve(self._result, beat_color=beat_color,
                                     downbeat_color=down_color,
                                     downbeats_only=only)
        self.ui(self._send_resolve_done, ok, msg)

    def _send_resolve_done(self, ok: bool, msg: str) -> None:
        self.send_resolve_btn.configure(state="normal")
        self.logview.write(msg, "ok" if ok else "fail")
        # The failure text is a multi-line checklist naming the paths that
        # were tried; the status line is one line tall, so it gets the
        # headline and the log keeps the detail.
        self.set_status(msg.split("\n")[0], T.OK if ok else T.FAIL)

    def _export_settings(self):
        fps = float(self.fps_box.get())
        beat_color = self.beat_color_box.get()
        down_color = self.down_color_box.get()
        downbeats_only = bool(self.downbeats_only_switch.get())
        start_tc = (self.start_tc_box.get() or "").strip() or be.DEFAULT_START_TC
        self.cfg.beat_fps = fps
        self.cfg.beat_beat_color = beat_color
        self.cfg.beat_downbeat_color = down_color
        self.cfg.beat_downbeats_only = downbeats_only
        self.cfg.beat_start_tc = start_tc
        self.cfg.save()
        return fps, beat_color, down_color, downbeats_only, start_tc

    # ── keyboard / lifecycle (dispatched centrally by the app) ─────────

    def on_app_close(self) -> bool:
        return True

    def after_settings_saved(self) -> None:
        # The model can be changed from Settings, and the Setup line is the
        # only place this tab names it - so re-probe rather than leave it
        # reporting the old one's download state.
        self._refresh_setup()


HELP_TEXT = """\
Beat This finds beats and downbeats in a song using the CPJKU "Beat This!" \
neural beat tracker, then turns them into markers for DaVinci Resolve.

Setup: the beat tracker needs PyTorch and a few other packages that aren't \
part of the rest of the suite. The Setup panel shows what's present; \
Install dependencies pip-installs whatever is missing into the same Python \
that's running this app, and Download model fetches the model checkpoint \
ahead of time so the first analysis isn't a long silent wait. Restart the \
app after installing. If PyTorch itself won't install, get the right build \
for your machine from https://pytorch.org/get-started/locally/ and press \
Re-check.

1. Browse to a song. Audio is read with ffmpeg (already required by the \
rest of PAZ), so anything ffmpeg can open works - mp3, wav, flac, m4a, \
ogg, even the audio track of a video file.
2. Press Analyze (or F5). There is nothing to configure first.

3. Once analysis finishes, the table lists every beat with its time and \
its position in the bar (1 = downbeat).

The model. There is no model to pick, on purpose. beat_this publishes \
about forty checkpoints, but most exist to reproduce tables in its paper \
rather than to track beats well - single_*, fold*, and the single_no* \
ablations are all trained on reduced data or deliberately missing a \
feature. Of what remains, final0/1/2 are the same model with three \
different random seeds (equivalent in expected quality, there is no best \
one) and small0/1/2 are a tenth the size and a little less accurate.

So this tab always runs all three final seeds over the same audio and \
averages their frame-by-frame probabilities before picking peaks, which \
cancels the noise particular to any one seed. That is one setting, not a \
choice between models - you never pick anything. Three times the work of a \
single model and 234 MB of checkpoints, for the most accurate result this \
tracker can give. Press "Download model" once in Setup to fetch all three \
ahead of time; otherwise the first analysis stops to download them.

If you ever do need something else - a slow machine where the small model \
is worth the accuracy, or a GPU to avoid - Settings > e621 & App > Beat \
This has the overrides. DBN postprocessing lives there too, and should \
stay off: the paper this tracker comes from is called "Accurate Beat \
Tracking Without DBN Postprocessing", so the DBN is there for comparison, \
not for quality.

Export:
- Save .beats writes the plain-text format beat_this and Sonic Visualiser \
already use: one "time<TAB>beat number" line per beat.
- Save EDL for Resolve writes a CMX3600 EDL of timeline markers, in the \
same form Resolve itself exports them: one one-frame event per beat, each \
carrying a |C: colour, |M: name and |D: duration. Import it with Timeline \
> Import > Timeline Markers from EDL.

Three things have to match or the import quietly produces nothing:
  · Frame rate - set it to your timeline's rate before exporting. Markers \
are written as non-drop-frame timecode.
  · Timeline starts at - EDL marker import is absolute, so this has to be \
your timeline's own start timecode. Resolve uses 01:00:00:00 for a new \
timeline, which is the default here; pick 00:00:00:00 only if you have \
changed yours. An EDL written from the wrong base puts every marker \
outside the timeline, where they don't land at all.
  · Song position - put the song at the very start of the timeline. EDL \
markers go by absolute timecode, not by where the clip happens to sit.

- Send to Resolve now skips the file entirely and adds markers straight to \
the timeline currently open in Resolve, via Resolve's scripting API. It \
needs Resolve open on this machine with Preferences > System > General > \
"External scripting using" set to Local (or Network) - that setting is \
Disabled out of the box and is the usual reason this fails while Resolve \
is plainly running. The scripting library is found automatically in the \
place the installer puts it, so the RESOLVE_SCRIPT_API / \
RESOLVE_SCRIPT_LIB / PYTHONPATH environment variables from Resolve's own \
Developer/Scripting README are honoured if you have set them but are not \
required. If it still can't connect, the log lists every path it tried.

Unlike the EDL, the live handoff doesn't need the clip at timeline zero or \
a matching frame rate - it reads the timeline's own rate and start frame.
"""
