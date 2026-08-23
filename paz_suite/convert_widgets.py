"""Convert-tab widgets: the render queue table, the scrub/inspector
preview, the contact sheet, and the duplicate finder.
"""

from __future__ import annotations

import io
import os
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

from .theme import T, font, px
from .format import fmt_clock, fmt_size
from .files import open_in_explorer
from .media import MediaInfo, ThumbCache, probe, dhash, hamming
from .widgets import PeekWindow, popup_menu
from .player_engine import ClipPlayer, HAS_FFPLAY
from . import uithread

STATE_COLOURS = {
    "queued":    T.FAINT,
    "probing":   T.FAINT,
    "running":   T.ACCENT,
    "done":      T.OK,
    "failed":    T.FAIL,
    "skipped":   T.FAINT,
    "cancelled": T.WARN,
}

STATE_LABELS = {
    "queued":    "Queued",
    "probing":   "Reading",
    "running":   "Encoding",
    "done":      "Done",
    "failed":    "Failed",
    "skipped":   "Skipped",
    "cancelled": "Stopped",
}


class QueueTable(ctk.CTkFrame):
    """
    The render queue. Built on ttk.Treeview so it stays fast with thousands
    of rows, sorts by any column, and supports keyboard navigation for free.
    A coloured bar on the left edge carries status, the way a render queue
    does.
    """

    COLUMNS = (
        ("name",   "File",        220, "w"),
        ("state",  "Status",       84, "w"),
        ("artist", "Artist",      110, "w"),
        ("rating", "R",            34, "w"),
        ("res",    "Resolution",   92, "w"),
        ("fps",    "FPS",          58, "e"),
        ("dur",    "Length",       70, "e"),
        ("size",   "Size",         74, "e"),
        ("dest",   "Destination", 140, "w"),
    )

    on_menu = None

    def __init__(self, parent, on_select=None, on_activate=None, **kw):
        kw.setdefault("border_color", T.ACCENT_DEEP)
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=12,
                          border_width=1, **kw)
        self.on_select = on_select
        self.on_activate = on_activate
        self._rows: dict = {}
        self._order: list = []
        self._visible: set = set()
        self._sort_key = None
        self._sort_reverse = False
        self._query = ""
        self._state_filter = "All"

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))
        bar.grid_columnconfigure(1, weight=1)

        self.search = ctk.CTkEntry(
            bar, placeholder_text="Filter name, artist or tags", width=200, height=28,
            font=font(11), corner_radius=6, fg_color=T.INPUT,
            border_color=T.LINE, border_width=1, text_color=T.TEXT)
        self.search.grid(row=0, column=0, sticky="w")
        self.search.bind("<KeyRelease>", self._on_search)

        self.count = ctk.CTkLabel(bar, text="", font=font(10, mono=True),
                                   text_color=T.FAINT)
        self.count.grid(row=0, column=1, sticky="w", padx=12)

        self.state_filter = ctk.CTkSegmentedButton(
            bar, values=["All", "Done", "Failed", "Queued"],
            command=self._on_state_filter, font=font(10), height=28,
            corner_radius=6, fg_color=T.INPUT, selected_color=T.ACCENT_DEEP,
            selected_hover_color=T.ACCENT_DEEP, unselected_color=T.INPUT,
            unselected_hover_color=T.BTN_HOV, text_color=T.DIM, border_width=1)
        self.state_filter.grid(row=0, column=2, sticky="e")
        self.state_filter.set("All")

        self._build_style()
        self._build_bars()

        self.tree = ttk.Treeview(
            self, style="Q.Treeview", columns=[c[0] for c in self.COLUMNS],
            show="tree headings", selectmode="browse")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))

        self._configure_tags()
        self.tree.column("#0", width=26, minwidth=26, stretch=False, anchor="center")
        self.tree.heading("#0", text="")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.column(key, width=width, minwidth=48, anchor=anchor,
                              stretch=(key == "name"))
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))

        scroll = ctk.CTkScrollbar(self, command=self.tree.yview, width=12,
                                   button_color=T.LINE, button_hover_color=T.FAINT)
        scroll.grid(row=1, column=1, sticky="ns", padx=(2, 6), pady=(0, 10))
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_activate)
        self.tree.bind("<Return>", self._on_activate)

        self.on_peek = None
        self.on_peek_hide = None
        self.tree.bind("<Motion>", self._on_motion)
        for hide_on in ("<Leave>", "<MouseWheel>", "<Button-4>", "<Button-5>",
                        "<Button-1>", "<Button-3>", "<KeyPress>"):
            self.tree.bind(hide_on, self._peek_hide, add="+")

        self.menu = popup_menu(self)
        self.tree.bind("<Button-3>", self._on_right_click)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Q.Treeview", background=T.ROW, fieldbackground=T.ROW,
                         foreground=T.TEXT, rowheight=28, borderwidth=0,
                         font=(T.UI, 10))
        style.configure("Q.Treeview.Heading", background=T.ELEVATED,
                         foreground=T.FAINT, relief="flat", borderwidth=0,
                         font=(T.UI, 9, "bold"), padding=(8, 7))
        style.map("Q.Treeview.Heading", background=[("active", T.BTN_HOV)])
        style.map("Q.Treeview",
                  background=[("selected", T.ROW_SEL)],
                  foreground=[("selected", T.TEXT)])
        self._tag_spec = {
            "queued": T.DIM, "probing": T.DIM, "running": T.TEXT,
            "done": T.TEXT, "failed": T.FAIL, "skipped": T.FAINT,
            "cancelled": T.WARN,
        }
        self._tag_back = {
            "running": (T.ACCENT_DEEP, T.ACCENT_DEEP),
            "failed": (T.FAIL_DEEP, T.FAIL_DEEP),
        }

    def _configure_tags(self) -> None:
        for state, colour in self._tag_spec.items():
            even, odd = self._tag_back.get(state, (T.ROW, T.ROW_ALT))
            self.tree.tag_configure(f"{state}0", background=even, foreground=colour)
            self.tree.tag_configure(f"{state}1", background=odd, foreground=colour)

    def _build_bars(self) -> None:
        self._bars = {}
        for state, colour in STATE_COLOURS.items():
            image = Image.new("RGBA", (4, 18), colour)
            self._bars[state] = ImageTk.PhotoImage(image)

    def clear(self) -> None:
        for iid in self._order:
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self._rows.clear()
        self._order.clear()
        self._visible.clear()
        self._update_count()

    def add(self, iid: str, name: str, state: str = "queued", **cells) -> None:
        self._rows[iid] = {"name": name, "state": state,
                            "_parity": len(self._order) % 2, **cells}
        self._order.append(iid)
        self._visible.add(iid)
        self.tree.insert("", "end", iid=iid, text="",
                          image=self._bars.get(state, self._bars["queued"]),
                          values=self._values(name, state, self._rows[iid]),
                          tags=self._tags(iid, state))
        self._apply_visibility(iid)
        self._update_count()

    def set_row(self, iid: str, **cells) -> None:
        row = self._rows.get(iid)
        if row is None:
            return
        row.update(cells)
        state = row.get("state", "queued")
        if not self.tree.exists(iid):
            return
        self.tree.item(iid, values=self._values(row["name"], state, row),
                        image=self._bars.get(state, self._bars["queued"]),
                        tags=self._tags(iid, state))
        self._apply_visibility(iid)
        self._update_count()

    def _values(self, name: str, state: str, cells: dict) -> tuple:
        return (
            name,
            STATE_LABELS.get(state, state),
            cells.get("artist", "--"),
            cells.get("rating", "--"),
            cells.get("res", "--"),
            cells.get("fps", "--"),
            cells.get("dur", "--"),
            cells.get("size", "--"),
            cells.get("dest", "--"),
        )

    def _tags(self, iid: str, state: str) -> tuple:
        return (f"{state}{self._rows.get(iid, {}).get('_parity', 0)}",)

    def row(self, iid: str) -> dict:
        return self._rows.get(iid, {})

    @property
    def selected(self) -> str | None:
        picks = self.tree.selection()
        return picks[0] if picks else None

    def select(self, iid: str) -> None:
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def iter_rows(self):
        for iid in self._order:
            yield iid, self._rows[iid]

    def _matches(self, iid: str) -> bool:
        row = self._rows.get(iid, {})
        if self._query:
            haystack = (row.get("name", "").lower() + " " +
                        str(row.get("artist", "")).lower() + " " +
                        str(row.get("_tags", "")))
            if self._query not in haystack:
                return False
        wanted = self._state_filter
        if wanted == "Done":
            return row.get("state") == "done"
        if wanted == "Failed":
            return row.get("state") in ("failed", "cancelled")
        if wanted == "Queued":
            return row.get("state") in ("queued", "probing", "running")
        return True

    def _apply_visibility(self, iid: str) -> None:
        wanted = self._matches(iid)
        shown = iid in self._visible
        if wanted and not shown:
            self._reattach(iid)
            self._visible.add(iid)
        elif shown and not wanted:
            self.tree.detach(iid)
            self._visible.discard(iid)

    def _reattach(self, iid: str) -> None:
        index = 0
        for other in self._order:
            if other == iid:
                break
            if other in self._visible:
                index += 1
        self.tree.move(iid, "", index)

    def refresh_filter(self) -> None:
        for iid in self._order:
            self._apply_visibility(iid)
        self._update_count()

    def empty_hint(self, text: str) -> None:
        self.count.configure(text=text)

    def _on_search(self, _event=None) -> None:
        self._query = self.search.get().strip().lower()
        self.refresh_filter()

    def _on_state_filter(self, value: str) -> None:
        self._state_filter = value
        self.refresh_filter()

    def sort_by(self, key: str) -> None:
        if self._sort_key == key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key = key
            self._sort_reverse = False

        numeric = key in ("res", "fps", "dur", "size")

        def sort_value(iid):
            row = self._rows[iid]
            if numeric:
                return row.get(f"_{key}", 0) or 0
            if key == "state":
                return STATE_LABELS.get(row.get("state", ""), "")
            return str(row.get(key if key != "name" else "name", "")).lower()

        self._order.sort(key=sort_value, reverse=self._sort_reverse)
        position = 0
        for iid in self._order:
            self._rows[iid]["_parity"] = position % 2
            if iid in self._visible:
                self.tree.move(iid, "", position)
                position += 1
            self.tree.item(iid, tags=self._tags(iid, self._rows[iid]["state"]))
        for column, title, _w, _a in self.COLUMNS:
            arrow = ""
            if column == key:
                arrow = "  ↓" if self._sort_reverse else "  ↑"
            self.tree.heading(column, text=title + arrow)

    def _update_count(self) -> None:
        shown = len(self.tree.get_children())
        total = len(self._order)
        self.count.configure(
            text=f"{shown} of {total}" if shown != total else f"{total} files")

    def _on_motion(self, event) -> None:
        if not self.on_peek:
            return
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            self._peek_hide()
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            self._peek_hide()
            return
        width = max(self.tree.winfo_width() - 30, 1)
        frac = max(0.0, min((event.x - 30) / width, 1.0))
        self.on_peek(iid, frac, event.x_root, event.y_root)

    def _peek_hide(self, _event=None) -> None:
        if self.on_peek_hide:
            self.on_peek_hide()

    def _on_select(self, _event=None) -> None:
        if self.on_select and self.selected:
            self.on_select(self.selected)

    def _on_activate(self, _event=None) -> None:
        if self.on_activate and self.selected:
            self.on_activate(self.selected)

    def _on_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        if self.menu.index("end") is not None:
            self.menu.delete(0, "end")
        if self.on_menu:
            self.on_menu(iid, self.menu)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


class ScrubPreview(ctk.CTkFrame):
    """
    Viewer with a clickable timeline and filmstrip.

    Click or drag the timeline to jump to any point in the file. The
    filmstrip below fills in as frames decode. When a converted output
    exists, the Source / Output switch holds the timecode so the same frame
    can be compared before and after encoding.
    """

    STRIP_COUNT_MIN = 6

    def __init__(self, parent, cache: ThumbCache, cfg, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=12,
                          border_width=1, border_color=T.LINE, **kw)
        self.cache = cache
        self.cfg = cfg
        self.on_grid = None            # wired by the tab: opens the contact sheet
        self.peek = PeekWindow(self)
        self._ghost_after = None
        self._ghost_token = 0
        self._ghost_busy = False
        self._ghost_pending = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._paths: dict = {}
        self._active = "Source"
        self._info: MediaInfo | None = None
        self._pos = 0.0
        self._token = 0
        self._strip_token = 0
        self._pending = None
        self._image_ref = None
        self._strip_refs: list = []
        self._strip_marks: list = []
        self._dragging = False

        self.view = tk.Canvas(self, bg=T.INPUT, highlightthickness=0, borderwidth=0,
                               height=340)
        self.view.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 6))
        self.view.bind("<Configure>", self._on_view_resize)
        self.view.bind("<Button-1>", lambda e: self.toggle_play())

        # Real playback (with audio) shares the same view canvas as the
        # static scrub frames - only one is ever active at a time.
        self.player_engine = ClipPlayer(
            self.view, 480, 270,
            on_tick=self._on_play_tick, on_state=self._on_play_state,
            on_fail=self._on_play_fail)
        self.player_engine.volume = max(0, min(int(cfg.player_volume), 100))
        self.player_engine.muted = bool(cfg.player_muted) or not HAS_FFPLAY
        self._volume_job = None

        self.timeline = tk.Canvas(self, bg=T.SURFACE, highlightthickness=0,
                                   borderwidth=0, height=26, cursor="hand2")
        self.timeline.grid(row=1, column=0, sticky="ew", padx=14)
        self.timeline.bind("<Configure>", lambda e: self._draw_timeline())
        self.timeline.bind("<Button-1>", self._on_press)
        self.timeline.bind("<B1-Motion>", self._on_drag)
        self.timeline.bind("<ButtonRelease-1>", self._on_release)
        self.timeline.bind("<Motion>", self._on_hover)
        self.timeline.bind("<Leave>", self._on_timeline_leave)

        self.strip = tk.Canvas(self, bg=T.SURFACE, highlightthickness=0,
                                borderwidth=0, height=48, cursor="hand2")
        self.strip.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 0))
        self.strip.bind("<Button-1>", self._on_strip_click)
        self.strip.bind("<Configure>", self._on_strip_resize)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=14, pady=(6, 10))
        footer.grid_columnconfigure(2, weight=1)

        transport = ctk.CTkFrame(footer, fg_color="transparent")
        transport.grid(row=0, column=0, sticky="w", rowspan=2, padx=(0, 12))

        def tbtn(text, command, width=34):
            return ctk.CTkButton(
                transport, text=text, width=width, height=26, corner_radius=6,
                font=font(12), fg_color=T.BTN, hover_color=T.BTN_HOV,
                text_color=T.DIM, command=command)

        self.play_btn = ctk.CTkButton(
            transport, text="▶ Play", width=68, height=26, corner_radius=6,
            font=font(12), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.ACCENT, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=(0, 4))
        tbtn("◀", lambda: self.step(-1)).pack(side="left", padx=(0, 4))
        tbtn("▶", lambda: self.step(1)).pack(side="left", padx=(0, 4))
        tbtn("Grid", lambda: self.on_grid() if self.on_grid else None,
             width=48).pack(side="left", padx=(0, 4))

        volume_box = ctk.CTkFrame(transport, fg_color="transparent")
        volume_box.pack(side="left", padx=(8, 0))
        self.mute_btn = ctk.CTkButton(
            volume_box, text="🔇" if self.player_engine.muted else "🔊",
            width=28, height=26, corner_radius=6, font=font(11),
            fg_color="transparent", hover_color=T.BTN_HOV,
            text_color=T.FAINT if self.player_engine.muted else T.TEXT,
            state="normal" if HAS_FFPLAY else "disabled",
            command=self._toggle_mute)
        self.mute_btn.pack(side="left")
        self.volume_slider = ctk.CTkSlider(
            volume_box, from_=0, to=100, number_of_steps=100, width=64,
            height=14, button_color=T.ACCENT, button_hover_color=T.ACCENT_HOV,
            progress_color=T.ACCENT, fg_color=T.LINE,
            command=self._on_volume_drag)
        self.volume_slider.set(0 if self.player_engine.muted else self.player_engine.volume)
        self.volume_slider.pack(side="left", padx=(6, 0))
        if not HAS_FFPLAY:
            self.volume_slider.configure(state="disabled")

        self.meta = ctk.CTkLabel(footer, text="", font=font(10, mono=True),
                                  text_color=T.DIM, anchor="w")
        self.meta.grid(row=0, column=2, sticky="w")

        self.note = ctk.CTkLabel(footer, text="", font=font(10), text_color=T.FAIL,
                                  anchor="w", justify="left", wraplength=380)
        self.note.grid(row=1, column=2, columnspan=2, sticky="w", pady=(3, 0))

        self.switch = ctk.CTkSegmentedButton(
            footer, values=["Source", "Output"], command=self._on_switch,
            font=font(10), height=24, corner_radius=6,
            fg_color=T.INPUT, selected_color=T.ACCENT_DEEP,
            selected_hover_color=T.ACCENT_DEEP, unselected_color=T.INPUT,
            unselected_hover_color=T.BTN_HOV, text_color=T.DIM,
            border_width=1,
        )
        self.switch.grid(row=0, column=3, sticky="e")
        self.switch.set("Source")

        self.clear("Select a file to inspect it")

    def clear(self, message: str = "Nothing loaded") -> None:
        self.player_engine.clear()
        self._ghost_hide()
        self._token += 1
        self._strip_token += 1
        self._paths = {}
        self._info = None
        self._pos = 0.0
        self._strip_marks = []
        self._strip_refs = []
        self._image_ref = None
        self._placeholder = message
        self._redraw_placeholder()
        self.strip.delete("all")
        self._draw_timeline()
        self.meta.configure(text="")
        self.note.configure(text="")

    def load(self, source: str | None, output: str | None = None,
             prefer: str = "Source", position: float | None = None) -> None:
        """
        Point the viewer at a file, and optionally its converted result.
        Probing happens off the UI thread so slow or networked drives never
        freeze the window.
        """
        paths = {}
        if source and os.path.exists(source):
            paths["Source"] = source
        if output and os.path.exists(output):
            paths["Output"] = output
        if not paths:
            self.clear("That file is no longer on disk")
            return

        self.player_engine.stop()
        self._ghost_hide()
        self._paths = paths
        self._active = prefer if prefer in paths else next(iter(paths))
        self.switch.set(self._active)
        self._info = None
        self._image_ref = None
        self._strip_token += 1
        self._strip_marks = []
        self._strip_refs = []
        self.strip.delete("all")
        self.meta.configure(text="")
        self.note.configure(text="")
        self._show_message("Reading file")
        self._draw_timeline()
        self._probe_async(position)

    def set_note(self, text: str, colour: str = T.FAIL) -> None:
        self.note.configure(text=text or "", text_color=colour)

    def _probe_async(self, position: float | None) -> None:
        path = self._current_path()
        if not path:
            return
        self._token += 1
        token = self._token

        def work():
            info = probe(path)
            uithread.post(self._on_info, info, token, position)

        threading.Thread(target=work, daemon=True).start()

    def _on_info(self, info, token: int, position: float | None) -> None:
        if token != self._token:
            return
        self._info = info
        duration = info.duration if info else 0.0
        if position is not None:
            self._pos = max(0.0, min(position, max(duration - 0.05, 0.0)))
        else:
            self._pos = min(duration * 0.25, 3.0) if duration else 0.0
        path = self._current_path()
        if path:
            self.player_engine.load(path, duration, info.fps if info else 30.0)
            self.player_engine.position = self._pos
            # Warm the hover-scrub sheet now rather than on first hover -
            # by the time anyone drags the timeline it's usually ready.
            self.cache.prime_hover(path, duration)
        self._update_meta()
        self._draw_timeline()
        self._request_frame(immediate=True)
        self._build_strip()

    def step(self, delta: float) -> None:
        if not self._paths:
            return
        self.player_engine.pause()
        duration = self._info.duration if self._info else 0.0
        limit = max(duration - 0.05, 0.0) if duration else self._pos + abs(delta)
        self._pos = max(0.0, min(self._pos + delta, limit))
        self._draw_timeline()
        self._request_frame()

    @property
    def position(self) -> float:
        return self._pos

    @property
    def active_path(self) -> str | None:
        return self._current_path()

    @property
    def clip_duration(self) -> float:
        return self._duration()

    def seek(self, seconds: float) -> None:
        """Jump straight to a timecode (used by the contact sheet)."""
        if not self._paths:
            return
        self.player_engine.pause()
        duration = self._duration()
        limit = max(duration - 0.05, 0.0) if duration else seconds
        self._pos = max(0.0, min(seconds, limit))
        self._draw_timeline()
        self._request_frame(immediate=True)

    def _current_path(self) -> str | None:
        return self._paths.get(self._active)

    def _on_switch(self, value: str) -> None:
        if value not in self._paths:
            self.switch.set(self._active)
            return
        self.player_engine.stop()
        keep = self._pos
        self._active = value
        self._probe_async(keep)

    # ── real playback (shares the view canvas with the static scrubber) ────

    def toggle_play(self) -> None:
        if not self._current_path() or not self._duration():
            return
        self._ghost_hide()
        self._sync_player_size(self.view.winfo_width(), self.view.winfo_height())
        if not self.player_engine.playing:
            self.player_engine.position = self._pos
        self.player_engine.toggle()

    def _toggle_mute(self) -> None:
        muted = self.player_engine.toggle_mute()
        self.mute_btn.configure(text="🔇" if muted else "🔊",
                                text_color=T.FAINT if muted else T.TEXT)
        self.volume_slider.set(0 if muted else self.player_engine.volume)
        self.cfg.player_muted = muted
        self.cfg.save()

    def _on_volume_drag(self, value) -> None:
        volume = max(0, min(int(round(float(value))), 100))
        if self.player_engine.muted and volume > 0:
            self.mute_btn.configure(text="🔊", text_color=T.TEXT)
        if self._volume_job is not None:
            try:
                self.after_cancel(self._volume_job)
            except ValueError:
                pass
        self._volume_job = self.after(220, lambda: self._commit_volume(volume))

    def _commit_volume(self, volume: int) -> None:
        self._volume_job = None
        self.player_engine.set_volume(volume)
        self.cfg.player_volume = self.player_engine.volume
        self.cfg.player_muted = self.player_engine.muted
        self.cfg.save()

    def _on_play_state(self, playing: bool) -> None:
        self.play_btn.configure(
            text="⏸ Pause" if playing else "▶ Play",
            fg_color=T.ACCENT_DEEP if playing else T.BTN,
            text_color=T.ACCENT)

    def _on_play_tick(self, position: float) -> None:
        self._pos = position
        self._draw_timeline()

    def _on_play_fail(self, message: str) -> None:
        self.set_note(message)

    def _update_meta(self) -> None:
        if not self._info:
            self.meta.configure(text="")
            return
        i = self._info
        bits = [i.resolution, f"{i.fps_text} fps" + (" (VFR)" if i.vfr else ""),
                fmt_clock(i.duration), fmt_size(i.size), i.vcodec or "?"]
        bits.append("audio" if i.has_audio else "silent")
        self.meta.configure(text="  ·  ".join(bits))

    def _on_view_resize(self, event) -> None:
        self._sync_player_size(event.width, event.height)
        if self.player_engine.playing:
            return
        if self._image_ref is not None:
            self.view.delete("all")
            self.view.create_image(event.width // 2, event.height // 2,
                                    image=self._image_ref, anchor="center")
        else:
            self._redraw_placeholder()

    def _sync_player_size(self, width: int, height: int) -> None:
        """Keep the playback engine's decode size roughly matched to the
        canvas. Skipped while actively playing to avoid restarting the
        decoder on every pixel of a window drag; takes effect on the next
        Play press instead."""
        if self.player_engine.playing:
            return
        width = max(int(width) - 4, 100) // 2 * 2
        height = max(int(height) - 4, 100)
        self.player_engine.view_w = width
        self.player_engine.view_h = height
        self.player_engine.frame_bytes = width * height * 3

    def _redraw_placeholder(self) -> None:
        if self._image_ref is not None:
            return
        self.view.delete("all")
        w = max(self.view.winfo_width(), 40)
        h = max(self.view.winfo_height(), 40)
        self.view.create_text(w // 2, h // 2, text=getattr(self, "_placeholder", ""),
                               fill=T.FAINT, font=(T.UI, 11), width=w - 40)

    def _show_message(self, text: str) -> None:
        self._image_ref = None
        self._placeholder = text
        self._redraw_placeholder()

    def _request_frame(self, immediate: bool = False) -> None:
        if self._pending is not None:
            try:
                self.after_cancel(self._pending)
            except ValueError:
                pass
            self._pending = None
        if immediate:
            # The settled/final frame (a release, or the initial load) -
            # worth the extra fallback attempts to make sure it lands.
            self._fetch_frame(fast=False)
        else:
            self._pending = self.after(90, lambda: self._fetch_frame(fast=True))

    def _fetch_frame(self, fast: bool = False) -> None:
        self._pending = None
        path = self._current_path()
        if not path:
            return
        self._token += 1
        token = self._token
        pos = self._pos
        width = max(self.view.winfo_width(), 320)

        def work():
            # fast=True while actively dragging: one quick attempt, and if
            # it misses this tick just shows nothing rather than blocking
            # on the slower fallback attempts - the next drag tick will
            # try again at wherever the mouse is by then anyway.
            data = self.cache.frame(path, pos, width, fast=fast)
            if token != self._token:
                return
            uithread.post(self._draw_frame, data, token)

        threading.Thread(target=work, daemon=True).start()

    def _draw_frame(self, data: bytes | None, token: int) -> None:
        if token != self._token:
            return
        if not data:
            self._show_message("No frame at this point")
            return
        try:
            image = Image.open(io.BytesIO(data))
            box_w = max(self.view.winfo_width() - 4, 100)
            box_h = max(self.view.winfo_height() - 4, 100)
            image.thumbnail((box_w, box_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            self._show_message("Frame could not be decoded")
            return
        self._image_ref = photo
        self.view.delete("all")
        self.view.create_image(self.view.winfo_width() // 2,
                                self.view.winfo_height() // 2,
                                image=photo, anchor="center")

    def _duration(self) -> float:
        return self._info.duration if (self._info and self._info.duration) else 0.0

    def _draw_timeline(self, hover_x: int | None = None) -> None:
        c = self.timeline
        c.delete("all")
        w = c.winfo_width()
        if w < 20:
            return
        y = 15
        pad = 2
        duration = self._duration()

        c.create_line(pad, y, w - pad, y, fill=T.LINE, width=4, capstyle="round")

        if duration <= 0:
            c.create_text(w // 2, 5, text="no timeline", fill=T.FAINT,
                           font=(T.MONO, 8), anchor="n")
            return

        frac = self._pos / duration
        px = pad + frac * (w - pad * 2)
        c.create_line(pad, y, px, y, fill=T.ACCENT, width=4, capstyle="round")

        for mark in self._strip_marks:
            mx = pad + (mark / duration) * (w - pad * 2)
            c.create_line(mx, y - 6, mx, y - 3, fill=T.LINE, width=1)

        c.create_line(px, y - 8, px, y + 8, fill=T.ACCENT_HOV, width=2)
        c.create_oval(px - 4, y - 4, px + 4, y + 4, fill=T.ACCENT_HOV, outline="")

        c.create_text(pad, 3, text=fmt_clock(self._pos), fill=T.TEXT,
                       font=(T.MONO, 9), anchor="nw")
        c.create_text(w - pad, 3, text=fmt_clock(duration), fill=T.FAINT,
                       font=(T.MONO, 9), anchor="ne")

        if hover_x is not None:
            hover_t = self._time_at(hover_x)
            c.create_line(hover_x, y - 8, hover_x, y + 8, fill=T.FAINT, width=1)
            anchor = "n" if pad + 30 < hover_x < w - 30 else ("nw" if hover_x <= pad + 30 else "ne")
            c.create_text(hover_x, 3, text=fmt_clock(hover_t), fill=T.DIM,
                           font=(T.MONO, 9), anchor=anchor)

    def _time_at(self, x: int) -> float:
        w = max(self.timeline.winfo_width() - 4, 1)
        frac = max(0.0, min((x - 2) / w, 1.0))
        return frac * self._duration()

    def _on_press(self, event):
        if not self._duration():
            return
        self._ghost_hide()
        self.player_engine.pause()
        self._dragging = True
        self._pos = self._time_at(event.x)
        self._draw_timeline()
        self._request_frame()

    def _on_drag(self, event):
        if not self._dragging:
            return
        self._pos = self._time_at(event.x)
        self._draw_timeline()
        self._request_frame()

    def _on_release(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._request_frame(immediate=True)

    def _on_hover(self, event):
        if self._duration() and not self._dragging:
            self._draw_timeline(hover_x=event.x)
            self._ghost_schedule(event)

    def _on_timeline_leave(self, _event=None):
        self._ghost_hide()
        self._draw_timeline()

    def _ghost_schedule(self, event) -> None:
        if self._ghost_after is not None:
            try:
                self.after_cancel(self._ghost_after)
            except ValueError:
                pass
        self._ghost_after = self.after(
            110, lambda: self._ghost_fetch(event.x, event.x_root, event.y_root))

    def _ghost_fetch(self, x: int, x_root: int, y_root: int) -> None:
        self._ghost_after = None
        path = self._current_path()
        duration = self._duration()
        if not path or self._dragging or not duration:
            return
        moment = self._time_at(x)
        self._ghost_token += 1
        token = self._ghost_token
        request = (path, duration, moment, token, x_root, y_root)
        if self._ghost_busy:
            # One extraction in flight at a time - only the latest hover
            # position matters, so it replaces whatever was pending
            # instead of piling up overlapping ffmpeg calls.
            self._ghost_pending = request
            return
        self._ghost_busy = True
        self._ghost_run(request)

    def _ghost_run(self, request) -> None:
        path, duration, moment, token, x_root, y_root = request
        frac = moment / duration

        def work():
            # A pre-built sprite sheet crop instead of an ffmpeg spawn per
            # hover - see ThumbCache.hover_frame() in media.py.
            data = self.cache.hover_frame(path, duration, frac)
            uithread.post(self._ghost_done, data, moment, token, x_root, y_root)

        threading.Thread(target=work, daemon=True).start()

    def _ghost_done(self, data, moment, token, x_root, y_root) -> None:
        self._ghost_busy = False
        if token == self._ghost_token:
            self._ghost_show(data, moment, token, x_root, y_root)
        pending = self._ghost_pending
        self._ghost_pending = None
        if pending is not None:
            self._ghost_busy = True
            self._ghost_run(pending)

    def _ghost_show(self, data, moment, token, x_root, y_root) -> None:
        if token != self._ghost_token or self._dragging:
            return
        duration = self._duration()
        fraction = (moment / duration) if duration else None
        self.peek.show_frame(data, os.path.basename(self._current_path() or ""),
                              fmt_clock(moment), x_root, y_root, fraction=fraction)

    def _ghost_hide(self) -> None:
        self._ghost_token += 1
        self._ghost_pending = None
        if self._ghost_after is not None:
            try:
                self.after_cancel(self._ghost_after)
            except ValueError:
                pass
            self._ghost_after = None
        self.peek.hide()

    def _on_strip_resize(self, event) -> None:
        if not self._strip_marks:
            return
        if abs(event.width - getattr(self, "_strip_width", 0)) < 10:
            return
        self._strip_width = event.width
        self._build_strip()

    def _build_strip(self) -> None:
        self._strip_width = self.strip.winfo_width()
        self._strip_token += 1
        token = self._strip_token
        self.strip.delete("all")
        self._strip_refs = []
        self._strip_marks = []

        path = self._current_path()
        duration = self._duration()
        if not path or duration <= 0:
            return

        count = max(self.STRIP_COUNT_MIN, min(int(self.master_count()), 16))
        marks = [duration * (i + 0.5) / count for i in range(count)]
        self._strip_marks = marks
        self._draw_timeline()
        self._layout_strip()

        def work():
            for index, mark in enumerate(marks):
                if token != self._strip_token:
                    return
                data = self.cache.frame(path, mark, 200)
                if token != self._strip_token:
                    return
                uithread.post(self._place_strip, data, index, token)

        threading.Thread(target=work, daemon=True).start()

    def master_count(self) -> int:
        try:
            return int(self.winfo_toplevel().filmstrip_frames)
        except Exception:
            return 10

    def _cell_geometry(self):
        count = len(self._strip_marks)
        if not count:
            return 0, 0, 0
        gap = 3
        total = max(self.strip.winfo_width(), 200)
        cell_w = int((total - gap * (count - 1)) / count)
        cell_h = min(int(cell_w * 9 / 16), 44)
        return cell_w, cell_h, gap

    def _layout_strip(self) -> None:
        if not self._strip_marks or self._strip_refs:
            return
        cell_w, cell_h, gap = self._cell_geometry()
        if cell_w <= 0:
            return
        self.strip.delete("slot")
        for i in range(len(self._strip_marks)):
            x = i * (cell_w + gap)
            self.strip.create_rectangle(x, 0, x + cell_w, cell_h,
                                         fill=T.INPUT, outline="", tags="slot")

    def _place_strip(self, data: bytes | None, index: int, token: int) -> None:
        if token != self._strip_token or not data:
            return
        cell_w, cell_h, gap = self._cell_geometry()
        if cell_w <= 0:
            return
        try:
            image = Image.open(io.BytesIO(data))
            image = image.resize((cell_w, cell_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return
        self._strip_refs.append(photo)
        x = index * (cell_w + gap)
        self.strip.create_image(x, 0, image=photo, anchor="nw", tags=f"cell{index}")

    def _on_strip_click(self, event):
        if not self._strip_marks:
            return
        self.player_engine.pause()
        cell_w, _, gap = self._cell_geometry()
        if cell_w <= 0:
            return
        index = int(event.x // (cell_w + gap))
        if 0 <= index < len(self._strip_marks):
            self._pos = self._strip_marks[index]
            self._draw_timeline()
            self._request_frame(immediate=True)


class ContactSheet(ctk.CTkToplevel):
    """
    One clip laid out as a grid of frames - the whole story at a glance.
    Click any cell to jump the inspector to that moment.

    The grid used to be a fixed 4x3. Twelve frames of a four-minute clip
    is one every twenty seconds, which tells you almost nothing, and on a
    4K screen it left most of the desktop empty. It now fills the space
    available: as many cells as fit at a readable size, so a big screen
    gets a genuinely useful sheet and a small one still gets a sensible
    twelve.
    """

    CELL_W = 300           # unscaled; the real width is worked out below
    MIN_COLS, MAX_COLS = 4, 8
    MIN_ROWS, MAX_ROWS = 3, 6

    def __init__(self, parent, cache: ThumbCache, path: str, title: str,
                 on_jump=None):
        super().__init__(parent)
        self.cache = cache
        self.path = path
        self.on_jump = on_jump
        self.alive = True
        self._refs: list = []
        self._marks: list = []
        self._token = 0

        cap = px(16)
        gap = px(6)
        self.CELL_W = px(self.CELL_W)
        self.cell_h = int(self.CELL_W * 9 / 16)
        try:
            room_w = int(parent.winfo_screenwidth() * 0.86)
            room_h = int(parent.winfo_screenheight() * 0.84)
        except tk.TclError:
            room_w, room_h = 1500, 900
        self.COLS = max(self.MIN_COLS,
                        min((room_w - gap) // (self.CELL_W + gap), self.MAX_COLS))
        self.ROWS = max(self.MIN_ROWS,
                        min((room_h - gap - px(90)) // (self.cell_h + cap + gap),
                            self.MAX_ROWS))

        cw = self.COLS * self.CELL_W + (self.COLS + 1) * gap
        chh = self.ROWS * (self.cell_h + cap) + (self.ROWS + 1) * gap
        self.gap, self.cap = gap, cap

        self.title(f"Contact sheet - {title}")
        self.geometry(f"{cw + 24}x{chh + 66}")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(head, text=title, font=font(12, "bold"),
                     text_color=T.TEXT, anchor="w").pack(side="left")
        ctk.CTkLabel(head,
                     text=f"{self.COLS * self.ROWS} frames · click one to jump "
                          "the inspector · Esc closes",
                     font=font(10), text_color=T.FAINT).pack(side="right")

        self.canvas = tk.Canvas(self, bg=T.SURFACE, highlightthickness=0,
                                 bd=0, width=cw, height=chh, cursor="hand2")
        self.canvas.pack(padx=12, pady=(0, 12))
        self.canvas.bind("<Button-1>", self._on_click)

        threading.Thread(target=self._prepare, daemon=True).start()

    def _close(self):
        self.alive = False
        self._token += 1
        self.destroy()

    def _post(self, fn) -> bool:
        if not self.alive:
            return False
        uithread.post(fn)
        return True

    def _cell_xy(self, index: int) -> tuple:
        col, row = index % self.COLS, index // self.COLS
        x = self.gap + col * (self.CELL_W + self.gap)
        y = self.gap + row * (self.cell_h + self.cap + self.gap)
        return x, y

    def _prepare(self):
        info = probe(self.path)
        duration = info.duration if info else 0.0
        if not self.alive:
            return
        if duration <= 0:
            self._post(lambda: self.canvas.create_text(
                40, 30, text="This file has no readable timeline.",
                fill=T.FAINT, font=(T.UI, 11), anchor="w"))
            return
        count = self.COLS * self.ROWS
        self._marks = [duration * (i + 0.5) / count for i in range(count)]
        self._token += 1
        token = self._token

        def slots():
            for i in range(count):
                x, y = self._cell_xy(i)
                self.canvas.create_rectangle(
                    x, y, x + self.CELL_W, y + self.cell_h,
                    fill=T.INPUT, outline="")
                self.canvas.create_text(
                    x + self.CELL_W // 2, y + self.cell_h + self.cap // 2,
                    text=fmt_clock(self._marks[i]), fill=T.FAINT,
                    font=(T.MONO, 9))
        if not self._post(slots):
            return

        for index, mark in enumerate(self._marks):
            if not self.alive or token != self._token:
                return
            data = self.cache.frame(self.path, mark, self.CELL_W)
            if not self.alive or token != self._token:
                return
            if not self._post(lambda d=data, i=index, t=token: self._place(d, i, t)):
                return

    def _place(self, data: bytes | None, index: int, token: int):
        if not self.alive or token != self._token or not data:
            return
        try:
            image = Image.open(io.BytesIO(data))
            image = image.resize((self.CELL_W, self.cell_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return
        self._refs.append(photo)
        x, y = self._cell_xy(index)
        self.canvas.create_image(x, y, image=photo, anchor="nw")

    def _on_click(self, event):
        if not self._marks or not self.on_jump:
            return
        for index in range(len(self._marks)):
            x, y = self._cell_xy(index)
            if x <= event.x <= x + self.CELL_W and y <= event.y <= y + self.cell_h:
                self.on_jump(self._marks[index])
                return


class DuplicateWindow(ctk.CTkToplevel):
    """
    Scans the converted library for clips that are the same footage under
    different names: same length within 0.2s and a near-identical
    mid-frame. Deletion only ever touches converted copies, never sources.
    """

    def __init__(self, parent, tab):
        super().__init__(parent)
        self.tab = tab
        self.alive = True
        self.title("Duplicate finder")
        self.geometry("760x600")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.status = ctk.CTkLabel(self, text="Reading the library",
                                    font=font(11, mono=True), text_color=T.DIM,
                                    anchor="w")
        self.status.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))

        self.body = ctk.CTkScrollableFrame(
            self, fg_color=T.SURFACE, corner_radius=12,
            scrollbar_button_color=T.LINE, scrollbar_button_hover_color=T.FAINT)
        self.body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.body.grid_columnconfigure(0, weight=1)

        threading.Thread(target=self._scan, daemon=True).start()

    def _close(self):
        self.alive = False
        self.destroy()

    def _scan(self):
        cfg = self.tab.cfg
        files = []
        roots = [cfg.output_root, cfg.premium_root, cfg.upscale_root]
        for root_dir in roots:
            for folder in cfg.subfolders:
                directory = os.path.join(root_dir, folder)
                if not os.path.isdir(directory):
                    continue
                for name in os.listdir(directory):
                    if name.lower().endswith(".mp4"):
                        files.append(os.path.join(directory, name))
        if not self.alive:
            return
        self._say(f"Probing {len(files)} clips")

        buckets: dict = {}
        workers = min(8, max(2, os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(probe, path): path for path in files}
            for index, future in enumerate(as_completed(futures)):
                if not self.alive:
                    return
                path = futures[future]
                info = future.result()
                if not info or info.duration <= 0:
                    continue
                key = int(info.duration * 5)
                buckets.setdefault(key, []).append((path, info))
                if index % 40 == 0:
                    self._say(f"Probing {index}/{len(files)}")

        candidates = []
        for key, members in buckets.items():
            pool = list(members) + buckets.get(key + 1, [])
            if len(pool) > 1:
                candidates.append(pool)

        self._say("Comparing frames (first run is the slow one; "
                   "frames are cached after)")
        groups = []
        seen = set()
        hashed: dict = {}

        def frame_hash(path, info):
            if path in hashed:
                return hashed[path]
            data = self.tab.cache.frame(path, info.duration * 0.4, 160)
            value = None
            if data:
                try:
                    value = dhash(Image.open(io.BytesIO(data)))
                except Exception:
                    value = None
            hashed[path] = value
            return value

        for pool in candidates:
            if not self.alive:
                return
            for i in range(len(pool)):
                path_a, info_a = pool[i]
                if path_a in seen:
                    continue
                group = [(path_a, info_a)]
                hash_a = frame_hash(path_a, info_a)
                if hash_a is None:
                    continue
                for j in range(i + 1, len(pool)):
                    path_b, info_b = pool[j]
                    if path_b in seen:
                        continue
                    if abs(info_a.duration - info_b.duration) > 0.25:
                        continue
                    hash_b = frame_hash(path_b, info_b)
                    if hash_b is not None and hamming(hash_a, hash_b) <= 5:
                        group.append((path_b, info_b))
                if len(group) > 1:
                    for path, _ in group:
                        seen.add(path)
                    groups.append(group)

        uithread.post(self._show, groups)

    def _say(self, text):
        if self.alive:
            uithread.post(self.status.configure, text=text)

    def _show(self, groups):
        if not self.alive:
            return
        if not groups:
            self.status.configure(text="No duplicates found", text_color=T.OK)
            return
        wasted = sum(sum(i.size for _, i in g) - max(i.size for _, i in g)
                     for g in groups)
        self.status.configure(
            text=f"{len(groups)} duplicate groups · {fmt_size(wasted)} "
                 f"reclaimable", text_color=T.WARN)

        for row, group in enumerate(groups):
            card = ctk.CTkFrame(self.body, fg_color=T.ELEVATED, corner_radius=8)
            card.grid(row=row, column=0, sticky="ew", padx=8, pady=5)
            card.grid_columnconfigure(0, weight=1)

            best = max(group, key=lambda pair: pair[1].size)
            head = ctk.CTkLabel(
                card, text=f"{len(group)} copies · {fmt_clock(group[0][1].duration)}",
                font=font(10, "bold"), text_color=T.DIM, anchor="w")
            head.grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))

            ctk.CTkButton(
                card, text="Keep largest, delete rest", height=24, width=170,
                corner_radius=6, font=font(10), fg_color=T.BTN,
                hover_color=T.FAIL_DEEP, text_color=T.DIM,
                command=lambda g=group, b=best, c=card: self._cull(g, b, c)
            ).grid(row=0, column=1, padx=10, pady=(8, 2))

            for line, (path, info) in enumerate(sorted(
                    group, key=lambda p: -p[1].size), start=1):
                is_best = path == best[0]
                label = ctk.CTkLabel(
                    card, font=font(10, mono=True), anchor="w",
                    text_color=T.OK if is_best else T.DIM,
                    text=f"{'KEEP ' if is_best else '     '}"
                         f"{info.resolution:>10}  {fmt_size(info.size):>9}  "
                         f"{os.path.basename(path)}")
                label.grid(row=line, column=0, sticky="w", padx=12)
                ctk.CTkButton(
                    card, text="Open", height=20, width=48, corner_radius=5,
                    font=font(9), fg_color=T.BTN, hover_color=T.BTN_HOV,
                    text_color=T.DIM,
                    command=lambda p=path: open_in_explorer(p)
                ).grid(row=line, column=1, padx=10, sticky="e")
            ctk.CTkLabel(card, text="", height=4).grid(row=len(group) + 1, column=0)

    def _cull(self, group, best, card):
        losers = [path for path, _ in group if path != best[0]]
        names = "\n".join(os.path.basename(p) for p in losers)
        if not messagebox.askyesno(
                "Delete duplicates",
                f"Delete these converted copies?\n\n{names}\n\n"
                "Originals in the source folder are not touched.",
                parent=self):
            return
        removed = 0
        for path in losers:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        card.destroy()
        self.tab.log(f"Removed {removed} duplicate copies", "warn")
        self.tab.refresh_census()
