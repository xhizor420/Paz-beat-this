"""The Vault tab: paste a list of post IDs (or filenames) to find them in
the library, then mark the ones you actually used in a named project.

Grabber downloads and finished PMVs both leave you with a pile of numeric
filenames (428483.mp4) and no memory of which ones already went into an
edit. This tab exists for the moment after you finish a project: paste
whatever list you have, find them here, mark them under a project name,
and from then on the Library gallery shows a coloured border on anything
you've already spent - a clip can carry marks from more than one project,
since reused footage across separate edits is normal.

Selecting a project shows its clips as a horizontal roll of thumbnails
(there's no single "the" clip the way Library's inspector has one selected
clip - a project is a set), with the focused thumbnail's details below it
and the full clip list below that. Thumbnails are read from the same
on-disk cache Library's gallery uses and loaded off the UI thread, same
as everywhere else thumbnails are shown.
"""

from __future__ import annotations

import io
import os
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

from .theme import T, font, VAULT_LABELS
from .format import fmt_clock, fmt_short, fmt_size
from .config import THUMB_DIR
from .media import fit_frame, round_corners, thumb_key
from .library_db import (
    db_connect, vault_ensure_project, vault_mark,
    vault_clear_project, vault_rename_project, vault_projects_list,
    vault_cover, vault_set_cover, vault_covers,
)
from .library_player import InlinePlayer
from .library_windows import HelpWindow
from .widgets import popup_menu, menu_rule
from . import artwork, uithread

_SPLIT_RE = re.compile(r"[,\n\r\t;]+|\s+")


def split_terms(text: str) -> list:
    """Pasted list -> individual lookup terms. Commas, semicolons, newlines
    and plain whitespace all work as separators, and order/duplicates in
    the input don't matter for matching."""
    seen = set()
    terms = []
    for raw in _SPLIT_RE.split(text):
        term = raw.strip()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


class VaultTab(ctk.CTkFrame):

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.app = app
        self.root = app.root
        self.cfg = app.cfg
        # InlinePlayer reaches for these on its tab - the Library tab has
        # them under these names, so the Vault does too rather than the
        # player growing a second way to ask.
        self.frames = app.cache
        self.peek = app.peek

        self._results: dict = {}   # tree iid -> Rec
        self._unmatched: list = []

        # Project covers, and the scaled thumbnails made from them.
        # The list is rebuilt on every mark and every rename, so the
        # scaling is cached rather than redone each time.
        self._covers: dict = {}
        self._cover_cache: dict = {}
        self._selected_project: str | None = None
        self._project_clips: list = []
        self._project_rows: dict = {}   # tree iid -> Rec, for the clip list
        self._focused_index: int | None = None
        self._strip_refs: list = []
        self._strip_boxes: list = []
        self._strip_token = 0

        self.grid_columnconfigure(0, weight=1, uniform="cols")
        self.grid_columnconfigure(1, weight=1, uniform="cols")
        self.grid_rowconfigure(1, weight=1)

        self._build()
        self._refresh_projects()
        self.set_status(self.F("empty"), T.FAINT)

    # ── copy ─────────────────────────────────────────────────────────────

    def F(self, key: str, **fmt) -> str:
        text = VAULT_LABELS[key]
        return text.format(**fmt) if fmt else text

    # ── layout ──────────────────────────────────────────────────────────

    def _build(self):
        self._build_topbar()
        self._build_lookup()
        self._build_projects()

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0, height=58)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=20, pady=10)
        ctk.CTkLabel(left, text="PAZ", font=font(19, "bold"),
                     text_color=T.ACCENT3).pack(side="left")
        ctk.CTkLabel(left, text="Vault", font=font(19), text_color=T.TEXT
                     ).pack(side="left", padx=(5, 0))
        ctk.CTkLabel(left, text=self.F("tagline"), font=font(10, mono=True),
                     text_color=T.FAINT).pack(side="left", padx=(12, 0), pady=(6, 0))

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=20)
        ctk.CTkButton(right, text="Settings", width=80, height=30, corner_radius=7,
                     font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.DIM, command=self._open_settings
                     ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(right, text="?", width=30, height=30, corner_radius=7,
                     font=font(12, "bold"), fg_color=T.BTN, hover_color=T.BTN_HOV,
                     text_color=T.FAINT, command=self._open_help).pack(side="left")

    def _build_lookup(self):
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=14)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(panel, text="PASTE POST IDS OR FILENAMES", font=font(9, "bold"),
                     text_color=T.FAINT, anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.paste_box = ctk.CTkTextbox(
            panel, height=110, corner_radius=10, fg_color=T.SURFACE,
            border_width=1, border_color=T.ACCENT3_DEEP, text_color=T.TEXT,
            font=font(11, mono=True))
        self.paste_box.grid(row=1, column=0, sticky="ew")

        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", pady=(8, 10))
        ctk.CTkButton(row, text="Look up", width=100, height=32, corner_radius=7,
                      font=font(11, "bold"), fg_color=T.ACCENT3_DEEP, hover_color=T.BTN_HOV,
                      text_color=T.ACCENT3, command=self._run_lookup).pack(side="left")
        ctk.CTkButton(row, text="Clear", width=80, height=32, corner_radius=7,
                      font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._clear_lookup).pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="Select all", width=90, height=32, corner_radius=7,
                      font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._select_all).pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="Select none", width=100, height=32, corner_radius=7,
                      font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._select_none).pack(side="left", padx=(8, 0))

        self.status_label = ctk.CTkLabel(panel, text="", font=font(10),
                                         text_color=T.DIM, anchor="w", justify="left",
                                         wraplength=560)
        self.status_label.grid(row=2, column=0, sticky="e")

        self._build_style()
        tree_wrap = ctk.CTkFrame(panel, fg_color=T.SURFACE, corner_radius=12,
                                 border_width=1, border_color=T.ACCENT3_DEEP)
        tree_wrap.grid(row=3, column=0, sticky="nsew")
        tree_wrap.grid_columnconfigure(0, weight=1)
        tree_wrap.grid_rowconfigure(0, weight=1)

        columns = ("name", "artist", "res", "len", "used")
        self.tree = ttk.Treeview(tree_wrap, style="V.Treeview", columns=columns,
                                 show="headings", selectmode="extended")
        for key, title, width, anchor in (
                ("name", "File", 220, "w"), ("artist", "Artist", 130, "w"),
                ("res", "Resolution", 90, "w"), ("len", "Length", 64, "e"),
                ("used", "Already used in", 190, "w")):
            self.tree.column(key, width=width, minwidth=60, anchor=anchor)
            self.tree.heading(key, text=title)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = ctk.CTkScrollbar(tree_wrap, command=self.tree.yview, width=12,
                                  button_color=T.LINE, button_hover_color=T.FAINT)
        scroll.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=8)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self._open_selected())

        mark_row = ctk.CTkFrame(panel, fg_color="transparent")
        mark_row.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(mark_row, text="Project:", font=font(11), text_color=T.DIM
                     ).pack(side="left", padx=(0, 6))
        self.project_box = ctk.CTkComboBox(
            mark_row, width=220, height=32, corner_radius=7, font=font(11),
            fg_color=T.INPUT, border_color=T.ACCENT3_DEEP, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED, dropdown_hover_color=T.ACCENT3_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11),
            text_color=T.TEXT, values=[])
        self.project_box.pack(side="left")
        self.project_box.set("")
        ctk.CTkButton(mark_row, text="Mark selected as used", height=32, corner_radius=7,
                      font=font(11, "bold"), fg_color=T.ACCENT3_DEEP, hover_color=T.BTN_HOV,
                      text_color=T.ACCENT3, command=self._mark_selected
                      ).pack(side="left", padx=(10, 0))

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("V.Treeview", background=T.ROW, fieldbackground=T.ROW,
                        foreground=T.TEXT, rowheight=26, borderwidth=0, font=(T.UI, 10))
        style.configure("V.Treeview.Heading", background=T.ELEVATED, foreground=T.FAINT,
                        relief="flat", borderwidth=0, font=(T.UI, 9, "bold"), padding=(8, 7))
        style.map("V.Treeview.Heading", background=[("active", T.BTN_HOV)])
        style.map("V.Treeview", background=[("selected", T.ROW_SEL)],
                  foreground=[("selected", T.TEXT)])

    STRIP_W, STRIP_H = 112, 63
    STRIP_GAP = 6

    def _build_projects(self):
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=14)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(panel, text="PROJECTS · click a name to browse its clips",
                     font=font(9, "bold"), text_color=T.FAINT, anchor="w"
                     ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.projects_list = ctk.CTkScrollableFrame(
            panel, fg_color=T.SURFACE, corner_radius=10, border_width=1,
            border_color=T.ACCENT3_DEEP, height=118,
            scrollbar_button_color=T.LINE, scrollbar_button_hover_color=T.FAINT)
        self.projects_list.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.projects_list.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        head.grid_columnconfigure(0, weight=1)
        self.detail_header = ctk.CTkLabel(head, text="Select a project above",
                                          font=font(11, "bold"), text_color=T.FAINT, anchor="w")
        self.detail_header.grid(row=0, column=0, sticky="w")
        self.open_in_library_btn = ctk.CTkButton(
            head, text="Open in Library →", width=130, height=24, corner_radius=6,
            font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV, text_color=T.ACCENT2,
            state="disabled", command=lambda: self._open_in_library(self._selected_project))
        self.open_in_library_btn.grid(row=0, column=1, sticky="e")

        strip_wrap = ctk.CTkFrame(panel, fg_color=T.SURFACE, corner_radius=10,
                                  border_width=1, border_color=T.ACCENT3_DEEP)
        strip_wrap.grid(row=3, column=0, sticky="ew")
        strip_wrap.grid_columnconfigure(0, weight=1)
        self.strip_canvas = tk.Canvas(strip_wrap, bg=T.SURFACE, highlightthickness=0,
                                      height=self.STRIP_H + 8)
        self.strip_canvas.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        strip_scroll = ctk.CTkScrollbar(
            strip_wrap, command=self.strip_canvas.xview, orientation="horizontal",
            height=10, button_color=T.LINE, button_hover_color=T.FAINT)
        strip_scroll.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        self.strip_canvas.configure(xscrollcommand=strip_scroll.set)
        self.strip_canvas.bind("<Button-1>", self._strip_click)
        self.strip_canvas.bind("<MouseWheel>", self._strip_wheel, add="+")
        self.strip_canvas.bind("<Button-4>", self._strip_wheel, add="+")
        self.strip_canvas.bind("<Button-5>", self._strip_wheel, add="+")
        self._draw_strip_placeholder("Select a project above to see its clips")

        self.focused_info = ctk.CTkLabel(panel, text="", font=font(10, mono=True),
                                         text_color=T.DIM, anchor="w", wraplength=460,
                                         justify="left")
        self.focused_info.grid(row=4, column=0, sticky="ew", pady=(6, 6))

        # The clip list and the player, side by side. A project is a list
        # of clips you have already spent, and the question you ask of it
        # is "which one was that?" - a filename and a thumbnail do not
        # answer that for a four-minute clip, and having to open an
        # external player to find out is the long way round.
        bottom = ctk.CTkFrame(panel, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="nsew")
        bottom.grid_rowconfigure(0, weight=1)
        bottom.grid_columnconfigure(0, weight=1)

        list_wrap = ctk.CTkFrame(bottom, fg_color=T.SURFACE, corner_radius=10,
                                 border_width=1, border_color=T.ACCENT3_DEEP)
        list_wrap.grid(row=0, column=0, sticky="nsew")
        list_wrap.grid_columnconfigure(0, weight=1)
        list_wrap.grid_rowconfigure(0, weight=1)

        # Two columns, not three: the list now shares this row with the
        # player, and the artist is on the caption under it. Three columns
        # in the room left over meant the length was clipped off the end,
        # which is the one thing on this row you cannot get anywhere else.
        columns = ("name", "len")
        self.project_tree = ttk.Treeview(list_wrap, style="V.Treeview", columns=columns,
                                         show="headings", selectmode="browse")
        for key, title, width, anchor, stretch in (
                ("name", "File", 150, "w", True),
                ("len", "Len", 58, "e", False)):
            self.project_tree.column(key, width=width, minwidth=48,
                                     anchor=anchor, stretch=stretch)
            self.project_tree.heading(key, text=title)
        self.project_tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        list_scroll = ctk.CTkScrollbar(list_wrap, command=self.project_tree.yview, width=12,
                                       button_color=T.LINE, button_hover_color=T.FAINT)
        list_scroll.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=8)
        self.project_tree.configure(yscrollcommand=list_scroll.set)
        self.project_tree.bind("<<TreeviewSelect>>", self._on_project_tree_select)
        self.project_tree.bind("<Double-1>", lambda e: self._play_focused())
        self.project_tree.bind("<Return>", lambda e: self._play_focused())

        self._build_player(bottom)

    # ── the player ───────────────────────────────────────────────────────

    PLAYER_W = 424

    def _build_player(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=T.SURFACE, corner_radius=10,
                            border_width=1, border_color=T.ACCENT3_DEEP)
        card.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        card.grid_columnconfigure(0, weight=1)
        self._player_card = card

        self.player = InlinePlayer(card, self)
        self.player.frame.grid(row=0, column=0, padx=8, pady=(8, 4))
        self.player_caption = ctk.CTkLabel(
            card, text="Pick a clip to see it", font=font(10, mono=True),
            text_color=T.FAINT, anchor="w", wraplength=self.PLAYER_W - 16,
            justify="left")
        self.player_caption.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        # Sized once the panel has a real width, not from the numbers above.
        self.after(250, self._fit_player)
        self.bind("<Configure>", lambda e: self._player_resize(), add="+")

    def _player_resize(self) -> None:
        if getattr(self, "_player_job", None):
            try:
                self.after_cancel(self._player_job)
            except ValueError:
                pass
        self._player_job = self.after(220, self._fit_player)

    def _fit_player(self) -> None:
        """Give the player whatever room the window can spare.

        The clip list needs a readable minimum and the rest is the
        player's, so the picture grows with the window instead of staying
        the size it happened to be built at. Height has a say too: a
        16:9 picture as wide as the panel allows is taller than a short
        window has room for, and the overflow goes exactly where it is
        least welcome - the transport buttons and the caption, off the
        bottom edge."""
        self._player_job = None
        try:
            room = int(self._player_card.master.winfo_width())
            tall = int(self._player_card.master.winfo_height())
        except tk.TclError:
            return
        if room <= 1:
            self.after(300, self._fit_player)
            return
        width = max(min(room - self.LIST_MIN_W, int(room * 0.62)), 300)
        # Everything in the card that isn't the picture - seek bar,
        # buttons, caption, padding - measured rather than guessed, so
        # the fit survives a change to any of them.
        chrome = max(self._player_chrome(), 60)
        picture = max(tall - chrome, 135)
        width = max(min(width, int(picture * 16 / 9)), 300)
        self.player.set_size(width - 20)
        self.player_caption.configure(wraplength=width - 30)

    def _player_chrome(self) -> int:
        try:
            return (int(self._player_card.winfo_reqheight())
                    - int(self.player.stack.winfo_reqheight()))
        except tk.TclError:
            return 0

    # A clip list narrower than this is not a list any more - the file
    # name column alone needs most of it.
    LIST_MIN_W = 380

    def _play_focused(self) -> None:
        """Double-click plays it here, the same as in the Library."""
        if self._focused_index is None:
            return
        self._show_in_player(self._project_clips[self._focused_index])
        self.player.play()

    def _show_in_player(self, rec) -> None:
        player = getattr(self, "player", None)
        if player is None:
            return
        player.show_rec(rec)
        if rec is None:
            self.player_caption.configure(text="Pick a clip to see it",
                                          text_color=T.FAINT)
            return
        bits = [rec.name]
        if rec.width:
            bits.append(f"{rec.width}x{rec.height}")
        bits.append(fmt_clock(rec.duration))
        if rec.artists:
            bits.append(", ".join(rec.artists[:2]))
        self.player_caption.configure(text="  ·  ".join(bits), text_color=T.DIM)

    def on_hidden(self) -> None:
        """Another tab is in front now. Sound coming out of a page nobody
        can see is worse than no sound at all."""
        player = getattr(self, "player", None)
        if player is not None and player.playing:
            player.pause()

    # ── status ──────────────────────────────────────────────────────────

    def set_status(self, text: str, colour: str = T.DIM) -> None:
        self.status_label.configure(text=text, text_color=colour)

    # ── lookup ──────────────────────────────────────────────────────────

    def _clear_lookup(self) -> None:
        self.paste_box.delete("1.0", tk.END)
        self.tree.delete(*self.tree.get_children())
        self._results = {}
        self._unmatched = []
        self.set_status(self.F("empty"), T.FAINT)

    def _run_lookup(self) -> None:
        terms = split_terms(self.paste_box.get("1.0", tk.END))
        self.tree.delete(*self.tree.get_children())
        self._results = {}
        if not terms:
            self.set_status(self.F("empty"), T.FAINT)
            return

        library = getattr(self.app, "library", None)
        records = library.records if library else []
        if not records:
            self.set_status("No library indexed yet - sync the Library tab first.", T.WARN)
            return

        by_pid = {r.pid: r for r in records if r.pid}
        matched: list = []
        matched_paths: set = set()
        self._unmatched = []
        for term in terms:
            stem = os.path.splitext(term)[0]
            rec = by_pid.get(stem) or by_pid.get(term)
            if rec is None and not stem.isdigit():
                needle = stem.lower()
                for candidate in records:
                    if needle in candidate.name.lower() and candidate.path not in matched_paths:
                        matched.append(candidate)
                        matched_paths.add(candidate.path)
                continue
            if rec is None:
                self._unmatched.append(term)
                continue
            if rec.path not in matched_paths:
                matched.append(rec)
                matched_paths.add(rec.path)

        for index, rec in enumerate(matched):
            iid = f"v{index}"
            self._results[iid] = rec
            used = ", ".join(rec.used_projects) if rec.used_projects else "--"
            self.tree.insert("", "end", iid=iid, values=(
                rec.name, ", ".join(rec.artists[:2]) or "--",
                f"{rec.width}x{rec.height}" if rec.width else "--",
                fmt_short(rec.duration), used))

        bits = [f"{len(matched)} found"]
        if self._unmatched:
            shown = ", ".join(self._unmatched[:12])
            more = f" (+{len(self._unmatched) - 12} more)" if len(self._unmatched) > 12 else ""
            bits.append(f"{len(self._unmatched)} not found: {shown}{more}")
        self.set_status(" · ".join(bits), T.OK if matched else T.WARN)

    def _select_all(self) -> None:
        self.tree.selection_set(list(self._results.keys()))

    def _select_none(self) -> None:
        self.tree.selection_remove(*self.tree.selection())

    def _open_selected(self) -> None:
        iid = self.tree.focus()
        rec = self._results.get(iid)
        if not rec:
            return
        from .files import open_file
        open_file(rec.path)

    def _open_settings(self) -> None:
        self.app.open_settings(initial_tab="Library")

    def _open_help(self) -> None:
        HelpWindow(self.root)

    # ── marking ─────────────────────────────────────────────────────────

    def _next_color(self, conn) -> str:
        existing = len(vault_projects_list(conn))
        return T.PROJECT_PALETTE[existing % len(T.PROJECT_PALETTE)]

    def _mark_selected(self) -> None:
        project = self.project_box.get().strip()
        if not project:
            self.set_status("Name the project first - type a new one or pick "
                            "an existing one.", T.WARN)
            return
        selected = [self._results[iid] for iid in self.tree.selection() if iid in self._results]
        if not selected:
            self.set_status("Select at least one clip in the results first.", T.WARN)
            return

        conn = db_connect()
        try:
            existing_names = {name for name, _c, _n, _t in vault_projects_list(conn)}
            if project not in existing_names:
                vault_ensure_project(conn, project, self._next_color(conn))
            vault_mark(conn, [rec.path for rec in selected], project)
        finally:
            conn.close()

        self._reload_library()
        self._refresh_projects()
        self.set_status(f"Marked {len(selected)} clip{'s' if len(selected) != 1 else ''} "
                        f"as used in '{project}'.", T.OK)

    def _reload_library(self) -> None:
        library = getattr(self.app, "library", None)
        if library is None:
            return
        library._load_library()
        library.run_search()
        # Rec objects are rebuilt fresh by _load_library(), so the results
        # table's and the project detail's references would otherwise go
        # stale - re-run whichever of the two is currently showing.
        if self._results:
            self._run_lookup()
        if self._selected_project:
            self._load_project_clips()

    # ── projects panel ──────────────────────────────────────────────────

    def _refresh_projects(self) -> None:
        for child in self.projects_list.winfo_children():
            child.destroy()
        conn = db_connect()
        try:
            projects = vault_projects_list(conn)
            self._covers = vault_covers(conn)
        finally:
            conn.close()

        self.project_box.configure(values=[name for name, _c, _n, _t in projects])

        if not projects:
            ctk.CTkLabel(self.projects_list, text="No projects yet - mark some clips "
                                                   "as used to start one.",
                         font=font(11), text_color=T.FAINT, wraplength=260,
                         justify="left").grid(row=0, column=0, padx=10, pady=10, sticky="w")
            return

        for row, (name, color, count, _created) in enumerate(projects):
            selected = name == self._selected_project
            line = ctk.CTkFrame(self.projects_list, fg_color=T.ELEVATED if selected else "transparent",
                                corner_radius=6, height=28)
            line.grid(row=row, column=0, sticky="ew", pady=1)
            line.grid_columnconfigure(1, weight=1)
            line.grid_propagate(False)

            # The cover if there is one, the colour bar if there isn't.
            # A project you have made a thumbnail for is easier to pick out
            # of a list by that thumbnail than by a coloured line.
            cover = self._cover_photo(name)
            if cover is not None:
                swatch = tk.Label(line, image=cover, bd=0,
                                  bg=T.ELEVATED if selected else T.BG)
                swatch.image = cover
            else:
                swatch = ctk.CTkFrame(line, width=6, height=16,
                                      fg_color=color, corner_radius=3)
            swatch.grid(row=0, column=0, padx=(8, 8), pady=4)

            label = ctk.CTkButton(
                line, text=f"{name}   ·   {count}", font=font(11), anchor="w", height=26,
                fg_color="transparent", hover_color=T.BTN_HOV,
                text_color=T.TEXT if selected else T.DIM,
                command=lambda n=name: self._select_project(n))
            label.grid(row=0, column=1, sticky="ew", padx=(0, 6))
            label.bind("<Button-3>", lambda e, n=name: self._project_menu(e, n))

    COVER_W, COVER_H = 48, 27          # 16:9, sized to the row

    def _cover_photo(self, name: str):
        """The project's cover, scaled into the list row. Cached, because
        the list is rebuilt on every mark and every rename."""
        crop = self._covers.get(name)
        if not crop or not crop.get("path"):
            return None
        key = (name, crop["path"], crop["zoom"], crop["fx"], crop["fy"])
        hit = self._cover_cache.get(key)
        if hit is not None:
            return hit
        picture = artwork.render(crop["path"], (self.COVER_W, self.COVER_H),
                                 crop["zoom"], crop["fx"], crop["fy"])
        if picture is None:
            return None
        photo = ImageTk.PhotoImage(picture)
        self._cover_cache[key] = photo
        return photo

    def _pick_cover(self, name: str) -> None:
        from .artwork_window import ArtworkWindow
        conn = db_connect()
        try:
            saved = vault_cover(conn, name)
        finally:
            conn.close()

        def keep(crop):
            inner = db_connect()
            try:
                vault_set_cover(inner, name, crop["path"], crop["zoom"],
                                crop["fx"], crop["fy"])
            finally:
                inner.close()
            self._cover_cache.clear()
            self.refresh()
            self.set_status(
                f"Cover {'set' if crop['path'] else 'cleared'} for “{name}”.",
                T.OK)

        ArtworkWindow(self.root, self.app, artwork.PROJECT_COVER,
                      saved=saved, on_save=keep)

    def _project_menu(self, event, name: str) -> None:
        menu = popup_menu(self.root, activebackground=T.ACCENT3_DEEP,
                          activeforeground=T.ACCENT3)
        menu.add_command(label="Browse clips here", command=lambda: self._select_project(name))
        menu.add_command(label="Open in Library", command=lambda: self._open_in_library(name))
        menu_rule(menu)
        menu.add_command(label="Add all clips to Resolve",
                         command=lambda: self._send_project_to_resolve(name))
        menu_rule(menu)
        has_cover = bool(self._covers.get(name, {}).get("path"))
        menu.add_command(label=f"Cover picture…{'  ✓' if has_cover else ''}",
                         command=lambda: self._pick_cover(name))
        menu.add_command(label="Rename…", command=lambda: self._rename_project(name))
        menu.add_command(label="Clear", command=lambda: self._clear_project(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _send_project_to_resolve(self, project: str) -> None:
        """Every clip marked as used in this project, into a Resolve bin
        named after it. The Library owns the actual hand-off - it is the
        tab that knows which copy of a clip is the 4K one."""
        library = getattr(self.app, "library", None)
        if library is None:
            return
        clips = [r for r in library.records if project in r.used_projects]
        if not clips:
            self.set_status(f"'{project}' has no clips marked yet.", T.WARN)
            return
        library.send_to_resolve(clips, bin_name=project)
        self.set_status(f"Sending {len(clips)} clips from '{project}' to "
                        f"Resolve - see the Library tab for the result.", T.DIM)

    def _open_in_library(self, project: str | None) -> None:
        library = getattr(self.app, "library", None)
        if library is None or not project:
            return
        library.search.delete(0, tk.END)
        library.search.insert(0, f'used:"{project}"')
        library.run_search()
        self.app.tabview.set("Library")

    def _rename_project(self, name: str) -> None:
        dialog = ctk.CTkInputDialog(text=f"Rename '{name}' to:", title="Rename project")
        new_name = (dialog.get_input() or "").strip()
        if not new_name or new_name == name:
            return
        conn = db_connect()
        try:
            vault_rename_project(conn, name, new_name)
        finally:
            conn.close()
        if self._selected_project == name:
            self._selected_project = new_name
        self._reload_library()
        self._refresh_projects()
        self.set_status(f"Renamed '{name}' to '{new_name}'.", T.OK)

    def _clear_project(self, name: str) -> None:
        if not messagebox.askyesno(
                "Clear project",
                f"Remove every 'used in {name}' mark? This only clears the "
                "mark - the clips themselves are untouched.", parent=self):
            return
        conn = db_connect()
        try:
            vault_clear_project(conn, name)
        finally:
            conn.close()
        if self._selected_project == name:
            self._selected_project = None
            self._project_clips = []
            self._show_project_detail()
        self._reload_library()
        self._refresh_projects()
        self.set_status(f"Cleared '{name}'.", T.OK)

    # ── project detail: thumbnail roll + focused clip + clip list ──────

    def _select_project(self, name: str) -> None:
        self._selected_project = name
        self._refresh_projects()
        self._load_project_clips()

    def _load_project_clips(self) -> None:
        library = getattr(self.app, "library", None)
        records = library.records if library else []
        name = self._selected_project
        clips = [r for r in records if name in r.used_projects]
        clips.sort(key=lambda r: r.name.lower())
        self._project_clips = clips
        self._focused_index = 0 if clips else None
        self._show_project_detail()

    def _show_project_detail(self) -> None:
        name = self._selected_project
        if not name:
            self.detail_header.configure(text="Select a project above", text_color=T.FAINT)
            self.open_in_library_btn.configure(state="disabled")
            self._draw_strip_placeholder("Select a project above to see its clips")
            self.focused_info.configure(text="")
            self.project_tree.delete(*self.project_tree.get_children())
            self._project_rows = {}
            self._show_in_player(None)
            return
        count = len(self._project_clips)
        self.detail_header.configure(
            text=f"{name}  ·  {count} clip{'s' if count != 1 else ''}", text_color=T.TEXT)
        self.open_in_library_btn.configure(state="normal")
        self._fill_project_tree()
        if not self._project_clips:
            self._draw_strip_placeholder("Nothing marked used in this project yet")
            self.focused_info.configure(text="")
            self._show_in_player(None)
            return
        self._draw_strip()
        self._update_focused_info()
        # The first clip is already the focused one, so the player should
        # be holding it rather than sitting empty next to a full list.
        if self._focused_index is not None:
            self._show_in_player(self._project_clips[self._focused_index])

    def _fill_project_tree(self) -> None:
        self.project_tree.delete(*self.project_tree.get_children())
        self._project_rows = {}
        for index, rec in enumerate(self._project_clips):
            iid = f"p{index}"
            self._project_rows[iid] = rec
            self.project_tree.insert("", "end", iid=iid, values=(
                rec.name, fmt_short(rec.duration)))
        if self._focused_index is not None and self._project_clips:
            self.project_tree.selection_set(f"p{self._focused_index}")

    def _on_project_tree_select(self, _event=None) -> None:
        selection = self.project_tree.selection()
        if not selection:
            return
        rec = self._project_rows.get(selection[0])
        if rec is None:
            return
        index = self._project_clips.index(rec)
        if index != self._focused_index:
            old = self._focused_index
            self._focused_index = index
            self._update_strip_highlight(old, index)
            self._update_focused_info()
            self._show_in_player(rec)

    def _open_focused(self) -> None:
        if self._focused_index is None:
            return
        rec = self._project_clips[self._focused_index]
        from .files import open_file
        open_file(rec.path)

    def _update_focused_info(self) -> None:
        if self._focused_index is None or not self._project_clips:
            self.focused_info.configure(text="")
            return
        rec = self._project_clips[self._focused_index]
        # What the caption under the player doesn't already say. Repeating
        # the name, size and length either side of the same panel is two
        # lines doing one line's work.
        bits = [fmt_size(rec.size), rec.folder or ""]
        if len(rec.used_projects) > 1:
            others = [p for p in rec.used_projects if p != self._selected_project]
            bits.append("also used: " + ", ".join(others))
        self.focused_info.configure(text="  ·  ".join(b for b in bits if b))

    # ── thumbnail roll ───────────────────────────────────────────────────

    def _draw_strip_placeholder(self, text: str) -> None:
        self._strip_token += 1
        c = self.strip_canvas
        c.delete("all")
        c.create_text(10, (self.STRIP_H + 8) // 2, text=text, fill=T.FAINT,
                      font=(T.UI, 10), anchor="w")
        c.configure(scrollregion=(0, 0, 0, self.STRIP_H + 8))

    def _draw_strip(self) -> None:
        self._strip_token += 1
        token = self._strip_token
        c = self.strip_canvas
        c.delete("all")
        self._strip_refs = []
        self._strip_boxes = []
        x = 4
        for index in range(len(self._project_clips)):
            self._strip_boxes.append((x, x + self.STRIP_W, index))
            selected = index == self._focused_index
            c.create_rectangle(x, 2, x + self.STRIP_W, 2 + self.STRIP_H,
                               fill=T.INPUT, outline=T.ACCENT3 if selected else T.LINE,
                               width=2 if selected else 1, tags=(f"cell{index}", f"rect{index}"))
            c.create_text(x + self.STRIP_W // 2, 2 + self.STRIP_H // 2, text="…",
                          fill=T.FAINT, font=(T.UI, 9), tags=(f"ph{index}", f"cell{index}"))
            x += self.STRIP_W + self.STRIP_GAP
        c.configure(scrollregion=(0, 0, x, self.STRIP_H + 8))
        clips = list(self._project_clips)
        threading.Thread(target=self._load_strip_thumbs, args=(clips, token), daemon=True).start()

    def _load_strip_thumbs(self, clips: list, token: int) -> None:
        for index, rec in enumerate(clips):
            if token != self._strip_token:
                return
            data = None
            try:
                with open(os.path.join(THUMB_DIR, thumb_key(rec.path)), "rb") as fh:
                    data = fh.read()
            except OSError:
                pass
            uithread.post(self._place_strip_thumb, index, data, token)

    def _place_strip_thumb(self, index: int, data, token: int) -> None:
        if token != self._strip_token or index >= len(self._strip_boxes):
            return
        c = self.strip_canvas
        c.delete(f"ph{index}")
        if not data:
            return
        x0, _x1, _i = self._strip_boxes[index]
        try:
            image = Image.open(io.BytesIO(data))
            image = fit_frame(image, self.STRIP_W, self.STRIP_H, "cover")
            image = round_corners(image, 6, T.SURFACE)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return
        self._strip_refs.append(photo)
        c.create_image(x0, 2, image=photo, anchor="nw", tags=(f"cell{index}",))
        c.tag_raise(f"cell{index}")

    def _focus_clip(self, index: int) -> None:
        if index == self._focused_index or not (0 <= index < len(self._project_clips)):
            return
        old = self._focused_index
        self._focused_index = index
        self._update_strip_highlight(old, index)
        self._update_focused_info()
        self._show_in_player(self._project_clips[index])
        iid = f"p{index}"
        if iid in self._project_rows:
            self.project_tree.selection_set(iid)
            self.project_tree.see(iid)

    def _update_strip_highlight(self, old_index: int | None, new_index: int | None) -> None:
        """Just recolours the two affected cell outlines - moving focus
        shouldn't re-read every thumbnail off disk and flash the strip
        back to placeholders, which a full _draw_strip() would do."""
        c = self.strip_canvas
        for index, colour, width in ((old_index, T.LINE, 1), (new_index, T.ACCENT3, 2)):
            if index is None:
                continue
            for item in c.find_withtag(f"rect{index}"):
                c.itemconfigure(item, outline=colour, width=width)

    def _strip_click(self, event) -> None:
        x = self.strip_canvas.canvasx(event.x)
        for x0, x1, index in self._strip_boxes:
            if x0 <= x <= x1:
                self._focus_clip(index)
                return

    def _strip_wheel(self, event) -> None:
        if getattr(event, "num", None) == 4:
            steps = -2
        elif getattr(event, "num", None) == 5:
            steps = 2
        else:
            delta = getattr(event, "delta", 0)
            steps = -int(delta / 120) * 2 if delta else 0
        if steps:
            self.strip_canvas.xview_scroll(steps, "units")

    # ── keyboard / lifecycle (dispatched centrally by the app) ─────────

    @staticmethod
    def is_typing(event) -> bool:
        return isinstance(event.widget, (ctk.CTkEntry, tk.Entry, tk.Text, ttk.Entry))

    def key_lookup(self, event=None) -> None:
        self._run_lookup()

    def on_app_close(self) -> bool:
        # mpv and VLC are separate processes embedded in our window.
        # Killing the window usually takes them with it, but "usually" is
        # not a shutdown.
        player = getattr(self, "player", None)
        shutdown = getattr(getattr(player, "engine", None), "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:
                pass
        return True

    def after_settings_saved(self) -> None:
        pass
