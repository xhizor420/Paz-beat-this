"""The Library tab: e621-style search over the converted library, a canvas
gallery with hover-scrub, an embedded player, tag sidebar, and the
Fix-missing / Fetch-tags / Sync pipeline.
"""

from __future__ import annotations

import collections
import io
import os
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from .theme import T, font, LIBRARY_LABELS
from .format import fmt_len, fmt_clock, fmt_size, fmt_score
from .files import (
    is_ignored_dir, in_ignored_path, post_id_from, open_file, open_in_explorer,
)
from .config import THUMB_DIR
from .media import fit_frame, round_corners, thumb_key, make_thumb, probe
from .e621 import E621_POST
from .library_db import (
    db_connect, Rec, parse_query, rec_matches, SORTS,
    vault_marks_by_path, vault_unmark, vault_projects_list, vault_ensure_project, vault_mark,
)
from .library_player import InlinePlayer
from . import uithread
from .library_windows import HiddenTagsWindow, HelpWindow, FoldersWindow, VerifyWindow
from .convert_widgets import ContactSheet

RATIO_TOKENS = ("is:portrait", "is:widescreen", "is:square")


class LibraryTab(ctk.CTkFrame):

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.app = app
        self.root = app.root
        self.cfg = app.cfg
        self.emeta = app.emeta
        self.frames = app.cache          # shared frame/thumbnail cache
        self.toaster = app.toaster
        self.peek = app.peek

        self.records: list[Rec] = []
        self.by_path: dict = {}
        self.tag_universe: set = set()
        self.filtered: list[Rec] = []
        self._project_colors: dict = {}
        self.page = 0
        self.selected: Rec | None = None

        self.busy = False
        self._page_token = 0
        self._page_refs: list = []
        self._layout: list = []
        self._hover_index = None
        self._peek_after = None
        self._peek_token = 0
        self._peek_path: str | None = None
        self._peek_busy = False
        self._peek_pending = None
        self._search_after = None
        self._resize_after = None
        self._columns = 0

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build()
        self._bind_local_keys()
        self._apply_brand()
        import tkinter.font as tkfont
        self._card_font = tkfont.Font(family=T.UI, size=10)
        self._badge_font = tkfont.Font(family=T.MONO, size=8)
        self._spec_font = tkfont.Font(family=T.MONO, size=9)
        self.folders_label.configure(text=self._folders_summary())
        self._restore_state()
        self._load_library()
        if self.records:
            self.set_status(self.F("idle"), T.FAINT)
        else:
            self.set_status("No index yet", T.WARN)
        self.run_search()
        # Quietly pick up tags for anything fresh as soon as the tab opens,
        # same small ambient batch a sync folds in - no need to wait for a
        # manual Sync just to notice new post IDs. The Fetch button itself
        # is reserved for a real, user-requested full pass (see _fetch_tags).
        if self.records and self.cfg.library_autofetch and self.cfg.e621_enabled:
            self.after(800, self._fetch_tags)

    # ── copy ─────────────────────────────────────────────────────────────

    def F(self, key: str, **fmt) -> str:
        text = LIBRARY_LABELS[key]
        return text.format(**fmt) if fmt else text

    def ui(self, fn, *args, **kwargs):
        uithread.post(fn, *args, **kwargs)

    # ── layout ──────────────────────────────────────────────────────────────

    def _build(self):
        self._build_topbar()
        self._build_sidebar()
        self._build_grid_area()
        self._build_details()

    def _build_topbar(self):
        """
        Two rows instead of one long one: browsing controls up top (brand,
        search, rating/sort), a separate action toolbar underneath (sync +
        maintenance on the left, configuration on the right). The standalone
        Folders button is gone - Settings > Library > "Change folders..."
        and Ctrl+O both still reach it, so it doesn't need its own slot in
        an already busy row.
        """
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")

        # ── row 1: browse ────────────────────────────────────────────────
        row1 = ctk.CTkFrame(bar, fg_color="transparent")
        row1.pack(fill="x")
        row1.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(row1, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=18, pady=(10, 6))
        self.brand_name = ctk.CTkLabel(left, text="PAZ", font=font(16, "bold"),
                                        text_color=T.ACCENT2)
        self.brand_name.pack(side="left")
        self.brand_kind = ctk.CTkLabel(left, text="Library", font=font(16), text_color=T.TEXT)
        self.brand_kind.pack(side="left", padx=(4, 0))
        self.brand_sub = ctk.CTkLabel(left, text="", font=font(10, mono=True),
                                       text_color=T.FAINT)
        self.brand_sub.pack(side="left", padx=(10, 0), pady=(3, 0))

        mid = ctk.CTkFrame(row1, fg_color="transparent")
        mid.grid(row=0, column=1, sticky="ew", padx=8, pady=(10, 6))
        mid.grid_columnconfigure(0, weight=1)

        self.search = ctk.CTkEntry(
            mid, placeholder_text="Search: wolf -mlp artist:name rating:e "
                                  "folder:Furry  ( / to focus )",
            height=34, font=font(12), corner_radius=8, fg_color=T.INPUT,
            border_color=T.LINE, border_width=1, text_color=T.TEXT)
        self.search.grid(row=0, column=0, sticky="ew")
        self.search.bind("<KeyRelease>", self._on_search_key)
        self.search.bind("<Return>", self._commit_search)
        self.search.bind("<Up>", lambda e: self._history_step(-1))
        self.search.bind("<Down>", lambda e: self._history_step(1))
        self.search.bind("<Escape>", lambda e: self._clear_search())
        self._history_pos = -1
        ctk.CTkButton(mid, text="↺", width=30, height=34, corner_radius=8,
                      font=font(13), fg_color=T.INPUT, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._show_history
                      ).grid(row=0, column=1, padx=(6, 0))
        ctk.CTkButton(mid, text="✕", width=30, height=34, corner_radius=8,
                      font=font(12), fg_color=T.INPUT, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._clear_search
                      ).grid(row=0, column=2, padx=(4, 0))

        right1 = ctk.CTkFrame(row1, fg_color="transparent")
        right1.grid(row=0, column=2, sticky="e", padx=16, pady=(10, 6))

        self.rating_seg = ctk.CTkSegmentedButton(
            right1, values=["All", "S", "Q", "E"], command=lambda _v: self.run_search(),
            font=font(10), height=30, corner_radius=7, fg_color=T.INPUT,
            selected_color=T.ACCENT_DEEP, selected_hover_color=T.ACCENT_DEEP,
            unselected_color=T.INPUT, unselected_hover_color=T.BTN_HOV,
            text_color=T.DIM, border_width=1)
        self.rating_seg.set("All")
        self.rating_seg.pack(side="left", padx=(0, 8))

        self.sort_menu = ctk.CTkOptionMenu(
            right1, values=list(SORTS), width=110, height=30, font=font(11),
            corner_radius=7, fg_color=T.INPUT, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED,
            text_color=T.TEXT, command=lambda _v: self.run_search())
        self.sort_menu.set(self.cfg.sort if self.cfg.sort in SORTS else "Newest")
        self.sort_menu.pack(side="left", padx=(0, 6))

        ctk.CTkButton(right1, text="▲ Top", width=54, height=30, corner_radius=7,
                      font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.ACCENT2, command=lambda: self.add_token("sort:score")
                      ).pack(side="left")

        # ── row 2: act ───────────────────────────────────────────────────
        row2 = ctk.CTkFrame(bar, fg_color="transparent")
        row2.pack(fill="x")
        row2.grid_columnconfigure(1, weight=1)

        actions = ctk.CTkFrame(row2, fg_color="transparent")
        actions.grid(row=0, column=0, sticky="w", padx=18, pady=(0, 10))

        self.sync_btn = ctk.CTkButton(
            actions, text="Sync library", width=110, height=30, corner_radius=7,
            font=font(11, "bold"), fg_color=T.ACCENT2_DEEP, hover_color=T.BTN_HOV,
            text_color=T.ACCENT2, command=self._sync_clicked)
        self.sync_btn.pack(side="left", padx=(0, 8))

        self.fix_btn = ctk.CTkButton(
            actions, text="Fix missing", width=190, height=30, corner_radius=7,
            font=font(11, "bold"), fg_color=T.ACCENT_DEEP, hover_color=T.BTN_HOV,
            text_color=T.ACCENT, command=self._fill_missing)
        self.fix_btn.pack(side="left", padx=(0, 8))
        # Right-click for the expensive, occasional check: a full decode
        # pass looking for corrupt files, not just missing tags/thumbs.
        self.fix_btn.bind("<Button-3>", self._verify_menu)

        self.fetch_btn = ctk.CTkButton(
            actions, text="Fetch e621 tags", width=124, height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.ACCENT, command=lambda: self._fetch_tags(full=True))
        self.fetch_btn.pack(side="left")

        config = ctk.CTkFrame(row2, fg_color="transparent")
        config.grid(row=0, column=2, sticky="e", padx=16, pady=(0, 10))

        self.settings_btn = ctk.CTkButton(
            config, text="Settings", width=80, height=30, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.DIM, command=self._open_settings)
        self.settings_btn.pack(side="left", padx=(0, 8))

        self.help_btn = ctk.CTkButton(
            config, text="?", width=30, height=30, corner_radius=7,
            font=font(12, "bold"), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.FAINT, command=lambda: HelpWindow(self.root))
        self.help_btn.pack(side="left")

    SIDEBAR_W = 280

    def _build_sidebar(self):
        self.side = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0, width=self.SIDEBAR_W)
        self.side.grid(row=1, column=0, sticky="nsw", padx=(10, 0), pady=(10, 0))
        self.side.grid_propagate(False)
        self.side.grid_rowconfigure(1, weight=1)
        self.side.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self.side, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(2, 4))
        head.grid_columnconfigure(1, weight=1)
        self.side_toggle = ctk.CTkButton(
            head, text="◀", width=26, height=24, corner_radius=6,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.DIM, command=self.toggle_sidebar)
        self.side_toggle.grid(row=0, column=0, sticky="w", padx=(2, 6))
        self.side_title = ctk.CTkLabel(head, text="TAGS IN RESULTS", font=font(10, "bold"),
                                        text_color=T.FAINT, anchor="w")
        self.side_title.grid(row=0, column=1, sticky="w")
        self.side_hint = ctk.CTkLabel(head, text="click adds · right-click for more",
                                       font=font(9), text_color=T.FAINT, anchor="w")
        self.side_hint.grid(row=1, column=1, sticky="w")

        self.tagpanel = ctk.CTkScrollableFrame(
            self.side, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.ACCENT2_DEEP, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        self.tagpanel.grid(row=1, column=0, sticky="nsew", pady=(2, 10))
        self.tagpanel.grid_columnconfigure(0, weight=1)

        self.folders_label = ctk.CTkLabel(self.side, text="")

        if not self.cfg.sidebar_open:
            self.after(60, lambda: self.toggle_sidebar(force=False))

    def toggle_sidebar(self, force=None):
        opening = (not self.cfg.sidebar_open) if force is None else force
        self.cfg.sidebar_open = opening
        self.cfg.save()
        if opening:
            self.side.configure(width=self.SIDEBAR_W)
            self.tagpanel.grid()
            self.side_title.grid()
            self.side_hint.grid()
            self.side_toggle.configure(text="◀")
        else:
            self.tagpanel.grid_remove()
            self.side_title.grid_remove()
            self.side_hint.grid_remove()
            self.side.configure(width=34)
            self.side_toggle.configure(text="▶")
        self.after(120, self.render_page)

    def _build_grid_area(self):
        center = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        center.grid(row=1, column=1, sticky="nsew", padx=10, pady=(10, 0))
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(2, weight=1)

        self.chips = ctk.CTkFrame(center, fg_color="transparent", height=30)
        self.chips.grid(row=0, column=0, sticky="ew")

        info = ctk.CTkFrame(center, fg_color="transparent")
        info.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        info.grid_columnconfigure(0, weight=1)

        # Filter chips only, on the left - actions (Random, ratio) live on
        # the right with the pager instead, since they're things you DO,
        # not ways of narrowing what's shown.
        left = ctk.CTkFrame(info, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        self.count_label = ctk.CTkLabel(left, text="", font=font(10, mono=True),
                                         text_color=T.DIM, anchor="w")
        self.count_label.pack(side="left", padx=(2, 12))
        self.quick_chips = {}
        for key, text, token in (
                ("untagged", "Untagged", "is:untagged"),
                ("noid", "No post ID", "is:noid"),
                ("4k", "4K ✓", "is:4k"),
                ("no4k", "Non-4K", "is:no4k")):
            chip = ctk.CTkButton(left, text=text, height=22, width=92,
                                 corner_radius=11, font=font(9),
                                 fg_color=T.BTN, hover_color=T.BTN_HOV,
                                 text_color=T.DIM,
                                 command=lambda t=token: self.add_token(t))
            chip.pack(side="left", padx=(0, 6))
            self.quick_chips[key] = (chip, text)

        pager = ctk.CTkFrame(info, fg_color="transparent")
        pager.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(pager, text="🎲 Random", height=22, width=84,
                      corner_radius=11, font=font(9), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.ACCENT2,
                      command=self._random).pack(side="left", padx=(0, 6))
        self.ratio_btn = ctk.CTkButton(
            pager, text="Ratio ▾", height=22, width=76, corner_radius=11,
            font=font(9), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.ACCENT2, command=self._ratio_menu)
        self.ratio_btn.pack(side="left", padx=(0, 14))

        def pbtn(text, cmd):
            return ctk.CTkButton(pager, text=text, width=34, height=24,
                                 corner_radius=6, font=font(11),
                                 fg_color=T.BTN, hover_color=T.BTN_HOV,
                                 text_color=T.DIM, command=cmd)

        pbtn("◀", lambda: self.turn_page(-1)).pack(side="left", padx=(0, 4))
        self.page_label = ctk.CTkLabel(pager, text="", font=font(10, mono=True),
                                        text_color=T.FAINT)
        self.page_label.pack(side="left", padx=4)
        pbtn("▶", lambda: self.turn_page(1)).pack(side="left", padx=(4, 0))

        shell = ctk.CTkFrame(center, fg_color=T.SURFACE, corner_radius=12,
                             border_width=1, border_color=T.ACCENT2_DEEP)
        shell.grid(row=2, column=0, sticky="nsew")
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        self.gallery = tk.Canvas(shell, bg=T.SURFACE, highlightthickness=0, bd=0)
        self.gallery.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        self.gallery_bar = ctk.CTkScrollbar(shell, command=self.gallery.yview,
                                            width=12, button_color=T.LINE,
                                            button_hover_color=T.FAINT)
        self.gallery_bar.grid(row=0, column=1, sticky="ns", padx=(2, 4), pady=4)
        self.gallery.configure(yscrollcommand=self._on_scroll)

        self.gallery.bind("<Configure>", self._on_grid_resize)
        self.gallery.bind("<Leave>", lambda e: self._leave_grid())
        self.gallery.bind("<Button-1>", self._gal_background_click)
        self.gallery.bind("<MouseWheel>", self._on_wheel, add="+")
        self.gallery.bind("<Button-4>", self._on_wheel, add="+")
        self.gallery.bind("<Button-5>", self._on_wheel, add="+")
        self.gallery.bind("<Prior>", lambda e: self._wheel_steps(-8))
        self.gallery.bind("<Next>", lambda e: self._wheel_steps(8))

        prog = ctk.CTkFrame(center, fg_color="transparent")
        prog.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        prog.grid_columnconfigure(1, weight=1)
        self.status_label = ctk.CTkLabel(prog, text="", font=font(10),
                                          text_color=T.DIM, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="w", padx=2)
        self.progress = ctk.CTkProgressBar(prog, height=5, corner_radius=3,
                                            fg_color=T.LINE_SOFT, progress_color=T.ACCENT2)
        self.progress.grid(row=0, column=1, sticky="ew", padx=(10, 2))
        self.progress.set(0)

    def _pointer_in_gallery(self) -> bool:
        try:
            x, y = self.root.winfo_pointerxy()
            gx, gy = self.gallery.winfo_rootx(), self.gallery.winfo_rooty()
            return (gx <= x <= gx + self.gallery.winfo_width()
                   and gy <= y <= gy + self.gallery.winfo_height())
        except tk.TclError:
            return False

    def _on_wheel(self, event):
        if not self._pointer_in_gallery():
            return
        if getattr(event, "num", None) == 4:
            steps = -3
        elif getattr(event, "num", None) == 5:
            steps = 3
        else:
            delta = getattr(event, "delta", 0)
            steps = -int(delta / 120) * 3 if delta else 0
        if steps:
            self._wheel_steps(steps)
            return "break"

    def _wheel_steps(self, steps: int):
        self._peek_hide()
        try:
            first, last = self.gallery.yview()
        except tk.TclError:
            return
        if last - first >= 1.0:
            return
        self.gallery.yview_scroll(steps, "units")

    def _on_scroll(self, first, last):
        self.gallery_bar.set(first, last)

    def _ratio_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                       activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                       bd=0, font=(T.UI, 10))
        menu.add_command(label="All ratios", command=lambda: self._set_ratio(None))
        menu.add_command(label="📱 Portrait", command=lambda: self._set_ratio("is:portrait"))
        menu.add_command(label="🖥 Widescreen", command=lambda: self._set_ratio("is:widescreen"))
        menu.add_command(label="◻ Square", command=lambda: self._set_ratio("is:square"))
        try:
            x = self.ratio_btn.winfo_rootx()
            y = self.ratio_btn.winfo_rooty() + self.ratio_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _set_ratio(self, token: str | None):
        tokens = [t for t in self.search.get().split() if t not in RATIO_TOKENS]
        if token:
            tokens.append(token)
        self.search.delete(0, tk.END)
        self.search.insert(0, " ".join(tokens))
        self.run_search()

    def _gal_background_click(self, event):
        if not self.gallery.find_withtag("current"):
            self.selected = None
            self._restyle_cards()
            self._render_details()

    PANEL_MIN, PANEL_MAX = 452, 1500
    # Theater always widens the panel (and with it the player - see
    # _fit_panel) by at least this many pixels over whatever the normal
    # width computed to, so the toggle can never land on the same value
    # twice regardless of window size. A pure percentage-of-window share
    # can't guarantee that: at some widths both the normal and theater
    # shares round to the same PANEL_MIN/MAX clamp, and the button reads
    # as doing nothing.
    THEATER_BONUS = 240

    def panel_width(self) -> int:
        try:
            # winfo_width() can be one geometry pass behind a change that
            # just happened (e.g. the sidebar collapsing when theater
            # turns on) - forcing pending layout through first keeps the
            # two calls that matter here (this one, and the one theater
            # makes right after) reading the same, current number.
            self.root.update_idletasks()
            total = self.root.winfo_width()
        except tk.TclError:
            total = 1680
        if total < 400:
            total = 1680
        base = int(max(self.PANEL_MIN, min(total * 0.30, self.PANEL_MAX)))
        if not self.cfg.theater:
            return base
        theater = max(base + self.THEATER_BONUS, int(total * 0.46))
        return min(theater, self.PANEL_MAX)

    def _build_details(self):
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0, width=self.PANEL_MIN)
        self.detail_panel = panel
        panel.grid(row=1, column=2, sticky="nse", padx=(0, 12), pady=(10, 0))
        panel.grid_propagate(False)
        panel.grid_rowconfigure(3, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        head_row = ctk.CTkFrame(panel, fg_color="transparent")
        head_row.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 4))
        head_row.grid_columnconfigure(0, weight=1)
        self.detail_head = ctk.CTkLabel(head_row, text="SELECTED", font=font(10, "bold"),
                                         text_color=T.FAINT, anchor="w")
        self.detail_head.grid(row=0, column=0, sticky="w")
        self.theater_btn = ctk.CTkButton(
            head_row, text="⛶ Theater", width=90, height=24, corner_radius=6,
            font=font(10), fg_color=T.ACCENT_DEEP if self.cfg.theater else T.BTN,
            hover_color=T.BTN_HOV, text_color=T.ACCENT if self.cfg.theater else T.DIM,
            command=self.toggle_theater)
        self.theater_btn.grid(row=0, column=1, sticky="e")

        card = ctk.CTkFrame(panel, fg_color=T.SURFACE, corner_radius=12,
                            border_width=1, border_color=T.ACCENT2_DEEP)
        card.grid(row=1, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        self.player = InlinePlayer(card, self)
        self.player.frame.grid(row=0, column=0, padx=8, pady=(8, 4))
        self.after(200, self._fit_panel)

        self.detail_name = ctk.CTkLabel(card, text="Nothing selected", font=font(13, "bold"),
                                         text_color=T.TEXT, anchor="w",
                                         wraplength=410, justify="left")
        self.detail_name.grid(row=1, column=0, sticky="ew", padx=12)
        self.detail_meta = ctk.CTkLabel(card, text="", font=font(11, mono=True),
                                         text_color=T.DIM, anchor="w",
                                         wraplength=410, justify="left")
        self.detail_meta.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 6))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))

        def dbtn(text, cmd, color=T.DIM, width=70):
            b = ctk.CTkButton(buttons, text=text, width=width, height=26,
                              corner_radius=6, font=font(10), fg_color=T.BTN,
                              hover_color=T.BTN_HOV, text_color=color, command=cmd)
            b.pack(side="left", padx=(0, 5))
            return b

        dbtn("Folder", self._reveal)
        self.e621_open_btn = dbtn("e621", self._open_post, T.ACCENT2, 52)
        dbtn("Grid", self._grid, T.ACCENT, 56)
        dbtn("Copy name", lambda: self._copy(self.selected.name)
             if self.selected else None, T.DIM, 88)

        ctk.CTkLabel(panel, text="TAGS · click to search · right-click for more",
                     font=font(11, "bold"), text_color=T.FAINT, anchor="w"
                     ).grid(row=2, column=0, sticky="ew", padx=6, pady=(10, 4))

        self.detail_tags = ctk.CTkScrollableFrame(
            panel, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.LINE, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        self.detail_tags.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        self.detail_tags.grid_columnconfigure(0, weight=1)

    # ── brand ────────────────────────────────────────────────────────────

    def _apply_brand(self):
        self.brand_sub.configure(text=f"{self.F('tagline')} · Library")
        self.sync_btn.configure(text=self.F("sync"))
        self.fetch_btn.configure(text=self.F("fetch"))

    def _bind_local_keys(self):
        """Bindings that only ever make sense inside this tab's own widgets
        (the shared, tab-aware shortcuts are dispatched centrally by the
        app shell instead)."""
        self.bind("<FocusOut>", lambda e: self._leave_grid())

    @staticmethod
    def is_typing(event) -> bool:
        return isinstance(event.widget, (ctk.CTkEntry, tk.Entry, tk.Text))

    def key_play(self, event):
        if self.is_typing(event):
            return
        if self.selected:
            self._peek_hide()
            self.player.toggle()
        return "break"

    def key_space(self, event):
        if self.is_typing(event):
            return
        if self.selected:
            self.player.toggle()
        return "break"

    def key_seek(self, event, seconds: float):
        if self.is_typing(event):
            return
        if self.selected and (self.player.playing or self.player.position):
            self.player.nudge(seconds)
            return "break"

    def key_grid(self, event):
        if self.is_typing(event):
            return
        self._grid()
        return "break"

    def key_copy_name(self, event):
        if self.is_typing(event):
            return
        if self.selected:
            self._copy(self.selected.name)
        return "break"

    def key_copy_path(self, event):
        if self.is_typing(event):
            return
        if self.selected:
            self._copy(self.selected.path)
        return "break"

    def key_escape(self, event):
        if self.is_typing(event):
            return
        if self.player.playing:
            self.player.pause()
        elif self.selected:
            self.selected = None
            self._restyle_cards()
            self._render_details()
        return "break"

    def key_random(self, event):
        if self.is_typing(event):
            return
        self._random()
        return "break"

    def key_find_search(self, event):
        if isinstance(event.widget, (ctk.CTkEntry, tk.Entry, tk.Text)):
            return
        self.search.focus_set()
        return "break"

    def key_sync(self, event=None):
        self._sync_clicked()

    def key_full_rebuild(self, event=None):
        self._sync(full=True)

    def key_open_folders(self, event=None):
        self._open_folders()

    def key_toggle_sidebar(self, event=None):
        self.toggle_sidebar()

    def key_toggle_theater(self, event=None):
        self.toggle_theater()

    def key_page(self, delta):
        self.turn_page(delta)

    def on_app_close(self) -> bool:
        self._remember_state()
        self.cfg.sort = self.sort_menu.get()
        self.cfg.save()
        self.emeta.save()
        return True

    def set_status(self, text: str, colour: str = T.DIM):
        self.status_label.configure(text=text, text_color=colour)

    # ── which folders are indexed ───────────────────────────────────────────

    def library_dirs(self) -> list:
        root_dir = self.cfg.effective_library_root()
        if not root_dir or not os.path.isdir(root_dir):
            return []
        subfolders = self.cfg.effective_library_subfolders()
        if not subfolders:
            return [root_dir]
        return [os.path.join(root_dir, name) for name in subfolders
                if os.path.isdir(os.path.join(root_dir, name))]

    def _folders_summary(self) -> str:
        dirs = self.library_dirs()
        root_dir = self.cfg.effective_library_root()
        if not dirs:
            return "No folders selected" if not root_dir else "No matching subfolders"
        root = os.path.basename(root_dir.rstrip("\\/")) if root_dir else "?"
        subfolders = self.cfg.effective_library_subfolders()
        if subfolders:
            return f"{root} · {', '.join(subfolders)}"
        return f"{root} · all subfolders"

    def _open_folders(self):
        FoldersWindow(self.root, self)

    def _open_settings(self):
        self.app.open_settings(initial_tab="Library")

    def _folders_saved(self, changed: bool):
        self.cfg.save()
        self.folders_label.configure(text=self._folders_summary())
        if changed:
            self.set_status("Folder list changed - press "
                            f"“{self.F('sync')}” to re-index.", T.WARN)

    def after_settings_saved(self):
        self._apply_brand()
        self._fit_panel()
        self.theater_btn.configure(
            fg_color=T.ACCENT_DEEP if self.cfg.theater else T.BTN,
            text_color=T.ACCENT if self.cfg.theater else T.DIM)
        self.render_page()
        self._render_details()
        self._refresh_missing_badge()
        self.set_status("Settings saved", T.OK)

    # ── library loading ─────────────────────────────────────────────────────

    def _refresh_missing_badge(self):
        """
        The badge used to just show a bare total, e.g. "(2)" - a number
        that maps to nothing else on screen, since it sums three unrelated
        counts (untagged / unprobed / unthumbnailed). That's what made a
        stuck "(2)" look like a bug even when it wasn't: the Untagged chip
        only reflects the tags part, so the two numbers can legitimately
        disagree. Showing the actual breakdown on the button removes the
        guessing.
        """
        report = self.missing_report()
        n_tags = len(report["tags"])
        n_probe = len(report["probe"])
        n_thumb = len(report["thumbs"])
        outstanding = n_tags + n_probe + n_thumb
        if outstanding:
            parts = []
            if n_tags:
                parts.append(f"{n_tags} untagged")
            if n_probe:
                parts.append(f"{n_probe} detail{'s' if n_probe != 1 else ''}")
            if n_thumb:
                parts.append(f"{n_thumb} thumb{'s' if n_thumb != 1 else ''}")
            self._fix_breakdown = " · ".join(parts)
            label = self._fix_breakdown if len(parts) == 1 else f"{outstanding} missing"
            self.fix_btn.configure(text=f"Fix missing: {label}",
                                    fg_color=T.ACCENT_DEEP, text_color=T.ACCENT)
        else:
            self.fix_btn.configure(text="Nothing missing", fg_color=T.BTN, text_color=T.FAINT)
            self._fix_breakdown = ""
        self._refresh_quick_counts()

    def _refresh_quick_counts(self):
        if not getattr(self, "quick_chips", None) or not self.records:
            return
        counts = {
            "untagged": sum(1 for r in self.records if not r.tags),
            "noid": sum(1 for r in self.records if not r.pid),
            "4k": sum(1 for r in self.records if r.premium),
            "no4k": sum(1 for r in self.records if not r.premium),
            "portrait": sum(1 for r in self.records if r.orientation == "portrait"),
            "widescreen": sum(1 for r in self.records if r.orientation == "widescreen"),
            "square": sum(1 for r in self.records if r.orientation == "square"),
        }
        for key, (chip, label) in self.quick_chips.items():
            count = counts.get(key)
            if count is None:
                continue
            chip.configure(text=f"{label} ({count})")
            if key in ("untagged", "noid", "portrait", "widescreen", "square") and count == 0:
                chip.configure(text_color=T.FAINT, state="disabled")
            else:
                chip.configure(text_color=T.DIM, state="normal")

    def _load_library(self):
        conn = db_connect()
        rows = conn.execute(
            "SELECT path,name,folder,pid,size,mtime,duration,width,height,fps "
            "FROM files").fetchall()
        vault_marks = vault_marks_by_path(conn)
        self._project_colors = {name: color for name, color, _n, _t
                                in vault_projects_list(conn)}
        conn.close()
        self.records = []
        self.by_path = {}
        self.tag_universe = set()

        premium: dict = {}
        if self.cfg.premium_root and os.path.isdir(self.cfg.premium_root):
            try:
                for name in os.listdir(self.cfg.premium_root):
                    sub = os.path.join(self.cfg.premium_root, name)
                    if os.path.isdir(sub) and not is_ignored_dir(name):
                        try:
                            with os.scandir(sub) as entries:
                                premium[name] = {
                                    e.name: e.path for e in entries if e.is_file()
                                    and os.path.splitext(e.name)[1].lower() == ".mp4"}
                        except OSError:
                            pass
            except OSError:
                pass

        for row in rows:
            rec = Rec(*row)
            meta = self.emeta.get(rec.pid) if rec.pid else None
            if meta and not meta.get("missing"):
                rec.artists = list(meta.get("artist") or [])
                rec.characters = list(meta.get("character") or [])
                rec.species = list(meta.get("species") or [])
                rec.copyrights = list(meta.get("copyright") or [])
                rec.lore = list(meta.get("lore") or [])
                rec.rating = meta.get("rating") or ""
                rec.score = meta.get("score") or 0
                rec.tags = set((meta.get("tags") or "").split())
                rec.url = meta.get("url") or ""
                self.tag_universe |= rec.tags
                rec.compute_named()
            alt_path = premium.get(rec.folder, {}).get(rec.name)
            rec.premium = rec.height >= 2000 or alt_path is not None
            rec.premium_path = alt_path or ""
            marks = vault_marks.get(rec.path)
            if marks:
                rec.used_projects = [project for project, _color, _t in marks]
                rec.used_color = marks[0][1]   # most-recent mark, per vault_marks_by_path
            self.records.append(rec)
            self.by_path[rec.path] = rec
        if hasattr(self, "fix_btn"):
            self._refresh_missing_badge()
        # The identity bar carries one live number for the whole suite, so
        # the size of the library is visible from any tab, not just this one.
        setter = getattr(self.app, "set_header_status", None)
        if setter:
            setter(f"{len(self.records):,} clips indexed", T.OK)

    # ── search & render ─────────────────────────────────────────────────────

    def _commit_search(self, _event=None):
        query = self.search.get().strip()
        if query:
            history = [q for q in self.cfg.search_history if q != query]
            history.insert(0, query)
            self.cfg.search_history = history[:30]
            self.cfg.save()
        self._history_pos = -1
        self.run_search()
        return "break"

    def _history_step(self, delta: int):
        history = self.cfg.search_history
        if not history:
            return "break"
        self._history_pos = max(-1, min(self._history_pos + delta, len(history) - 1))
        self.search.delete(0, tk.END)
        if self._history_pos >= 0:
            self.search.insert(0, history[self._history_pos])
        self.run_search()
        return "break"

    def _show_history(self):
        history = self.cfg.search_history
        if not history:
            self.set_status("No previous searches yet.", T.DIM)
            return
        menu = tk.Menu(self.root, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                       activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                       bd=0, font=(T.UI, 10))
        for query in history[:20]:
            menu.add_command(label=query, command=lambda q=query: self._apply_search(q))
        menu.add_separator()
        menu.add_command(label="Clear history", command=self._clear_history)
        try:
            x = self.search.winfo_rootx()
            y = self.search.winfo_rooty() + self.search.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _apply_search(self, query: str):
        self.search.delete(0, tk.END)
        self.search.insert(0, query)
        self.run_search()

    def _clear_search(self):
        self.search.delete(0, tk.END)
        self._history_pos = -1
        self.run_search()
        return "break"

    def _clear_history(self):
        self.cfg.search_history = []
        self.cfg.save()
        self.set_status("Search history cleared", T.OK)

    def _on_search_key(self, _event=None):
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except ValueError:
                pass
        self._search_after = self.after(250, self.run_search)

    def _restore_state(self):
        if self.cfg.last_sort in SORTS:
            self.sort_menu.set(self.cfg.last_sort)
        if self.cfg.last_rating in ("All", "S", "Q", "E"):
            self.rating_seg.set(self.cfg.last_rating)
        if self.cfg.last_search:
            self.search.delete(0, tk.END)
            self.search.insert(0, self.cfg.last_search)

    def _remember_state(self):
        query = self.search.get().strip()
        sort = self.sort_menu.get()
        rating = self.rating_seg.get()
        if (query, sort, rating) == (self.cfg.last_search, self.cfg.last_sort,
                                     self.cfg.last_rating):
            return
        self.cfg.last_search = query
        self.cfg.last_sort = sort
        self.cfg.last_rating = rating
        self.cfg.save()

    def run_search(self):
        self._search_after = None
        query = self.search.get().strip()
        includes, excludes = parse_query(query)
        rating = self.rating_seg.get()
        if rating in ("S", "Q", "E"):
            includes.append(("rating", rating.lower()))
        self.filtered = [r for r in self.records if rec_matches(r, includes, excludes)]
        key = SORTS.get(self.sort_menu.get(), SORTS["Newest"])
        self.filtered.sort(key=key)
        self.page = 0
        self._remember_state()
        self._render_chips(query)
        self._render_tagpanel()
        self.render_page()

    def _render_chips(self, query: str):
        for child in self.chips.winfo_children():
            child.destroy()
        tokens = query.split()
        for token in tokens[:12]:
            negative = token.startswith("-")
            chip = ctk.CTkButton(
                self.chips, text=f"{token}  ✕", height=22, corner_radius=11,
                font=font(9), width=10,
                fg_color=T.FAIL_DEEP if negative else T.ACCENT_DEEP,
                hover_color=T.BTN_HOV, text_color=T.FAIL if negative else T.ACCENT,
                command=lambda t=token: self._remove_token(t))
            chip.pack(side="left", padx=(0, 5), pady=2)

    def _remove_token(self, token: str):
        tokens = [t for t in self.search.get().split() if t != token]
        self.search.delete(0, tk.END)
        self.search.insert(0, " ".join(tokens))
        self.run_search()

    def add_token(self, token: str, negative: bool = False):
        if token == "sort:score":
            self.sort_menu.set("Score")
            self.run_search()
            return
        token = ("-" if negative else "") + token
        current = self.search.get()
        # A quoted token (used:"...") can hold a space, so a plain
        # whitespace split can't reliably detect "already there" - a
        # substring check is enough for that, without needing the full
        # shlex-aware parser just to de-duplicate.
        if token in current.split() or token in current:
            return
        text = (current + " " + token).strip() if current.strip() else token
        self.search.delete(0, tk.END)
        self.search.insert(0, text)
        self.run_search()

    def hide_tag(self, name: str):
        if name not in self.cfg.hidden_tags:
            self.cfg.hidden_tags.append(name)
            self.cfg.save()
            self._render_tagpanel()
            self.set_status(f"'{name}' hidden from the sidebar (not deleted "
                            "- manage at the bottom of the tag list)", T.OK)

    def unhide_tag(self, name: str):
        if name in self.cfg.hidden_tags:
            self.cfg.hidden_tags.remove(name)
            self.cfg.save()
            self._render_tagpanel()

    def _manage_hidden(self):
        HiddenTagsWindow(self.root, self)

    def _toggle_sidebar_group(self, key: str):
        self.cfg.sidebar_group_open[key] = not self.cfg.sidebar_group_open.get(key, True)
        self.cfg.save()
        self._render_tagpanel()

    def _render_tagpanel(self):
        for child in self.tagpanel.winfo_children():
            child.destroy()
        artists = collections.Counter()
        characters = collections.Counter()
        species = collections.Counter()
        series = collections.Counter()
        lore = collections.Counter()
        other = collections.Counter()
        projects = collections.Counter()
        for rec in self.filtered:
            artists.update(rec.artists)
            characters.update(rec.characters)
            species.update(rec.species)
            series.update(rec.copyrights)
            lore.update(rec.lore)
            other.update(t for t in rec.tags if t not in rec.named)
            projects.update(rec.used_projects)

        hidden = set(self.cfg.hidden_tags)
        row = 0
        groups = (("ARTISTS", artists, "artist:", T.ACCENT2),
                 ("CHARACTERS", characters, "character:", T.ACCENT),
                 ("SPECIES", species, "species:", T.OK),
                 ("SERIES", series, "copyright:", T.WARN),
                 ("LORE", lore, "lore:", T.ACCENT2_HOV),
                 ("TAGS", other, "", T.DIM))
        for title, counter, prefix, colour in groups:
            visible = [(n, c) for n, c in counter.most_common(60) if n not in hidden][:24]
            if not visible:
                continue
            key = title.lower()
            open_now = self.cfg.sidebar_group_open.get(key, True)
            header = ctk.CTkButton(
                self.tagpanel,
                text=("▾  " if open_now else "▸  ") + f"{title}   {len(visible)}",
                height=26, corner_radius=6, font=font(10, "bold"), anchor="w",
                fg_color=T.ELEVATED, hover_color=T.BTN_HOV, text_color=T.FAINT,
                command=lambda k=key: self._toggle_sidebar_group(k))
            header.grid(row=row, column=0, sticky="ew", padx=8,
                        pady=(10 if row else 6, 2))
            row += 1
            if not open_now:
                continue
            for name, count in visible:
                token = prefix + name
                label = ctk.CTkButton(
                    self.tagpanel, text=f"{name}   {count}", height=27,
                    corner_radius=5, font=font(12), anchor="w",
                    fg_color="transparent", hover_color=T.BTN_HOV, text_color=colour,
                    command=lambda t=token: self.add_token(t))
                label.grid(row=row, column=0, sticky="ew", padx=6, pady=1)
                label.bind("<Button-3>", lambda e, t=token, n=name: self._tag_menu(e, t, n))
                row += 1

        # PROJECTS gets its own block instead of the loop above - project
        # names are free text (can hold spaces), so the search token needs
        # quoting, and each one gets its own Vault-assigned colour rather
        # than one fixed colour for the whole group.
        if projects:
            key = "projects"
            open_now = self.cfg.sidebar_group_open.get(key, True)
            header = ctk.CTkButton(
                self.tagpanel,
                text=("▾  " if open_now else "▸  ") + f"PROJECTS   {len(projects)}",
                height=26, corner_radius=6, font=font(10, "bold"), anchor="w",
                fg_color=T.ELEVATED, hover_color=T.BTN_HOV, text_color=T.FAINT,
                command=lambda k=key: self._toggle_sidebar_group(k))
            header.grid(row=row, column=0, sticky="ew", padx=8,
                        pady=(10 if row else 6, 2))
            row += 1
            if open_now:
                for name, count in projects.most_common(60):
                    colour = self._project_colors.get(name, T.DIM)
                    label = ctk.CTkButton(
                        self.tagpanel, text=f"{name}   {count}", height=27,
                        corner_radius=5, font=font(12), anchor="w",
                        fg_color="transparent", hover_color=T.BTN_HOV, text_color=colour,
                        command=lambda n=name: self.add_token(f'used:"{n}"'))
                    label.grid(row=row, column=0, sticky="ew", padx=6, pady=1)
                    row += 1
        if hidden:
            ctk.CTkButton(self.tagpanel,
                         text=f"{len(hidden)} hidden tag{'s' if len(hidden) != 1 else ''} "
                              f"· manage",
                         height=24, corner_radius=5, font=font(9),
                         fg_color="transparent", hover_color=T.BTN_HOV,
                         text_color=T.FAINT, command=self._manage_hidden
                         ).grid(row=row, column=0, sticky="ew", padx=8, pady=(10, 6))

    # ── gallery ─────────────────────────────────────────────────────────────

    CARD_W, IMG_H = 224, 126
    CAP_H, GAP = 42, 10

    @property
    def card_width(self) -> int:
        return max(120, min(int(self.cfg.card_width), 480))

    def _leave_grid(self, _event=None):
        self._set_hover(None)
        self._peek_hide()

    def _on_grid_resize(self, event):
        columns = max(2, (event.width - self.GAP) // (self.card_width + self.GAP))
        if columns == getattr(self, "_columns", 0):
            return
        if self._resize_after is not None:
            try:
                self.after_cancel(self._resize_after)
            except ValueError:
                pass
        self._resize_after = self.after(180, self.render_page)

    def _grid(self):
        rec = self.selected
        if not rec or not os.path.exists(rec.path):
            self.set_status("Select a clip first for its contact sheet.", T.DIM)
            return
        ContactSheet(self.root, self.frames, rec.path, rec.name, on_jump=self._grid_jump)

    def _grid_jump(self, moment: float) -> None:
        if not self.selected:
            return
        if self.player.playing:
            self.player.engine.seek(moment)
        else:
            self.player.engine.position = moment
            self.player.play()
        self.player._draw_bar()

    def _random(self):
        if not self.filtered:
            return
        import random
        rec = random.choice(self.filtered)
        index = self.filtered.index(rec)
        self.page = index // self.cfg.page_size
        self.selected = rec
        self.render_page()
        self._render_details()
        self.set_status(f"Random pick: {rec.name}", T.ACCENT2)

    def turn_page(self, delta: int):
        pages = max((len(self.filtered) + self.cfg.page_size - 1) // self.cfg.page_size, 1)
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:
            self.page = new
            self.render_page()

    def _ellipsize(self, text: str, max_px: int) -> str:
        if self._card_font.measure(text) <= max_px:
            return text
        while text and self._card_font.measure(text + "…") > max_px:
            text = text[:-1]
        return text + "…"

    def render_page(self):
        self._resize_after = None
        self._page_token += 1
        token = self._page_token
        self._page_refs = []
        self._layout = []
        self._hover_index = None
        self._peek_hide()

        canvas = self.gallery
        canvas.delete("all")
        canvas.yview_moveto(0)

        total = len(self.filtered)
        pages = max((total + self.cfg.page_size - 1) // self.cfg.page_size, 1)
        self.page = min(self.page, pages - 1)
        start = self.page * self.cfg.page_size
        batch = self.filtered[start:start + self.cfg.page_size]

        self.page_label.configure(text=f"page {self.page + 1}/{pages}")
        self.count_label.configure(
            text=f"{total} clips · {fmt_size(sum(r.size for r in self.filtered))}"
                 f" · {fmt_len(sum(r.duration for r in self.filtered))} of footage")

        base = self.card_width
        width = max(canvas.winfo_width(), base + 2 * self.GAP)
        columns = max(2, (width - self.GAP) // (base + self.GAP))
        self._columns = columns
        usable = width - self.GAP * (columns + 1)
        self.CARD_W = max(int(usable // columns), 120)
        self.IMG_H = int(self.CARD_W * 9 / 16)
        margin = self.GAP

        if not self.records:
            canvas.create_text(24, 28, text=self.F("empty_db"), fill=T.FAINT,
                               font=(T.UI, 12), anchor="nw", width=width - 60)
            return
        if not batch:
            canvas.create_text(24, 28, text=self.F("no_results"), fill=T.FAINT,
                               font=(T.UI, 12), anchor="nw")
            return

        cell_h = self.IMG_H + self.CAP_H + self.GAP
        self._cell_h = cell_h
        for index, rec in enumerate(batch):
            col, row = index % columns, index // columns
            x = margin + col * (self.CARD_W + self.GAP)
            y = self.GAP + row * cell_h
            self._draw_card(index, rec, x, y)

        rows = (len(batch) + columns - 1) // columns
        content = rows * cell_h + self.GAP
        visible = max(canvas.winfo_height(), 1)
        canvas.configure(scrollregion=(0, 0, width, max(content, visible)))
        canvas.yview_moveto(0)
        threading.Thread(target=self._load_thumbs, args=(list(batch), token), daemon=True).start()

    def _draw_card(self, index: int, rec: Rec, x: int, y: int):
        canvas = self.gallery
        tag = f"cd{index}"

        canvas.create_rectangle(x, y, x + self.CARD_W, y + self.IMG_H,
                                fill=T.INPUT, outline="", tags=(tag, "well"))
        canvas.create_text(x + self.CARD_W // 2, y + self.IMG_H // 2, text="…",
                           fill=T.FAINT, font=(T.UI, 11), tags=(tag, f"ph{index}"))
        if rec.used_projects:
            # A clip already spent on a project stays visibly marked in the
            # gallery - the colour is that project's own, so different
            # sessions read as different marks instead of one flat "used".
            canvas.create_rectangle(x + 1, y + 1, x + self.CARD_W - 1, y + self.IMG_H - 1,
                                    outline=rec.used_color, width=3,
                                    tags=(tag, f"used{index}"))

        canvas.create_rectangle(x, y + self.IMG_H + 2, x + self.CARD_W, y + self.IMG_H + 5,
                                outline="", fill=self._bar_colour(rec, hover=False),
                                tags=(tag, f"bar{index}"))

        tx = x + 2
        if rec.rating:
            canvas.create_text(tx, y + self.IMG_H + 15, text="●",
                               fill=T.RATING.get(rec.rating, T.DIM), font=(T.UI, 7),
                               anchor="w", tags=(tag,))
            tx += 12
        title = rec.artists[0] if rec.artists else (rec.pid or os.path.splitext(rec.name)[0])
        canvas.create_text(tx, y + self.IMG_H + 15,
                           text=self._ellipsize(title, x + self.CARD_W - tx - 4),
                           fill=T.TEXT, font=(T.UI, 10), anchor="w", tags=(tag, f"tt{index}"))

        spec = f"{rec.height}p" if rec.height else "--"
        if rec.fps:
            spec += f" · {rec.fps:.0f}fps"
        sx = x + 2
        canvas.create_text(sx, y + self.IMG_H + 31, text=spec, fill=T.FAINT,
                           font=(T.MONO, 9), anchor="w", tags=(tag,))
        score = fmt_score(rec.score)
        if score:
            sx += self._spec_font.measure(spec) + 8
            canvas.create_text(sx, y + self.IMG_H + 31, text=f"▲{score}", fill=T.ACCENT2,
                               font=(T.MONO, 9), anchor="w", tags=(tag,))
        if rec.premium:
            canvas.create_text(x + self.CARD_W - 2, y + self.IMG_H + 31, text="4K ✓",
                               fill=T.OK, font=(T.MONO, 9, "bold"), anchor="e", tags=(tag,))

        self._layout.append({"rec": rec, "x": x, "y": y, "tag": tag})

        canvas.tag_bind(tag, "<Button-1>", lambda e, r=rec: self._select(r))
        canvas.tag_bind(tag, "<Double-Button-1>", lambda e, r=rec: self._select_and_play(r))
        canvas.tag_bind(tag, "<Button-3>", lambda e, r=rec: self._card_menu(e, r))
        canvas.tag_bind(tag, "<Enter>", lambda e, i=index: self._set_hover(i))
        canvas.tag_bind(tag, "<Leave>", lambda e, i=index: self._unhover(i))
        canvas.tag_bind(tag, "<Motion>", lambda e, i=index: self._card_hover(e, i))

    def _bar_colour(self, rec: Rec, hover: bool) -> str:
        if self.selected and self.selected.path == rec.path:
            return T.ACCENT
        if hover:
            return T.ACCENT_HOV
        return T.LINE_SOFT

    def _restyle_cards(self):
        for index, slot in enumerate(self._layout):
            self.gallery.itemconfigure(
                f"bar{index}",
                fill=self._bar_colour(slot["rec"], hover=(index == self._hover_index)))

    def _set_hover(self, index):
        if index == self._hover_index:
            return
        self._hover_index = index
        self._restyle_cards()

    def _unhover(self, index):
        if self._hover_index == index:
            self._hover_index = None
            self._restyle_cards()
        self._peek_hide()

    def _load_thumbs(self, batch: list, token: int):
        for index, rec in enumerate(batch):
            if token != self._page_token:
                return
            data = None
            try:
                with open(os.path.join(THUMB_DIR, thumb_key(rec.path)), "rb") as fh:
                    data = fh.read()
            except OSError:
                pass
            self.ui(self._place_thumb, index, rec, data, token)

    def _place_thumb(self, index: int, rec: Rec, data, token: int):
        if token != self._page_token or index >= len(self._layout):
            return
        slot = self._layout[index]
        canvas = self.gallery
        canvas.delete(f"ph{index}")
        if not data:
            canvas.create_text(slot["x"] + self.CARD_W // 2, slot["y"] + self.IMG_H // 2,
                               text="no thumb", fill=T.FAINT, font=(T.UI, 9),
                               tags=(slot["tag"],))
            return
        try:
            image = Image.open(io.BytesIO(data))
            image = fit_frame(image, self.CARD_W, self.IMG_H, self.cfg.thumb_fit)
            image = round_corners(image, 9, T.SURFACE)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return
        self._page_refs.append(photo)
        canvas.create_image(slot["x"], slot["y"], image=photo, anchor="nw",
                            tags=(slot["tag"], f"im{index}"))
        text = fmt_len(rec.duration)
        bx = slot["x"] + self.CARD_W - 5
        by = slot["y"] + self.IMG_H - 5
        pad = 4
        w = self._badge_font.measure(text)
        canvas.create_rectangle(bx - w - pad * 2, by - 15, bx, by, fill=T.BG, outline="",
                                tags=(slot["tag"],))
        canvas.create_text(bx - pad, by - 2, text=text, fill=T.TEXT, font=(T.MONO, 8),
                           anchor="se", tags=(slot["tag"],))
        canvas.tag_raise(f"bar{index}")
        if rec.used_projects:
            canvas.tag_raise(f"used{index}")

    # ── hover scrub ─────────────────────────────────────────────────────────

    def _card_hover(self, event, index: int):
        if index >= len(self._layout):
            return
        slot = self._layout[index]
        rec = slot["rec"]
        if rec.duration <= 0:
            return
        y = self.gallery.canvasy(event.y)
        if y > slot["y"] + self.IMG_H:
            self._peek_hide()
            return
        if self._peek_after is not None:
            try:
                self.after_cancel(self._peek_after)
            except ValueError:
                pass
        x = self.gallery.canvasx(event.x)
        frac = max(0.0, min((x - slot["x"]) / self.CARD_W, 1.0))
        self._peek_path = rec.path
        self._peek_after = self.after(
            100, lambda: self._peek_fetch(rec, frac, event.x_root, event.y_root))

    def _peek_fetch(self, rec: Rec, frac: float, x_root: int, y_root: int):
        self._peek_after = None
        if self._peek_path != rec.path:
            return
        moment = max(0.0, min(frac * rec.duration, rec.duration - 0.05))
        self._peek_token += 1
        token = self._peek_token
        request = (rec, moment, token, x_root, y_root)
        if self._peek_busy:
            # One extraction in flight at a time - piling up an overlapping
            # ffmpeg call per debounce tick on fast mouse movement is what
            # made the preview lag behind the cursor. Only the latest hover
            # position matters, so it replaces whatever was pending.
            self._peek_pending = request
            return
        self._peek_busy = True
        self._peek_run(request)

    def _peek_run(self, request) -> None:
        rec, moment, token, x_root, y_root = request
        frac = (moment / rec.duration) if rec.duration else 0.0

        def work():
            # hover_frame() crops a pre-built sprite sheet instead of
            # spawning ffmpeg per hover - see media.py for why. The sprite
            # itself still needs one ffmpeg pass the first time a clip is
            # hovered, hence still doing this off the UI thread. Cell width
            # stays at the cache's own default rather than matching the
            # preview bubble 1:1 - keeping the sprite (and the handful kept
            # in memory) small matters more than a perfectly crisp hover
            # thumbnail here.
            data = self.frames.hover_frame(rec.path, rec.duration, frac)
            self.ui(self._peek_done, rec, data, moment, token, x_root, y_root)

        threading.Thread(target=work, daemon=True).start()

    def _peek_done(self, rec: Rec, data, moment, token, x_root, y_root) -> None:
        self._peek_busy = False
        if token == self._peek_token:
            self._peek_show(rec, data, moment, token, x_root, y_root)
        pending = self._peek_pending
        self._peek_pending = None
        if pending is not None:
            self._peek_busy = True
            self._peek_run(pending)

    def _peek_show(self, rec: Rec, data, moment, token, x_root, y_root):
        if token != self._peek_token or self._peek_path != rec.path:
            return
        title = rec.pid or rec.name
        if rec.artists:
            title = f"{rec.artists[0]} · #{rec.pid}"
        fraction = (moment / rec.duration) if rec.duration else None
        self.peek.show_frame(data, title, fmt_clock(moment), x_root, y_root,
                             fraction=fraction)

    def _peek_hide(self):
        self._peek_path = None
        self._peek_token += 1
        self._peek_pending = None
        if self._peek_after is not None:
            try:
                self.after_cancel(self._peek_after)
            except ValueError:
                pass
            self._peek_after = None
        self.peek.hide()

    # ── selection & details ─────────────────────────────────────────────────

    def _select(self, rec: Rec):
        self.selected = rec
        self._restyle_cards()
        self._render_details()

    def _select_and_play(self, rec: Rec):
        self._select(rec)
        self._peek_hide()
        self.player.play()

    def _fit_panel(self, _event=None):
        width = self.panel_width()
        try:
            self.detail_panel.configure(width=width)
        except tk.TclError:
            return
        inner = width - 26
        self.player.set_size(inner)
        for label in (self.detail_name, self.detail_meta):
            label.configure(wraplength=inner - 10)

    def on_root_resize(self):
        if getattr(self, "_panel_job", None):
            try:
                self.after_cancel(self._panel_job)
            except ValueError:
                pass
        self._panel_job = self.after(220, self._fit_panel)

    def toggle_theater(self):
        self.cfg.theater = not self.cfg.theater
        self.cfg.save()
        if self.cfg.theater and self.cfg.sidebar_open:
            self.toggle_sidebar(force=False)
        self.theater_btn.configure(
            fg_color=T.ACCENT_DEEP if self.cfg.theater else T.BTN,
            text_color=T.ACCENT if self.cfg.theater else T.DIM)
        self._fit_panel()
        self.after(150, self.render_page)

    def _render_details(self):
        for child in self.detail_tags.winfo_children():
            child.destroy()
        rec = self.selected
        self.player.show_rec(rec)
        if not rec:
            self.detail_name.configure(text="Nothing selected")
            self.detail_meta.configure(text="")
            return

        self.detail_name.configure(text=rec.name)
        bits = [f"{rec.width}x{rec.height}", f"{rec.fps:.0f} fps",
               fmt_len(rec.duration), fmt_size(rec.size), rec.folder]
        if rec.score:
            bits.insert(2, f"▲ {rec.score} score")
        if rec.premium:
            bits.append("4K available ✓")
        if rec.pid:
            bits.append(f"#{rec.pid}")
        if rec.used_projects:
            bits.append("used: " + ", ".join(rec.used_projects))
        self.detail_meta.configure(text="  ·  ".join(bits))

        groups = [
            ("Artists", "artist:", rec.artists, T.ACCENT2),
            ("Characters", "character:", rec.characters, T.ACCENT),
            ("Species", "species:", rec.species, T.OK),
            ("Series", "copyright:", rec.copyrights, T.WARN),
            ("Lore", "lore:", rec.lore, T.ACCENT2_HOV),
            ("Tags", "", sorted(rec.tags - rec.named), T.DIM),
        ]

        row = 0
        any_content = False
        for title, prefix, names, colour in groups:
            if not names:
                continue
            any_content = True
            row = self._detail_group(title, prefix, names, colour, row)

        if not any_content:
            ctk.CTkLabel(self.detail_tags,
                        text="No tags for this clip yet - press "
                             "“Fix missing” up top.",
                        font=font(11), text_color=T.FAINT, wraplength=380,
                        justify="left").grid(row=0, column=0, columnspan=2,
                                             padx=10, pady=10, sticky="w")

    def _detail_group(self, title: str, prefix: str, names: list, colour: str, row: int) -> int:
        key = title.lower()
        open_now = self.cfg.detail_open.get(key, True)
        header = ctk.CTkButton(
            self.detail_tags,
            text=("▾  " if open_now else "▸  ") + f"{title.upper()}   {len(names)}",
            height=26, corner_radius=6, font=font(10, "bold"), anchor="w",
            fg_color=T.ELEVATED, hover_color=T.BTN_HOV, text_color=T.FAINT,
            command=lambda k=key: self._toggle_group(k))
        header.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6,
                   pady=(8 if row else 4, 2))
        row += 1
        if not open_now:
            return row

        two_up = len(names) > 6 and prefix == ""
        for index, name in enumerate(names[:160]):
            token = prefix + name
            if two_up:
                self._tag_button(token, name, colour, row + index // 2, column=index % 2)
            else:
                self._tag_button(token, name, colour, row + index, column=0, span=2)
        row += ((len(names[:160]) + 1) // 2) if two_up else len(names[:160])
        return row

    def _toggle_group(self, key: str):
        self.cfg.detail_open[key] = not self.cfg.detail_open.get(key, True)
        self.cfg.save()
        self._render_details()

    def _tag_button(self, token: str, label: str, colour: str, row: int,
                    column: int = 0, span: int = 1):
        button = ctk.CTkButton(
            self.detail_tags, text=label, height=25, corner_radius=5,
            font=font(12), anchor="w", fg_color="transparent",
            hover_color=T.BTN_HOV, text_color=colour,
            command=lambda t=token: self.add_token(t))
        button.grid(row=row, column=column, columnspan=span, sticky="ew", padx=6, pady=1)
        button.bind("<Button-3>", lambda e, t=token: self._tag_menu(e, t, label))

    def _tag_menu(self, event, token: str, name: str):
        menu = tk.Menu(self.root, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                       activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                       bd=0, font=(T.UI, 10))
        menu.add_command(label=f"Search {name}", command=lambda: self.add_token(token))
        menu.add_command(label=f"Exclude -{name}",
                         command=lambda: self.add_token(token, negative=True))
        menu.add_separator()
        menu.add_command(label="Hide from sidebar", command=lambda: self.hide_tag(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _card_menu(self, event, rec: Rec):
        self._select(rec)
        menu = tk.Menu(self.root, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                       activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                       bd=0, font=(T.UI, 10))
        menu.add_command(label="Play here", command=lambda: self._select_and_play(rec))
        menu.add_command(label="Open externally", command=lambda: open_file(rec.path))
        menu.add_command(label="Show in folder", command=lambda: open_in_explorer(rec.path))
        if rec.pid:
            menu.add_command(label=f"Open e621 post #{rec.pid}",
                             command=lambda: self._open_url(rec))
        menu.add_separator()

        used_menu = tk.Menu(menu, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                            activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                            bd=0, font=(T.UI, 10))
        conn = db_connect()
        try:
            projects = vault_projects_list(conn)
        finally:
            conn.close()
        for name, _color, _count, _created in projects:
            if name in rec.used_projects:
                continue
            used_menu.add_command(label=name, command=lambda n=name: self._mark_used(rec, n))
        used_menu.add_command(label="New project…", command=lambda: self._mark_used_new(rec))
        menu.add_cascade(label="Mark as used…", menu=used_menu)
        for name in rec.used_projects:
            menu.add_command(label=f"Remove from '{name}'",
                             command=lambda n=name: self._unmark_used(rec, n))
        menu.add_separator()
        copy_menu = tk.Menu(menu, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                            activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                            bd=0, font=(T.UI, 10))
        copy_menu.add_command(label="File name", command=lambda: self._copy(rec.name))
        copy_menu.add_command(label="File name (no extension)",
                              command=lambda: self._copy(os.path.splitext(rec.name)[0]))
        copy_menu.add_command(label="Full path", command=lambda: self._copy(rec.path))
        copy_menu.add_command(label="Folder path",
                              command=lambda: self._copy(os.path.dirname(rec.path)))
        if rec.pid:
            copy_menu.add_command(label=f"Post ID ({rec.pid})", command=lambda: self._copy(rec.pid))
            copy_menu.add_command(label="e621 URL",
                                  command=lambda: self._copy(rec.url or E621_POST.format(pid=rec.pid)))
        if rec.tags:
            copy_menu.add_command(label="All tags",
                                  command=lambda: self._copy(" ".join(sorted(rec.tags))))
        if rec.artists:
            copy_menu.add_command(label="Artist", command=lambda: self._copy(", ".join(rec.artists)))
        menu.add_cascade(label="Copy", menu=copy_menu)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(str(text))
        shown = str(text)
        if len(shown) > 60:
            shown = shown[:57] + "…"
        self.set_status(f"Copied: {shown}", T.OK)

    def _reveal(self):
        if self.selected:
            open_in_explorer(self.selected.path)

    def _open_post(self):
        if self.selected:
            self._open_url(self.selected)

    def _open_url(self, rec: Rec):
        if not rec.pid:
            return
        url = rec.url or E621_POST.format(pid=rec.pid)
        try:
            webbrowser.open(url)
        except Exception:
            self._copy(url)

    # ── Vault marks (right-click "used in a project") ───────────────────

    def _mark_used(self, rec: Rec, project: str) -> None:
        conn = db_connect()
        try:
            vault_mark(conn, [rec.path], project)
        finally:
            conn.close()
        self._resync_after_vault_change(rec.path)
        self.set_status(f"Marked as used in '{project}'.", T.OK)

    def _mark_used_new(self, rec: Rec) -> None:
        dialog = ctk.CTkInputDialog(text="Name this project:", title="Mark as used")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        conn = db_connect()
        try:
            existing = len(vault_projects_list(conn))
            vault_ensure_project(conn, name, T.PROJECT_PALETTE[existing % len(T.PROJECT_PALETTE)])
        finally:
            conn.close()
        self._mark_used(rec, name)

    def _unmark_used(self, rec: Rec, project: str) -> None:
        conn = db_connect()
        try:
            vault_unmark(conn, rec.path, project)
        finally:
            conn.close()
        self._resync_after_vault_change(rec.path)
        self.set_status(f"Removed from '{project}'.", T.OK)

    def _resync_after_vault_change(self, path: str) -> None:
        """_load_library() rebuilds every Rec fresh, so self.selected (if
        it's the clip that was just marked/unmarked) would otherwise keep
        pointing at the old, now-stale copy until re-clicked."""
        self._load_library()
        self.run_search()
        if self.selected and self.selected.path == path:
            self.selected = self.by_path.get(path)
            self._render_details()

    # ── sync (incremental index build) ──────────────────────────────────────

    def _sync_clicked(self, event=None):
        self._sync(full=False)

    def _sync(self, full: bool = False):
        if self.busy:
            return
        if not self.library_dirs():
            self.set_status("Nothing to index - open Folders and pick the "
                            "converted library plus its categories.", T.WARN)
            self._open_folders()
            return
        self.busy = True
        self.sync_btn.configure(state="disabled")
        self.set_status(self.F("scanning"), T.ACCENT2)
        self.progress.set(0)
        threading.Thread(target=self._sync_work, args=(full,), daemon=True).start()

    def _sync_work(self, full: bool):
        conn = db_connect()
        gone: list = []
        try:
            if full:
                conn.execute("DELETE FROM files")
                conn.commit()

            ext = self.cfg.library_ext_set
            on_disk: dict = {}

            def take(path: str) -> None:
                if os.path.splitext(path)[1].lower() not in ext:
                    return
                if in_ignored_path(path):
                    return
                try:
                    st = os.stat(path)
                except OSError:
                    return
                on_disk[path] = (st.st_size, int(st.st_mtime))

            for directory in self.library_dirs():
                if self.cfg.library_recursive:
                    for base, _dirs, names in os.walk(directory):
                        for name in names:
                            take(os.path.join(base, name))
                else:
                    try:
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                if entry.is_file():
                                    take(entry.path)
                    except OSError:
                        continue

            known = {row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT path,size,mtime FROM files")}

            gone = [p for p in known if p not in on_disk]
            todo = [p for p, sig in on_disk.items() if known.get(p) != sig]

            for path in gone:
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                try:
                    os.remove(os.path.join(THUMB_DIR, thumb_key(path)))
                except OSError:
                    pass
            if gone:
                conn.commit()

            total = len(todo)
            self.ui(self.set_status,
                    f"{self.F('indexing')} · {total} new/changed · "
                    f"{len(gone)} removed", T.ACCENT2)
            if total:
                done = 0

                def job(path):
                    info = probe(path)
                    duration = info.duration if info else 0.0
                    width = info.width if info else 0
                    height = info.height if info else 0
                    fps = info.fps if info else 0.0
                    make_thumb(path, duration, self.cfg.thumb_width)
                    return path, (duration, width, height, fps)

                # Each job is two ffmpeg/ffprobe calls (probe + thumbnail),
                # so this stays a bit below the probe-only pool sizes used
                # elsewhere - plenty of parallelism without saturating disk
                # I/O on a first-time build of a large library.
                workers = min(6, max(2, os.cpu_count() or 4))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(job, p) for p in todo]
                    for future in as_completed(futures):
                        path, (duration, width, height, fps) = future.result()
                        size, mtime = on_disk[path]
                        base = os.path.dirname(path)
                        conn.execute(
                            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (path, os.path.basename(path), os.path.basename(base),
                             post_id_from(path), size, mtime, duration, width, height, fps))
                        done += 1
                        if done % 20 == 0:
                            conn.commit()
                        self.ui(self.progress.set, done / total)
                        if done % 5 == 0 or done == total:
                            self.ui(self.set_status,
                                    f"{self.F('indexing')} {done}/{total}", T.ACCENT2)
                conn.commit()
        finally:
            conn.close()
            self.ui(self._sync_done, len(gone))

    def _sync_done(self, removed: int):
        self.busy = False
        self.sync_btn.configure(state="normal")
        self.progress.set(1.0)
        self._load_library()
        self.run_search()
        untagged = sum(1 for r in self.records if r.pid and not r.tags)
        if untagged and self.cfg.library_autofetch and self.cfg.e621_enabled:
            self.after(400, self._fetch_tags)
        message = f"{self.F('synced')} · {len(self.records)} clips in {self._folders_summary()}"
        if removed:
            message += f" · {removed} gone"
        if untagged:
            message += f" · {untagged} still untagged (press {self.F('fetch')})"
        self.set_status(message, T.OK)

    # ── tag fetching (shared cache with Convert) ────────────────────────────

    def missing_report(self) -> dict:
        """
        What Fix missing can actually still do something about.

        A record whose file no longer exists on disk is excluded from the
        probe/thumbnail counts - Fix missing can't rebuild a thumbnail for
        a file that's gone, and counting it anyway made the button's number
        get permanently stuck (a Sync, not Fix missing, is what clears a
        deleted file out of the index).
        """
        no_tags, no_probe, no_thumb, no_id = [], [], [], 0
        seen_pid = set()
        for rec in self.records:
            if not rec.pid:
                no_id += 1
            elif self.emeta.get(rec.pid) is None and rec.pid not in seen_pid:
                seen_pid.add(rec.pid)
                no_tags.append(rec.pid)
            if not os.path.exists(rec.path):
                continue
            if rec.duration <= 0 or not rec.width:
                no_probe.append(rec)
            elif not os.path.exists(os.path.join(THUMB_DIR, thumb_key(rec.path))):
                no_thumb.append(rec)
        return {"tags": no_tags, "probe": no_probe, "thumbs": no_thumb, "no_id": no_id}

    # ── integrity check (full decode, catches what probing can't) ──────────

    def _verify_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, bg=T.ELEVATED, fg=T.TEXT,
                       activebackground=T.ACCENT_DEEP, activeforeground=T.TEXT,
                       bd=0, font=(T.UI, 10))
        menu.add_command(label="Verify library integrity…", command=self._verify_library)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _verify_library(self):
        if not self.records:
            self.set_status("Nothing to verify yet - sync the library first.", T.WARN)
            return
        if not messagebox.askyesno(
                "Verify library",
                f"Fully decode all {len(self.records)} clips to check for "
                "corruption?\n\nProbing only reads the file header, so it "
                "can't catch a truncated download or a broken frame in the "
                "middle - this does a full decode instead, which is much "
                "slower. It runs in the background, can be stopped at any "
                "point, and nothing is modified unless you delete a result "
                "yourself.",
                parent=self.root):
            return
        VerifyWindow(self.root, self)

    def _fill_missing(self):
        if self.busy:
            return
        report = self.missing_report()
        media = report["probe"] + report["thumbs"]
        if not media and not report["tags"]:
            extra = (f" ({report['no_id']} files have no post ID in the name)"
                    if report["no_id"] else "")
            self.set_status("Nothing missing - every clip is probed, "
                            f"thumbnailed and tagged{extra}.", T.OK)
            return

        if not media:
            # Nothing to probe/thumbnail - straight to tags, whose own
            # status message already says what's happening. (Setting a
            # "Fixing: N untagged" line here would just be overwritten in
            # the same tick by _fetch_tags()'s own status.)
            self._fetch_tags()
            return

        bits = []
        if report["probe"]:
            bits.append(f"{len(report['probe'])} missing details")
        if report["thumbs"]:
            bits.append(f"{len(report['thumbs'])} missing thumbnails")
        self.set_status("Fixing: " + ", ".join(bits), T.ACCENT2)

        self.busy = True
        self.fix_btn.configure(state="disabled")
        self.fetch_btn.configure(state="disabled")
        self.progress.set(0)
        self.set_status(f"Rebuilding {len(media)} missing thumbnails/details", T.ACCENT2)

        def work():
            conn = db_connect()
            done = 0
            failed = 0
            try:
                for rec in media:
                    if not os.path.exists(rec.path):
                        continue
                    info = probe(rec.path)
                    if info:
                        conn.execute(
                            "UPDATE files SET duration=?,width=?,height=?,fps=? WHERE path=?",
                            (info.duration, info.width, info.height, info.fps, rec.path))
                        make_thumb(rec.path, info.duration, self.cfg.thumb_width)
                    else:
                        failed += 1
                    done += 1
                    self.ui(self.progress.set, done / len(media))
                    if done % 10 == 0:
                        conn.commit()
                        self.ui(self.set_status, f"Rebuilding {done}/{len(media)}", T.ACCENT2)
                conn.commit()
            finally:
                conn.close()
                self.busy = False
                self.ui(self._media_fixed, done, bool(report["tags"]), failed)

        threading.Thread(target=work, daemon=True).start()

    def _media_fixed(self, done: int, more_tags: bool, failed: int = 0):
        self.fix_btn.configure(state="normal")
        self.fetch_btn.configure(state="normal")
        self._load_library()
        self.run_search()
        message = f"Rebuilt {done - failed}/{done} thumbnails/details"
        if failed:
            message += f" - {failed} could not be read (corrupt or unsupported; try Verify library)"
        self.set_status(message, T.WARN if failed else T.OK)
        if more_tags and self.cfg.e621_enabled:
            self._fetch_tags()

    def _fetch_tags(self, full: bool = False):
        """
        Fetch every uncached post ID, then fold in posts that are "due" for
        a soft refresh (see E621Meta.is_stale) - fresh posts recheck every
        few days, old ones every few months, so scores/tags stay roughly
        current without re-fetching the whole library every time.

        Called two ways: quietly and automatically (tab open, after a
        sync) with the small ambient budget from Settings, so new files
        get tagged without you having to ask; or with `full=True` when you
        press the Fetch button yourself, which lifts that budget entirely
        and catches up every post that's due, not just a small batch of it.
        """
        if self.busy:
            return
        if not self.cfg.e621_enabled:
            self.set_status("e621 lookups are switched off - turn them back on "
                            "in Settings.", T.WARN)
            return
        seen = set()
        todo = []
        all_pids = []
        for rec in self.records:
            if not rec.pid:
                continue
            all_pids.append(rec.pid)
            if rec.pid not in seen and self.emeta.get(rec.pid) is None:
                seen.add(rec.pid)
                todo.append(rec.pid)

        budget = len(all_pids) if full else self.cfg.library_stale_refresh_budget
        refreshing = self.emeta.due_for_refresh(all_pids, budget, exclude=todo)
        todo.extend(refreshing)

        if not todo:
            self.set_status("Every post ID is already tagged or cached, and "
                            "nothing is due for a refresh yet.", T.OK)
            return
        self.busy = True
        self.fetch_btn.configure(state="disabled")
        self.fix_btn.configure(state="disabled")
        delay = max(float(self.cfg.e621_fetch_delay), 0.5)
        status = f"{self.F('fetching')} · {len(todo)} posts"
        if refreshing:
            status += f" ({len(refreshing)} refreshed for freshness)"
        status += f" (~{fmt_len(len(todo) * (delay + 0.1))})"
        self.set_status(status, T.ACCENT2)
        self.progress.set(0)

        def work():
            hits = missing = failed = 0
            last_error = ""
            try:
                for index, pid in enumerate(todo):
                    record = self.emeta.fetch(pid, self.cfg.e621_user, self.cfg.e621_key)
                    if record.get("missing"):
                        missing += 1
                    elif record.get("error"):
                        failed += 1
                        last_error = record["error"]
                    else:
                        hits += 1
                    self.ui(self.progress.set, (index + 1) / len(todo))
                    if index % 5 == 0:
                        self.ui(self.set_status, f"{self.F('fetching')} {index + 1}/{len(todo)}",
                                T.ACCENT2)
                    if index % 10 == 9:
                        self.emeta.save()
                    if index + 1 < len(todo):
                        time.sleep(delay)
            finally:
                self.emeta.save()
                self.busy = False
                self.ui(self.fetch_btn.configure, state="normal")
                self.ui(self.fix_btn.configure, state="normal")
                self.ui(self._fetch_done, hits, missing, failed, last_error, len(refreshing))

        threading.Thread(target=work, daemon=True).start()

    def _fetch_done(self, hits: int, missing: int, failed: int = 0,
                    last_error: str = "", refreshed: int = 0) -> None:
        self._load_library()
        self.run_search()
        self._render_details()
        # "missing" (post deleted/hidden on e621, cached so it's never
        # retried) and "failed" (a transient network error, not cached, so
        # it's retried the next time Fix missing / Fetch tags runs) look the
        # same from the outside if lumped together - a permanently stuck
        # count with no way to tell why is worse than no count at all.
        bits = [f"{hits} tagged"]
        if missing:
            bits.append(f"{missing} gone on e621")
        if failed:
            reason = f" ({last_error})" if last_error else ""
            bits.append(f"{failed} failed, will retry{reason}")
        if refreshed:
            bits.append(f"{refreshed} refreshed")
        self.set_status("Tags fetched: " + " · ".join(bits),
                        T.OK if hits else (T.WARN if (missing or failed) else T.OK))

