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

from .theme import T, font, lens_photo, pt, px, LIBRARY_LABELS
from .format import fmt_len, fmt_size, fmt_score
from .files import (
    is_ignored_dir, in_ignored_path, post_id_from, open_file, open_in_explorer,
)
from .config import THUMB_DIR
from .media import fit_frame, round_corners, thumb_key, make_thumb, probe
from .e621 import E621_POST
from .library_db import (
    db_connect, Rec, parse_query, rec_matches, SORTS,
    vault_marks_by_path, vault_unmark, vault_projects_list, vault_ensure_project, vault_mark,
    manual_tags_by_path, manual_tag_set, manual_tag_add, clean_tags,
)
from .library_player import InlinePlayer
from . import uithread
from .library_windows import HiddenTagsWindow, HelpWindow, FoldersWindow, VerifyWindow
from .convert_widgets import ContactSheet
from .widgets import popup_menu, menu_rule

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
        # In-tile hover preview (see "hover preview" further down)
        self._pv_index: int | None = None
        self._pv_after = None
        self._pv_step = 0
        self._pv_token = 0
        self._pv_photo = None            # keep-alive for the frame on screen
        self._static_thumb: dict = {}    # index -> the card's resting thumbnail
        # path -> {"frames": [jpeg bytes], "photos": {i: PhotoImage}}. The
        # converted frames ride along with the reel they came from, so
        # evicting one clip drops its images with it.
        self._reels: dict = {}
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
        self._badge_font = tkfont.Font(family=T.MONO, size=pt(8))
        self._spec_font = tkfont.Font(family=T.MONO, size=9)
        self._chip_font = tkfont.Font(family=T.UI, size=pt(11))
        self._quick_font = tkfont.Font(family=T.UI, size=pt(10))
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

    SEARCH_H = 38

    def _build_topbar(self):
        """
        Two rows instead of one long one: browsing up top (the search field
        leading, then rating/sort), a separate action toolbar underneath
        (sync + maintenance on the left, configuration on the right). The
        standalone Folders button is gone - Settings > Library > "Change
        folders..." and Ctrl+O both still reach it, so it doesn't need its
        own slot in an already busy row.
        """
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")

        # ── row 1: browse ────────────────────────────────────────────────
        row1 = ctk.CTkFrame(bar, fg_color="transparent")
        row1.pack(fill="x")
        row1.grid_columnconfigure(0, weight=1)

        # Search leads the row. The suite's identity is in the header strip
        # and which tab you're on is in the tab strip right above this, so a
        # third "PAZ Library" lockup here was only pushing the one control
        # this tab is actually built around into the middle of the row.
        box = ctk.CTkFrame(row1, fg_color=T.INPUT, corner_radius=10,
                           border_width=1, border_color=T.ACCENT2_DEEP,
                           height=self.SEARCH_H)
        box.grid(row=0, column=0, sticky="ew", padx=(18, 12), pady=(11, 7))
        box.pack_propagate(False)

        self._lens = lens_photo(15, T.ACCENT2)
        tk.Label(box, image=self._lens, bg=T.INPUT, bd=0
                 ).pack(side="left", padx=(13, 0))

        # The ↺/✕ pair rides inside the field rather than beside it, so the
        # row reads as one control instead of three parked next to each other.
        def boxbtn(text, cmd, size=13):
            ctk.CTkButton(box, text=text, width=28, height=self.SEARCH_H - 12,
                          corner_radius=7, font=font(size),
                          fg_color="transparent", hover_color=T.BTN_HOV,
                          text_color=T.DIM, command=cmd
                          ).pack(side="right", padx=(0, 5))

        boxbtn("✕", self._clear_search, 12)
        boxbtn("↺", self._show_history)

        self.search = ctk.CTkEntry(
            box, placeholder_text="wolf -mlp  artist:name  rating:e  "
                                  "folder:Furry      ( / to focus )",
            height=self.SEARCH_H - 8, font=font(13), corner_radius=0,
            fg_color="transparent", border_width=0, text_color=T.TEXT,
            placeholder_text_color=T.FAINT)
        self.search.pack(side="left", fill="both", expand=True, padx=(8, 4))
        self.search.bind("<KeyRelease>", self._on_search_key)
        self.search.bind("<Return>", self._commit_search)
        self.search.bind("<Up>", lambda e: self._history_step(-1))
        self.search.bind("<Down>", lambda e: self._history_step(1))
        self.search.bind("<Escape>", lambda e: self._clear_search())
        self._history_pos = -1

        right1 = ctk.CTkFrame(row1, fg_color="transparent")
        right1.grid(row=0, column=1, sticky="e", padx=(0, 16), pady=(11, 7))

        self.rating_seg = ctk.CTkSegmentedButton(
            right1, values=["All", "S", "Q", "E"], command=lambda _v: self.run_search(),
            font=font(11), height=self.SEARCH_H, corner_radius=9, fg_color=T.INPUT,
            selected_color=T.ACCENT2_DEEP, selected_hover_color=T.ACCENT2_DEEP,
            unselected_color=T.INPUT, unselected_hover_color=T.BTN_HOV,
            text_color=T.DIM, border_width=2)
        self.rating_seg.set("All")
        self.rating_seg.pack(side="left", padx=(0, 8))

        self.sort_menu = ctk.CTkOptionMenu(
            right1, values=list(SORTS), width=118, height=self.SEARCH_H,
            font=font(12), corner_radius=9, fg_color=T.INPUT, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED,
            dropdown_hover_color=T.ACCENT2_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11),
            text_color=T.TEXT, command=lambda _v: self.run_search())
        self.sort_menu.set(self.cfg.sort if self.cfg.sort in SORTS else "Newest")
        self.sort_menu.pack(side="left", padx=(0, 8))

        ctk.CTkButton(right1, text="▲ Top", width=62, height=self.SEARCH_H,
                      corner_radius=9, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.ACCENT2,
                      command=lambda: self.add_token("sort:score")
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

    # Everything this app sends lands in one bin, so a Resolve project
    # doesn't end up with library clips scattered through its root.
    RESOLVE_BIN = "PAZ Library"

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

        # Row 0 pairs the active search tokens (left, usually empty) with
        # the running total (right). The total used to sit on the filter
        # row below, where it was the widest thing competing for space and
        # got cut to "6 clips · 22" the moment the window narrowed.
        head = ctk.CTkFrame(center, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)

        self.chips = ctk.CTkFrame(head, fg_color="transparent", height=30)
        self.chips.grid(row=0, column=0, sticky="ew")

        self.count_label = ctk.CTkLabel(head, text="", font=font(10, mono=True),
                                        text_color=T.DIM, anchor="e")
        self.count_label.grid(row=0, column=1, sticky="e", padx=(12, 2))

        info = ctk.CTkFrame(center, fg_color="transparent")
        info.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        # Only the empty spacer between the two groups carries a weight, so
        # a shortfall comes out of the gap rather than off the end of the
        # chip row - which is how the last chip used to vanish whenever the
        # inspector panel grew.
        info.grid_columnconfigure(1, weight=1)

        # Filter chips only, on the left - actions (Random, ratio) live on
        # the right with the pager instead, since they're things you DO,
        # not ways of narrowing what's shown.
        left = ctk.CTkFrame(info, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        self._info_row = info
        self._info_chips = left
        self._info_stacked = False
        self.quick_chips = {}
        for key, text, token in (
                ("untagged", "Untagged", "is:untagged"),
                ("noid", "No post ID", "is:noid"),
                ("4k", "4K ✓", "is:4k"),
                ("no4k", "Non-4K", "is:no4k")):
            chip = ctk.CTkButton(left, text=text, height=22, width=92,
                                 corner_radius=11, font=font(10),
                                 fg_color=T.BTN, hover_color=T.BTN_HOV,
                                 text_color=T.DIM,
                                 command=lambda t=token: self.add_token(t))
            chip.pack(side="left", padx=(0, 6))
            self.quick_chips[key] = (chip, text)

        pager = ctk.CTkFrame(info, fg_color="transparent")
        pager.grid(row=0, column=2, sticky="e", padx=(14, 0))
        self._info_pager = pager
        info.bind("<Configure>", self._fit_info_row)

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
        menu = popup_menu(self.root)
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

    PANEL_MIN, PANEL_MAX = 512, 1500
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
        base = int(max(self.PANEL_MIN, min(total * 0.345, self.PANEL_MAX)))
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

        self.detail_name = ctk.CTkLabel(card, text="Nothing selected", font=font(15, "bold"),
                                         text_color=T.TEXT, anchor="w",
                                         wraplength=470, justify="left")
        self.detail_name.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 0))
        self.detail_meta = ctk.CTkLabel(card, text="", font=font(12, mono=True),
                                         text_color=T.DIM, anchor="w",
                                         wraplength=470, justify="left")
        self.detail_meta.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 8))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))

        def dbtn(text, cmd, color=T.DIM, width=76):
            b = ctk.CTkButton(buttons, text=text, width=width, height=29,
                              corner_radius=7, font=font(11), fg_color=T.BTN,
                              hover_color=T.BTN_HOV, text_color=color, command=cmd)
            b.pack(side="left", padx=(0, 5))
            return b

        dbtn("Folder", self._reveal)
        dbtn("→ Resolve", lambda: self.send_to_resolve(
            [self.selected] if self.selected else []), T.ACCENT3, 92)
        self.e621_open_btn = dbtn("e621", self._open_post, T.ACCENT2, 58)
        dbtn("Grid", self._grid, T.ACCENT, 62)
        dbtn("Copy name", lambda: self._copy(self.selected.name)
             if self.selected else None, T.DIM, 96)

        ctk.CTkLabel(panel, text="TAGS · click to search · right-click for more",
                     font=font(11, "bold"), text_color=T.FAINT, anchor="w"
                     ).grid(row=2, column=0, sticky="ew", padx=6, pady=(14, 5))

        self.detail_tags = ctk.CTkScrollableFrame(
            panel, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.LINE, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        self.detail_tags.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        self.detail_tags.grid_columnconfigure(0, weight=1)

    # ── brand ────────────────────────────────────────────────────────────

    def _apply_brand(self):
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
            # Sized to the text, not to a fixed 92px: "Non-4K (4)" is
            # wider than "4K ✓ (2)" and a shared width clipped the longest
            # label's closing bracket.
            text = f"{label}  {count}"
            chip.configure(text=text,
                           width=self._quick_font.measure(text) + 26)
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
        manual = manual_tags_by_path(conn)
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
            # Hand-typed tags sit alongside anything fetched, so a clip
            # with no post ID is still searchable and no longer counts as
            # untagged. Kept separately as well so the tag editor knows
            # which of a clip's tags are yours to change.
            rec.manual = manual.get(rec.path, set())
            if rec.manual:
                rec.tags = set(rec.tags) | rec.manual
                rec.compute_named()
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
        menu = popup_menu(self.root)
        for query in history[:20]:
            menu.add_command(label=query, command=lambda q=query: self._apply_search(q))
        menu_rule(menu)
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

    def _fit_info_row(self, event) -> None:
        """Chips and pager share one row until they can't both fit, then the
        chips drop to a second row. Narrow windows used to push the pager -
        page numbers and the next/previous buttons - clean off the edge."""
        needed = (self._info_chips.winfo_reqwidth()
                  + self._info_pager.winfo_reqwidth() + 30)
        stack = needed > event.width
        if stack == self._info_stacked:
            return
        self._info_stacked = stack
        if stack:
            self._info_chips.grid(row=1, column=0, columnspan=3, sticky="w",
                                  pady=(6, 0))
        else:
            self._info_chips.grid(row=0, column=0, columnspan=1, sticky="w",
                                  pady=0)

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

    # ── tag panel ────────────────────────────────────────────────────────
    #
    # Tags render as chips packed into rows, the way the design has them,
    # not one full-width button per tag: the sidebar holds three or four
    # times as many that way, and a wall of identical full-width rows is
    # exactly the "not clean" part. Tk has no flex-wrap, so rows are
    # measured and filled by hand.

    CHIP_PAD = 18        # chip padding + border, on top of the text width
    CHIP_ROOM = 236      # usable width inside the sidebar

    def _group_header(self, title: str, key: str, open_now: bool,
                      count: int, row: int) -> int:
        header = ctk.CTkButton(
            self.tagpanel,
            text=("▾  " if open_now else "▸  ") + f"{title}   {count}",
            height=22, corner_radius=5, font=font(9, "bold"), anchor="w",
            fg_color="transparent", hover_color=T.BTN_HOV, text_color=T.FAINT,
            command=lambda k=key: self._toggle_sidebar_group(k))
        header.grid(row=row, column=0, sticky="ew", padx=6,
                    pady=(12 if row else 4, 3))
        return row + 1

    def _chip_flow(self, items: list, row: int, menu: bool) -> int:
        """items: (name, count, token, text colour, swatch colour or None).
        Packs them left-to-right, wrapping when the next chip won't fit."""
        line = None
        used = 0
        for name, count, token, colour, swatch in items:
            label = f"{name}  {count}"
            width = self._chip_font.measure(label) + self.CHIP_PAD
            if swatch:
                width += 10
            if line is None or used + width > self.CHIP_ROOM:
                line = ctk.CTkFrame(self.tagpanel, fg_color="transparent")
                line.grid(row=row, column=0, sticky="w", padx=5, pady=1)
                row += 1
                used = 0
            chip = ctk.CTkButton(
                line, text=label, height=24, width=width, corner_radius=6,
                font=font(11), fg_color=T.SURFACE, hover_color=T.BTN_HOV,
                border_width=1, border_color=T.LINE, text_color=colour,
                command=lambda t=token: self.add_token(t))
            chip.pack(side="left", padx=(0, 4))
            if menu:
                chip.bind("<Button-3>",
                          lambda e, t=token, n=name: self._tag_menu(e, t, n))
            used += width + 4
        return row

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
            row = self._group_header(title, key, open_now, len(visible), row)
            if not open_now:
                continue
            row = self._chip_flow(
                [(name, count, prefix + name, colour, None) for name, count in visible],
                row, menu=True)

        # PROJECTS gets its own block instead of the loop above - project
        # names are free text (can hold spaces), so the search token needs
        # quoting, and each one gets its own Vault-assigned colour rather
        # than one fixed colour for the whole group.
        if projects:
            key = "projects"
            open_now = self.cfg.sidebar_group_open.get(key, True)
            row = self._group_header("PROJECTS", key, open_now, len(projects), row)
            if open_now:
                row = self._chip_flow(
                    [(name, count, f'used:"{name}"',
                      self._project_colors.get(name, T.DIM), self._project_colors.get(name))
                     for name, count in projects.most_common(60)],
                    row, menu=False)
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
    GAP = 10

    @property
    def CAP_H(self) -> int:
        """Caption strip height. Grows with the text scale - at 200% the
        two lines of caption no longer fit in a fixed 42px band."""
        return px(42)

    @property
    def card_width(self) -> int:
        """The tile width actually drawn. The setting is in unscaled
        pixels - what it looks like at 100% - so raising the display scale
        grows the tiles along with the text in them, instead of leaving
        224px tiles surrounded by text that no longer fits."""
        return px(max(120, min(int(self.cfg.card_width), 480)))

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
        self._static_thumb = {}
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
                               font=(T.UI, pt(12)), anchor="nw", width=width - 60)
            return
        if not batch:
            canvas.create_text(24, 28, text=self.F("no_results"), fill=T.FAINT,
                               font=(T.UI, pt(12)), anchor="nw")
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

        # Each card is a box, the way the design has it: a surface plate
        # behind image and caption together, and an outline on top that
        # carries state. The outline is one line doing three jobs - the
        # project colour when a clip is already spent, the accent when it
        # is selected or hovered - which is why the old separate "used"
        # frame and hover strip are gone.
        bottom = y + self.IMG_H + self.CAP_H - 4
        canvas.create_rectangle(x, y, x + self.CARD_W, bottom,
                                fill=T.SURFACE, outline="", tags=(tag, f"cardbg{index}"))
        canvas.create_rectangle(x, y, x + self.CARD_W, y + self.IMG_H,
                                fill=T.INPUT, outline="", tags=(tag, "well"))
        canvas.create_text(x + self.CARD_W // 2, y + self.IMG_H // 2, text="…",
                           fill=T.FAINT, font=(T.UI, pt(11)), tags=(tag, f"ph{index}"))
        colour, width = self._card_outline(rec, hover=False)
        canvas.create_rectangle(x, y, x + self.CARD_W, bottom, fill="",
                                outline=colour, width=width, tags=(tag, f"cardline{index}"))

        # Caption follows the design: the clip's own name on top, then the
        # artist with the score pushed right. Resolution/fps and the rating
        # moved onto the thumbnail as badges (see _place_thumb) - they read
        # faster over the frame than as another line of grey text, and it
        # buys the name a full line instead of sharing one with a dot.
        name = rec.pid or os.path.splitext(rec.name)[0]
        canvas.create_text(x + px(8), y + self.IMG_H + px(15),
                           text=self._ellipsize(name, x + self.CARD_W - 10),
                           fill=T.TEXT, font=(T.MONO, pt(10)), anchor="w", tags=(tag, f"tt{index}"))

        score = fmt_score(rec.score)
        score_w = (self._spec_font.measure(f"▲{score}") + 10) if score else 0
        if rec.artists:
            canvas.create_text(x + px(8), y + self.IMG_H + px(31),
                               text=self._ellipsize(rec.artists[0],
                                                    x + self.CARD_W - score_w - 12),
                               fill=T.ACCENT2, font=(T.UI, pt(10)), anchor="w", tags=(tag,))
        if score:
            canvas.create_text(x + self.CARD_W - px(8), y + self.IMG_H + px(31), text=f"▲{score}",
                               fill=T.OK if rec.score >= 1000 else T.FAINT,
                               font=(T.MONO, pt(9)), anchor="e", tags=(tag,))

        self._layout.append({"rec": rec, "x": x, "y": y, "tag": tag})

        canvas.tag_bind(tag, "<Button-1>", lambda e, r=rec: self._select(r))
        canvas.tag_bind(tag, "<Double-Button-1>", lambda e, r=rec: self._select_and_play(r))
        canvas.tag_bind(tag, "<Button-3>", lambda e, r=rec: self._card_menu(e, r))
        canvas.tag_bind(tag, "<Enter>", lambda e, i=index: self._set_hover(i))
        canvas.tag_bind(tag, "<Leave>", lambda e, i=index: self._unhover(i))

    def _card_outline(self, rec: Rec, hover: bool) -> tuple:
        """(colour, width) for a card's border. Selection outranks the
        project mark, which outranks hover - you need to know which card
        you are acting on before you need to know where it has been."""
        if self.selected and self.selected.path == rec.path:
            return T.ACCENT, 2
        if rec.used_projects and rec.used_color:
            return rec.used_color, 2
        if hover:
            return T.ACCENT2, 2
        return T.LINE, 1

    def _restyle_cards(self):
        for index, slot in enumerate(self._layout):
            colour, width = self._card_outline(
                slot["rec"], hover=(index == self._hover_index))
            self.gallery.itemconfigure(f"cardline{index}", outline=colour, width=width)

    def _set_hover(self, index):
        if index == self._hover_index:
            return
        self._hover_index = index
        self._restyle_cards()
        self._preview_stop()
        self._preview_arm(index)

    def _unhover(self, index):
        if self._hover_index == index:
            self._hover_index = None
            self._restyle_cards()
        self._preview_stop()

    # ── hover preview ────────────────────────────────────────────────────
    #
    # YouTube's model: rest on a tile and the tile plays. Nothing floats,
    # nothing follows the cursor, and the motion is real motion.
    #
    # Getting there took two goes. The first version flipped storyboard
    # cells, which cannot look smooth however fast you flip it - those
    # cells are seconds apart in the clip, so consecutive frames have
    # nothing to do with each other, and any frame the sheet had not built
    # yet cost an ffmpeg seek of its own mid-playback. That is the jumping
    # and stuttering. Now one ffmpeg pass decodes a run of CONSECUTIVE
    # frames up front (see ThumbCache.preview_reel) and playback is pure
    # memory: no subprocess between frames, so the cadence is even.
    #
    # Decoded reels are kept for the last few clips hovered, so going back
    # to a tile you just left starts instantly instead of decoding again.

    PREVIEW_DWELL_MS = 140     # rest this long before a tile starts
    # Clips worth of decoded frames to keep. Held down as the preview
    # window grew, so the total frames in memory stays about where it was.
    REEL_CACHE       = 4
    # Play at the clip's OWN frame rate rather than a number picked here,
    # so a preview runs at the speed the footage was shot at - that is what
    # makes it read as the video playing rather than an animation of it.
    # Clamped at both ends: below ~12 nothing looks like motion, and above
    # 30 the decode cost doubles for smoothness nobody can see in a 224px
    # tile (a 60fps 4K clip would otherwise mean 240 frames per preview).
    PREVIEW_FPS_MIN  = 12
    PREVIEW_FPS_MAX  = 30
    PREVIEW_FPS_FALLBACK = 24  # when the clip never reported a frame rate

    def _preview_fps(self, rec: Rec) -> int:
        rate = rec.fps or self.PREVIEW_FPS_FALLBACK
        return int(max(self.PREVIEW_FPS_MIN, min(round(rate), self.PREVIEW_FPS_MAX)))

    def _preview_arm(self, index) -> None:
        if index is None or index >= len(self._layout):
            return
        rec = self._layout[index]["rec"]
        if rec.duration <= 0:
            return
        self._pv_index = index
        self._pv_step = 0
        self._pv_token += 1
        self._pv_after = self.after(self.PREVIEW_DWELL_MS, self._preview_begin)

    def _preview_begin(self) -> None:
        self._pv_after = None
        index = self._pv_index
        if index is None or index >= len(self._layout):
            return
        rec = self._layout[index]["rec"]
        token = self._pv_token
        reel = self._reels.get(rec.path)
        if reel is not None:
            self._preview_play(index, reel, token)
            return
        threading.Thread(target=self._preview_decode,
                         args=(rec, index, token), daemon=True).start()

    def _preview_decode(self, rec: Rec, index: int, token: int) -> None:
        """Decode on a worker thread, handing frames to the UI as they
        appear instead of when the whole reel is done. A ten-second reel of
        4K takes seconds to decode in full; the first frames are ready
        almost at once, and that is the difference between a preview that
        starts when you stop moving and one that looks broken."""
        fps = self._preview_fps(rec)
        self.frames.preview_reel_stream(
            rec.path, rec.duration, self.CARD_W,
            on_frames=lambda frames, done: self.ui(
                self._preview_frames, rec.path, index, frames, done, fps, token),
            alive=lambda: token == self._pv_token,
            fps=fps)

    def _preview_frames(self, path: str, index: int, frames: list, done: bool,
                         fps: int, token: int) -> None:
        reel = self._reels.get(path)
        if reel is None:
            reel = {"frames": [], "photos": {}, "fps": fps, "done": False}
            self._reels[path] = reel
            while len(self._reels) > self.REEL_CACHE:
                oldest = next(iter(self._reels))
                if oldest != path:
                    self._reels.pop(oldest)
                else:
                    break
        reel["frames"].extend(frames)
        if done:
            reel["done"] = True
            if not reel["frames"]:
                self._reels.pop(path, None)
                return
        if token != self._pv_token or self._pv_index != index:
            return
        # Start on the first batch; later batches just extend what is
        # already playing, so nothing restarts mid-preview.
        if frames and len(reel["frames"]) == len(frames):
            self._preview_play(index, reel, token)

    def _preview_play(self, index: int, reel: dict, token: int) -> None:
        if token != self._pv_token or self._pv_index != index:
            return
        if index >= len(self._layout) or not self.gallery.find_withtag(f"im{index}"):
            return
        count = len(reel["frames"])
        if not count:
            return
        # Only wrap once the whole reel is in. Wrapping early would loop
        # the first second over and over while the rest is still decoding.
        i = self._pv_step % count if reel.get("done") else min(self._pv_step, count - 1)
        frame = self._reel_photo(reel, i)
        if frame is not None:
            self.gallery.itemconfigure(f"im{index}", image=frame)
            self._pv_photo = frame
            self._draw_progress(index, i / count)
        self._pv_step += 1
        self._pv_after = self.after(
            max(int(1000 / reel.get("fps", self.PREVIEW_FPS_FALLBACK)), 16),
            lambda: self._preview_play(index, reel, token))

    def _reel_photo(self, reel: dict, i: int):
        """PhotoImages have to be built on the UI thread, so they are made
        as each frame comes up and kept - one small conversion per frame is
        invisible, where converting a whole reel up front is a visible
        stall right when playback should be starting."""
        cache = reel["photos"]
        photo = cache.get(i)
        if photo is not None:
            return photo
        try:
            image = Image.open(io.BytesIO(reel["frames"][i]))
            image = fit_frame(image, self.CARD_W, self.IMG_H, self.cfg.thumb_fit)
            image = round_corners(image, 9, T.SURFACE)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return None
        cache[i] = photo
        return photo

    def _draw_progress(self, index: int, frac: float) -> None:
        slot = self._layout[index]
        canvas = self.gallery
        x, y = slot["x"], slot["y"]
        top = y + self.IMG_H - 3
        canvas.delete(f"pv{index}")
        canvas.create_rectangle(x, top, x + self.CARD_W, y + self.IMG_H,
                                fill=T.LINE_SOFT, outline="",
                                tags=(slot["tag"], f"pv{index}"))
        canvas.create_rectangle(x, top, x + int(self.CARD_W * frac), y + self.IMG_H,
                                fill=T.ACCENT, outline="",
                                tags=(slot["tag"], f"pv{index}"))

    def _preview_stop(self) -> None:
        self._pv_token += 1
        if self._pv_after is not None:
            try:
                self.after_cancel(self._pv_after)
            except ValueError:
                pass
            self._pv_after = None
        index, self._pv_index = self._pv_index, None
        self._pv_step = 0
        self._pv_photo = None
        if index is None:
            return
        self.gallery.delete(f"pv{index}")
        resting = self._static_thumb.get(index)
        if resting is not None and self.gallery.find_withtag(f"im{index}"):
            self.gallery.itemconfigure(f"im{index}", image=resting)

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
                               text="no thumb", fill=T.FAINT, font=(T.UI, pt(9)),
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
        self._static_thumb[index] = photo
        canvas.create_image(slot["x"], slot["y"], image=photo, anchor="nw",
                            tags=(slot["tag"], f"im{index}"))
        self._draw_badges(index, rec, slot)
        canvas.tag_raise(f"cardline{index}")

    # ── thumbnail badges ─────────────────────────────────────────────────
    #
    # Over the frame rather than under it, the way the design has them: the
    # three things you scan a wall of clips for - is it edit-ready, how
    # explicit is it, how long is it - without any of them costing a line
    # of caption. Drawn here rather than in _draw_card because they have to
    # sit on top of the thumbnail, which only exists once it has loaded.

    def _pill(self, tag: str, x: int, y: int, text: str, colour: str,
              anchor: str = "nw") -> None:
        """A small dark plate with a line of mono on it. `x`,`y` is the
        corner named by `anchor` ("nw" or "ne")."""
        canvas = self.gallery
        pad, h = px(4), px(14)
        w = self._badge_font.measure(text) + pad * 2
        x0 = x if anchor == "nw" else x - w
        canvas.create_rectangle(x0, y, x0 + w, y + h, fill=T.BG, outline="",
                                tags=(tag,))
        canvas.create_text(x0 + pad, y + h // 2, text=text, fill=colour,
                           font=(T.MONO, pt(8)), anchor="w", tags=(tag,))

    def _draw_badges(self, index: int, rec: Rec, slot: dict) -> None:
        canvas = self.gallery
        tag = slot["tag"]
        x, y = slot["x"], slot["y"]

        # Top left: what the pipeline cares about. Green once a clip is
        # edit-pool quality, dim while it still needs an upscale.
        spec = f"{rec.height}p" if rec.height else "--"
        if rec.fps:
            spec += f"·{rec.fps:.0f}"
        self._pill(tag, x + 5, y + 5, spec, T.OK if rec.premium else T.DIM)

        # Top right: the rating, as its own colour. Explicit is the loudest
        # of the three because that is what gets scanned for.
        if rec.rating:
            colour = T.RATING.get(rec.rating, T.DIM)
            cx, cy, r = x + self.CARD_W - 13, y + 12, 8
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=T.BG,
                               outline="", tags=(tag,))
            canvas.create_text(cx, cy, text=rec.rating.upper(), fill=colour,
                               font=(T.MONO, pt(8), "bold"), tags=(tag,))

        # Bottom right: length.
        self._pill(tag, x + self.CARD_W - px(5), y + self.IMG_H - px(19),
                   fmt_len(rec.duration), T.TEXT, anchor="ne")

        # Bottom left: which project already spent this clip. The coloured
        # border says "used"; this says used *where*, which is the part you
        # actually need when deciding whether to reuse it.
        if rec.used_projects:
            label = rec.used_projects[0]
            room = self.CARD_W - 62
            while label and self._badge_font.measure(label) > room:
                label = label[:-1]
            self._pill(tag, x + px(5), y + self.IMG_H - px(19), label or "used",
                       rec.used_color or T.DIM)
        if rec.used_projects:
            canvas.tag_raise(f"used{index}")

    # ── hover scrub ─────────────────────────────────────────────────────────

    def _peek_hide(self):
        """Kept because several places (tab switch, page render, scroll)
        want to make sure nothing is previewing. The gallery's preview now
        lives in the tile, so that is what this stops."""
        self._preview_stop()

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
            label.configure(wraplength=inner - 22)

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
            height=29, corner_radius=7, font=font(11, "bold"), anchor="w",
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
            self.detail_tags, text=label, height=28, corner_radius=6,
            font=font(13), anchor="w", fg_color="transparent",
            hover_color=T.BTN_HOV, text_color=colour,
            command=lambda t=token: self.add_token(t))
        button.grid(row=row, column=column, columnspan=span, sticky="ew", padx=6, pady=2)
        button.bind("<Button-3>", lambda e, t=token: self._tag_menu(e, t, label))

    def _tag_menu(self, event, token: str, name: str):
        menu = popup_menu(self.root)
        menu.add_command(label=f"Search {name}", command=lambda: self.add_token(token))
        menu.add_command(label=f"Exclude -{name}",
                         command=lambda: self.add_token(token, negative=True))
        menu_rule(menu)
        menu.add_command(label="Hide from sidebar", command=lambda: self.hide_tag(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _card_menu(self, event, rec: Rec):
        self._select(rec)
        menu = popup_menu(self.root)
        menu.add_command(label="Play here", command=lambda: self._select_and_play(rec))
        menu.add_command(label="Open externally", command=lambda: open_file(rec.path))
        menu.add_command(label="Show in folder", command=lambda: open_in_explorer(rec.path))
        if rec.pid:
            menu.add_command(label=f"Open e621 post #{rec.pid}",
                             command=lambda: self._open_url(rec))
        menu_rule(menu)
        menu.add_command(label="Tags…", command=lambda: self._edit_tags(rec))
        page = self.page_recs()
        menu.add_command(label=f"Tag these {len(page)} results…",
                         command=lambda: self._bulk_tag(page))
        menu_rule(menu)
        menu.add_command(label="Add to Resolve",
                         command=lambda: self.send_to_resolve([rec]))
        page = self.page_recs()
        menu.add_command(label=f"Add these {len(page)} results to Resolve",
                         command=lambda: self.send_to_resolve(page))
        menu_rule(menu)

        used_menu = popup_menu(menu)
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
        menu_rule(menu)
        copy_menu = popup_menu(menu)
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

    # ── tags you type yourself ──────────────────────────────────────────
    #
    # Fetching from e621 needs a post ID and a lot of patience; on a
    # library of 4K files it is an evening's work, and it can do nothing at
    # all for a clip whose ID was lost in a rename. Typing a few tags by
    # hand takes seconds and works on anything, so "Untagged" stops being a
    # filter you can look at but not act on.

    def _ask_tags(self, title: str, prompt: str, initial: str = "") -> str | None:
        """A themed text prompt. CustomTkinter's input dialog builds its
        entry a beat after construction, so an initial value has to be put
        in on a timer rather than straight away - and left to its own
        colours it turns up in stock blue, which nothing else here is."""
        dialog = ctk.CTkInputDialog(
            title=title, text=prompt,
            fg_color=T.SURFACE, text_color=T.TEXT,
            button_fg_color=T.ACCENT2_DEEP, button_hover_color=T.BTN_HOV,
            button_text_color=T.ACCENT2, entry_fg_color=T.INPUT,
            entry_border_color=T.LINE, entry_text_color=T.TEXT)

        def prefill() -> None:
            entry = getattr(dialog, "_entry", None)
            if entry is None:
                dialog.after(30, prefill)
                return
            if initial:
                entry.insert(0, initial)
            entry.focus_set()

        dialog.after(40, prefill)
        return dialog.get_input()

    def _edit_tags(self, rec: Rec) -> None:
        answer = self._ask_tags(
            f"Tags for {rec.name}",
            "Your own tags, separated by spaces.\n"
            "Anything fetched from e621 is kept as well.",
            " ".join(sorted(rec.manual)))
        if answer is None:
            return
        tags = clean_tags(answer)
        conn = db_connect()
        try:
            manual_tag_set(conn, rec.path, tags)
        finally:
            conn.close()
        self._reload_and_keep_place()
        self.set_status(
            f"{len(tags)} tag{'s' if len(tags) != 1 else ''} on {rec.name}."
            if tags else f"Cleared your tags on {rec.name}.", T.OK)

    def _bulk_tag(self, recs: list) -> None:
        """Add the same tags to everything currently on screen. Adds only -
        a bulk edit that could wipe tags off a hundred clips at once is not
        worth the one keystroke it saves."""
        recs = [r for r in recs if r]
        if not recs:
            return
        answer = self._ask_tags(
            f"Tag {len(recs)} clips",
            f"Tags to add to all {len(recs)} results on this page, "
            "separated by spaces.")
        if answer is None:
            return
        tags = clean_tags(answer)
        if not tags:
            return
        conn = db_connect()
        try:
            added = manual_tag_add(conn, [r.path for r in recs], tags)
        finally:
            conn.close()
        self._reload_and_keep_place()
        self.set_status(f"Added {', '.join(tags)} to {len(recs)} clips "
                        f"({added} new).", T.OK)

    def _reload_and_keep_place(self) -> None:
        """Re-read the library and land back where you were, rather than
        bouncing to page one after every edit."""
        page, selected = self.page, self.selected.path if self.selected else ""
        self._load_library()
        self.run_search()
        self.page = page
        if selected and selected in self.by_path:
            self.selected = self.by_path[selected]
        self.render_page()
        self._render_details()
        self._render_tagpanel()

    # ── Resolve hand-off ────────────────────────────────────────────────
    #
    # The library already holds two copies of a clip - the converted one it
    # indexes and, where the Convert tab made one, a 4K/60 edit-ready copy
    # in the premium pool. That is the same split Resolve calls master and
    # proxy, so importing wires it up: the 4K file becomes the clip and the
    # converted one becomes its proxy. Timeline playback stays cheap and
    # renders still come off the big file, without anyone linking proxies
    # by hand.

    def page_recs(self) -> list:
        """What the gallery is showing right now - the current page of the
        current search, not the whole library. Sending "everything" from a
        five-figure library would be a mistake nobody could undo quickly."""
        start = self.page * self.cfg.page_size
        return self.filtered[start:start + self.cfg.page_size]

    def _resolve_pair(self, rec: Rec) -> tuple:
        """(what Resolve should treat as the clip, what it should use as
        the proxy). The premium copy leads when there is one; with no
        premium copy there is nothing to proxy, so the file stands alone."""
        if rec.premium_path and os.path.exists(rec.premium_path):
            return rec.premium_path, rec.path
        return rec.path, ""

    def send_to_resolve(self, recs: list, bin_name: str = "") -> None:
        """Import `recs` into Resolve's media pool. `bin_name` overrides the
        default bin - the Vault sends a whole project into a bin named after
        it, which is how you'd organise it by hand anyway."""
        recs = [r for r in recs if r]
        if not recs:
            self.set_status("Nothing selected to send.", T.WARN)
            return
        pairs = [self._resolve_pair(rec) for rec in recs]
        premium = sum(1 for _master, proxy in pairs if proxy)
        self.set_status(
            f"Sending {len(pairs)} clip{'s' if len(pairs) != 1 else ''} "
            f"to Resolve…", T.DIM)
        threading.Thread(target=self._run_send_to_resolve,
                         args=(pairs, premium, bin_name or self.RESOLVE_BIN),
                         daemon=True).start()

    def _run_send_to_resolve(self, pairs: list, premium: int, bin_name: str) -> None:
        from .resolve_api import import_clips
        ok, message = import_clips(pairs, bin_name=bin_name)
        self.ui(self._sent_to_resolve, ok, message, premium)

    def _sent_to_resolve(self, ok: bool, message: str, premium: int) -> None:
        if ok and premium:
            message += (f" {premium} came from the 4K pool."
                        if premium != 1 else " It came from the 4K pool.")
        # The failure text is a multi-line checklist; the status bar is one
        # line, so it gets the headline and the toast carries the rest.
        self.set_status(message.split("\n")[0], T.OK if ok else T.FAIL)
        self.toaster.show(message, "ok" if ok else "fail",
                          ms=5000 if ok else 12000)

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
        menu = popup_menu(self.root)
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

