"""The Proxies tab: edit proxies for every clip in the 4K/60 pool, the way
Convert makes the converted copies - scan, see what is left, press one
button, walk away.

Only the .mp4 masters sitting directly in the pool's category folders are
looked at. Each one's proxy goes in that category's own Proxy folder,
where Resolve finds it by name. What a proxy is and how one is made and
checked lives in proxy_engine; this is the window around it.

A run can be stopped at any point and started again later: a proxy is
only ever written under its real name once it has been checked, and the
ones already made are skipped without being opened - see
proxy_engine.scan.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from .theme import (T, button_style, column_width, font, label_style, pt, px, table_style,
                    unscaled)
from .format import fmt_size, fmt_time, fmt_short
from .files import open_file, open_in_explorer
from .widgets import (Bar, Card, JobPanel, PrimaryButton, StatTile, empty_overlay,
                      popup_menu, rounded_well, tip)
from . import proxy_engine as pe
from . import uithread
from .winsys import awake

ACCENT, ACCENT_DEEP = T.ACCENT5, T.ACCENT5_DEEP

STATE_TEXT = {
    pe.DONE: "Done",
    pe.NEEDED: "Needs proxy",
    pe.STALE: "4K file changed",
    pe.CHECK: "Checking",
    pe.BAD: "Doesn't match",
    pe.FAILED: "Failed",
    pe.WAITING: "Waiting",
    "making": "Making",
}
STATE_COLOUR = {
    pe.DONE: T.OK, pe.NEEDED: T.DIM, pe.STALE: T.WARN, pe.CHECK: T.FAINT,
    pe.BAD: T.FAIL, pe.FAILED: T.FAIL, pe.WAITING: T.FAINT, "making": T.ACCENT5,
}
# Opening the tab looks again if the last look is older than this - the
# pool is live, and a count from an hour ago is just a wrong number.
RESCAN_AFTER = 60.0
FILTERS = ("To do", "Failed", "Done", "All")
ALL = "All categories"
# Length assumed for a clip the library has no length for, for the
# estimate only. About the pool's average.
GUESS_SECONDS = 35.0


class ProxyTab(ctk.CTkFrame):

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG, corner_radius=0)
        self.pack(fill="both", expand=True)
        self.app = app
        self.root = app.root
        self.cfg = app.cfg
        self.items: list = []
        self._rows: dict = {}          # tree iid -> Item
        self._iid_of: dict = {}        # master path -> tree iid
        self._missing: list = []
        self._scan_gen = 0
        self._scanned = False
        self._scanned_at = 0.0
        self._scanning = False
        self._running = False
        self._cancel = threading.Event()
        self._current = None
        self._run_bytes = 0
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build()
        self._set_status("Not scanned yet", T.FAINT)

    # ── plumbing ────────────────────────────────────────────────────────

    def ui(self, fn, *args, **kwargs) -> None:
        uithread.post(fn, *args, **kwargs)

    def _set_status(self, text: str, colour: str = T.DIM) -> None:
        self.status.configure(text=text, text_color=colour)
        self.pill.configure(text=text if self._running else "",
                            text_color=T.DIM, fg_color=ACCENT)

    # ── layout ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.pill = self.app.status_slot("Proxies", "pill")
        panel = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        panel.grid(row=1, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(6, weight=1)

        controls = Card(panel, border_color=ACCENT_DEEP)
        controls.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        controls.grid_columnconfigure(1, weight=1)

        picker = ctk.CTkFrame(controls, fg_color="transparent")
        picker.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=13)
        ctk.CTkLabel(picker, text="CATEGORY", **label_style()).pack(anchor="w", pady=(0, 5))
        self.category = ctk.CTkOptionMenu(
            picker, values=[ALL] + list(self.cfg.subfolders), width=170, height=32,
            font=font(11), corner_radius=T.R_SM, fg_color=T.INPUT, button_color=T.LINE,
            button_hover_color=T.BTN_HOV, dropdown_fg_color=T.ELEVATED,
            dropdown_hover_color=ACCENT_DEEP, dropdown_text_color=T.TEXT,
            dropdown_font=font(11), text_color=T.TEXT, command=lambda _v: self._refill())
        self.category.set(ALL)
        self.category.pack(anchor="w")

        about = ctk.CTkFrame(controls, fg_color="transparent")
        about.grid(row=0, column=1, sticky="w", padx=18, pady=13)
        ctk.CTkLabel(about, text="PROXY", **label_style()).pack(anchor="w", pady=(0, 5))
        recipe = ctk.CTkLabel(about, text=pe.RECIPE_LABEL, font=font(11),
                              text_color=T.DIM, anchor="w")
        recipe.pack(anchor="w")
        tip(recipe, "Every frame of the 4K file at its own frame rate, 1920 on the long "
                    "side, the sound copied as it is. Saved as Proxy\\<name>.mov next to "
                    "the 4K file, where Resolve looks for it.")

        buttons = ctk.CTkFrame(controls, fg_color="transparent")
        buttons.grid(row=0, column=2, sticky="e", padx=(0, 14), pady=13)
        self.link_btn = ctk.CTkButton(
            buttons, text="Link in Resolve", width=130, height=38, corner_radius=T.R_SM,
            command=lambda: self.link_in_resolve(quiet=False), **button_style("secondary"))
        self.link_btn.pack(side="left", padx=(0, 8))
        tip(self.link_btn, "Give every clip in the open Resolve project its proxy - "
                           "clips dragged in by hand and ones added before their "
                           "proxy was made. Also done on its own as proxies are made.")
        self.scan_btn = ctk.CTkButton(
            buttons, text="Scan", width=96, height=38, corner_radius=T.R_SM,
            command=self.scan, **button_style("secondary"))
        self.scan_btn.pack(side="left", padx=(0, 8))
        tip(self.scan_btn, "Look for clips without a proxy  ·  F5")
        self.stop_btn = ctk.CTkButton(
            buttons, text="Stop", width=84, height=38, corner_radius=T.R_SM,
            state="disabled", command=self.stop, **button_style("secondary"))
        self.stop_btn.pack(side="left", padx=(0, 8))
        tip(self.stop_btn, "Stop after throwing away the one being made. "
                           "Start carries on from there.")
        self.start_btn = PrimaryButton(
            buttons, ACCENT, text="Make proxies", width=150, height=38,
            corner_radius=T.R_SM, command=self.start)
        self.start_btn.pack(side="left")

        tiles = ctk.CTkFrame(panel, fg_color="transparent")
        tiles.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.tiles = {}
        for i, (key, label, colour) in enumerate((
                ("clips", "4K clips", T.TEXT), ("done", "Have a proxy", T.OK),
                ("todo", "Need one", ACCENT), ("failed", "Failed", T.FAIL),
                ("written", "Written this run", T.ACCENT2), ("eta", "Time left", T.DIM))):
            tiles.grid_columnconfigure(i, weight=1)
            tile = StatTile(tiles, label, colour)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            self.tiles[key] = tile

        self.per_category = ctk.CTkLabel(panel, text="", font=font(10, mono=True),
                                         text_color=T.DIM, anchor="w", justify="left")
        self.per_category.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 6))

        self.overall = Bar(panel, height=4, color=ACCENT)
        self.overall.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.jobs = JobPanel(panel)
        self.jobs.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 8))

        bar = ctk.CTkFrame(panel, fg_color="transparent")
        bar.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 6))
        bar.grid_columnconfigure(1, weight=1)
        self.filter = ctk.CTkSegmentedButton(
            bar, values=list(FILTERS), command=lambda _v: self._refill(), font=font(11),
            height=28, corner_radius=T.R_SM, fg_color=T.INPUT, selected_color=ACCENT_DEEP,
            selected_hover_color=ACCENT_DEEP, unselected_color=T.INPUT,
            unselected_hover_color=T.BTN_HOV, text_color=T.TEXT)
        self.filter.set("To do")
        self.filter.grid(row=0, column=0, sticky="w")
        self.status = ctk.CTkLabel(bar, text="", font=font(10), text_color=T.DIM,
                                   anchor="e", justify="right")
        self.status.grid(row=0, column=1, sticky="e")

        # The list, and beside it the clip being made - or the one clicked
        # on - as a picture. A file name alone says nothing about which clip.
        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.grid(row=6, column=0, sticky="nsew", padx=14, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        wrap = ctk.CTkFrame(body, fg_color=T.SURFACE, corner_radius=T.R_LG,
                            border_width=1, border_color=ACCENT_DEEP)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)
        table_style(self, "P.Treeview", 26)
        columns = ("category", "name", "len", "state", "note")
        self.tree = ttk.Treeview(wrap, style="P.Treeview", columns=columns,
                                 show="headings", selectmode="browse")
        empty_overlay(self.tree, "check", "Nothing here")
        for key, title, width, anchor, stretch in (
                ("category", "Category", 110, "w", False), ("name", "File", 170, "w", False),
                ("len", "Len", 60, "e", False), ("state", "Proxy", 120, "w", False),
                ("note", "Note", 300, "w", True)):
            self.tree.column(key, width=column_width(title, width), minwidth=px(50),
                             anchor=anchor, stretch=stretch)
            self.tree.heading(key, text=title, anchor=anchor)
        for state, colour in STATE_COLOUR.items():
            self.tree.tag_configure(state, foreground=colour)
        rounded_well(self.tree, row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = ctk.CTkScrollbar(wrap, command=self.tree.yview, width=12,
                                  button_color=T.LINE, button_hover_color=T.FAINT)
        scroll.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=8)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self._open_selected())
        self.tree.bind("<Button-3>", self._menu)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self._build_side(body)

    # ── the clip beside the list ─────────────────────────────────────────

    # The picture takes this share of the row beside the list, within these
    # widths, and is 16:9. Sized from the room there is rather than fixed,
    # so it is big on a big window without pushing a small one off screen.
    PIC_SHARE = 0.50
    PIC_MIN, PIC_MAX = 320, 1400
    PIC_W, PIC_H = 400, 225            # until the window has a size

    def _build_side(self, parent) -> None:
        card = Card(parent, border_color=ACCENT_DEEP)
        card.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        card.grid_columnconfigure(0, weight=1)
        self.side_head = ctk.CTkLabel(card, text="CLIP", **label_style(), anchor="w")
        self.side_head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        holder = tk.Frame(card, bg=T.INPUT, width=px(self.PIC_W), height=px(self.PIC_H),
                          highlightthickness=0, bd=0)
        holder.grid(row=1, column=0, padx=14)
        holder.grid_propagate(False)
        holder.pack_propagate(False)
        self._pic_box = holder
        self._pic_size = (px(self.PIC_W), px(self.PIC_H))
        parent.bind("<Configure>", self._fit_pic, add="+")
        self.side_pic = tk.Label(holder, bg=T.INPUT, bd=0, fg=T.FAINT,
                                 font=(T.UI, pt(10)), text="Pick a clip in the list")
        self.side_pic.place(relx=0.5, rely=0.5, anchor="center")
        self.side_name = ctk.CTkLabel(card, text="", font=font(13, "bold"),
                                      text_color=T.TEXT, anchor="w")
        self.side_name.grid(row=2, column=0, sticky="ew", padx=14, pady=(10, 2))
        self.side_info = ctk.CTkLabel(card, text="", font=font(10, mono=True),
                                      text_color=T.DIM, anchor="w", justify="left",
                                      wraplength=self.PIC_W)
        self.side_info.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 12))
        self._side_item = None
        self._side_token = 0
        self._pics: dict = {}          # master path -> (PIL image, Stream)
        self._side_photo = None

    def _fit_pic(self, event) -> None:
        width = int(max(px(self.PIC_MIN), min(px(self.PIC_MAX), event.width * self.PIC_SHARE)))
        size = (width, width * 9 // 16)
        if size == self._pic_size:
            return
        self._pic_size = size
        try:
            self._pic_box.configure(width=size[0], height=size[1])
            self.side_info.configure(wraplength=unscaled(size[0]))
        except tk.TclError:
            return
        item = self._side_item
        hit = self._pics.get(item.master) if item is not None else None
        if hit is not None:
            self._place_pic(self._side_token, item, *hit)

    def _on_select(self) -> None:
        if self._running:
            return                     # the panel follows the encode then
        item = self._selected()
        if item is not None:
            self._show_side(item, "SELECTED")

    def _show_side(self, item, heading: str) -> None:
        self._side_item = item
        self._side_token += 1
        token = self._side_token
        self.side_head.configure(text=heading)
        self.side_name.configure(text=f"{item.category} / {item.name}")
        self._side_text(item, None)
        hit = self._pics.get(item.master)
        if hit is not None:
            self._place_pic(token, item, *hit)
            return
        self.side_pic.configure(image="", text="Loading…")
        threading.Thread(target=self._load_pic, args=(item, token), daemon=True,
                         name="proxy-pic").start()

    def _side_text(self, item, info) -> None:
        lines = []
        if info is not None:
            bits = [f"{info.width}x{info.height}", f"{info.fps:.3g} fps", info.codec.upper()]
            if info.duration:
                bits.append(fmt_short(info.duration))
            lines.append("  ·  ".join(bits))
            lines.append(f"sound: {info.audio_codec.upper() if info.has_audio else 'none'}"
                         f"  ·  4K file {fmt_size(item.size)}")
        else:
            lines.append(f"4K file {fmt_size(item.size)}")
        state = STATE_TEXT.get(item.state, item.state)
        if item.state == pe.DONE and item.proxy_size:
            state += f"  ·  proxy {fmt_size(item.proxy_size)}"
        lines.append(f"proxy: {state}")
        if item.reason:
            lines.append(item.reason)
        lines.append(f"→ {os.path.basename(os.path.dirname(item.proxy))}\\"
                     f"{os.path.basename(item.proxy)}")
        self.side_info.configure(text="\n".join(lines))

    def _load_pic(self, item, token: int) -> None:
        """One frame a third of the way in, off the UI thread - a 4K file
        takes a moment to open and decode."""
        import io
        import subprocess
        from PIL import Image
        from .files import NO_WINDOW
        info = pe.probe(item.master)
        image = None
        at = (info.duration if info and info.duration else item.seconds or 3) / 3
        try:
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", item.master,
                 "-frames:v", "1", "-vf", "scale='min(1600,iw)':-2",
                 "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                capture_output=True, timeout=30, creationflags=NO_WINDOW)
            if out.stdout:
                # Kept large; fitted to the box when it is shown, so a
                # resize does not mean decoding the 4K file again.
                image = Image.open(io.BytesIO(out.stdout))
                image.load()
        except Exception:
            image = None
        self._pics[item.master] = (image, info)
        if len(self._pics) > 60:
            self._pics.pop(next(iter(self._pics)))
        self.ui(self._place_pic, token, item, image, info)

    def _place_pic(self, token: int, item, image, info) -> None:
        if token != self._side_token:
            return
        self._side_text(item, info)
        if image is None:
            self.side_pic.configure(image="", text="No picture")
            return
        from PIL import ImageTk
        from .media import fit_frame
        self._side_photo = ImageTk.PhotoImage(fit_frame(image, *self._pic_size, "contain"))
        self.side_pic.configure(image=self._side_photo, text="")

    # ── tab lifecycle ───────────────────────────────────────────────────

    def on_shown(self) -> None:
        if self._running or self._scanning:
            return
        if not self._scanned or time.monotonic() - self._scanned_at > RESCAN_AFTER:
            self.scan()

    def key_scan(self, event=None) -> None:
        self.scan()

    def key_stop(self) -> None:
        self.stop()

    def on_app_close(self) -> bool:
        # Nothing to ask: a stopped run loses only the proxy being made,
        # and its .partial file is cleared by the next scan.
        self._cancel.set()
        return True

    # ── scanning ────────────────────────────────────────────────────────

    def _categories(self) -> list:
        return list(self.cfg.subfolders)

    def scan(self) -> None:
        if self._running or self._scanning:
            return
        root = self.cfg.premium_root
        if not root or not os.path.isdir(root):
            self._set_status("No 4K pool folder set - pick it in Settings → Library.",
                             T.WARN)
            return
        self._scanning = True
        self._scan_gen += 1
        gen = self._scan_gen
        self.scan_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self._set_status("Scanning the 4K folders…", ACCENT)
        threading.Thread(target=self._scan_work, args=(gen, root, self._categories()),
                         daemon=True, name="proxy-scan").start()

    def _scan_work(self, gen: int, root: str, categories: list) -> None:
        try:
            items, missing = self._look(root, categories,
                                        lambda found, gone: self.ui(
                                            self._scan_listed, gen, found, gone))
        except Exception as exc:              # a scan must never leave the tab stuck
            self.ui(self._set_status, f"Scan failed: {exc}", T.FAIL)
        self.ui(self._scan_finished, gen)

    def _look(self, root: str, categories: list, listed) -> tuple:
        """List the pool, show it (`listed`), then check any proxy this app
        does not know yet. Worker thread. Returns (items, missing)."""
        from .library_db import db_connect, proxy_record, proxy_rows
        conn = db_connect()
        try:
            known = proxy_rows(conn)
            durations = {os.path.splitext(name)[0]: duration for name, duration
                         in conn.execute("SELECT name, duration FROM files")}
        finally:
            conn.close()
        items, partials, missing = pe.scan(root, categories, known, durations)
        if not self._running:
            # Not while a run is writing one: its .partial is in use.
            pe.clean_partials(partials)
        listed(items, missing)
        # Proxies this app does not know yet - put there by hand, or made
        # before the database was. Checked by their headers, one by one.
        unknown = [item for item in items if item.state == pe.CHECK]
        if unknown:
            conn = db_connect()
            try:
                for done, item in enumerate(unknown, 1):
                    if self._running and self._cancel.is_set():
                        break
                    state, reason = pe.check_existing(item)
                    made = pe.probe(item.proxy) if state == pe.DONE else None
                    proxy_record(conn, item.master,
                                 pe.record_fields(item, state, reason, made))
                    self.ui(self._item_changed, item, state, reason)
                    if done % 25 == 0:
                        self.ui(self._set_status,
                                f"Checking proxies already there · {done} of "
                                f"{len(unknown)}", ACCENT)
            finally:
                conn.close()
        return items, missing

    def _scan_listed(self, gen: int, items: list, missing: list) -> None:
        if gen != self._scan_gen:
            return
        self.items = items
        self._missing = missing
        self._refill()
        self._update_tiles()

    def _scan_finished(self, gen: int) -> None:
        if gen != self._scan_gen:
            return
        self._scanning = False
        self._scanned = True
        self._scanned_at = time.monotonic()
        self.scan_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        todo = sum(1 for item in self.items if item.state in pe.TODO)
        if self._missing:
            self._set_status("Not found: " + ", ".join(self._missing), T.WARN)
        elif todo:
            self._set_status(f"{todo:,} clip{'s' if todo != 1 else ''} need a proxy.", T.DIM)
        else:
            self._set_status("Every clip has a proxy.", T.OK)

    # ── the table ───────────────────────────────────────────────────────

    def _wanted(self, item) -> bool:
        category = self.category.get()
        if category != ALL and item.category != category:
            return False
        view = self.filter.get()
        if view == "To do":
            return item.state in pe.TODO or item.state in (pe.CHECK, pe.WAITING, "making")
        if view == "Failed":
            return item.state in (pe.FAILED, pe.BAD)
        if view == "Done":
            return item.state == pe.DONE
        return True

    def _values(self, item) -> tuple:
        return (item.category, item.name, fmt_short(item.seconds) if item.seconds else "—",
                STATE_TEXT.get(item.state, item.state), item.reason)

    def _refill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows, self._iid_of = {}, {}
        for index, item in enumerate(self.items):
            if not self._wanted(item):
                continue
            iid = f"x{index}"
            self._rows[iid] = item
            self._iid_of[item.master] = iid
            self.tree.insert("", "end", iid=iid, values=self._values(item),
                             tags=(item.state,))
        self._update_tiles()

    def _item_changed(self, item, state: str, reason: str = "") -> None:
        item.state, item.reason = state, reason
        if self._side_item is not None and self._side_item.master == item.master:
            hit = self._pics.get(item.master)
            if state == pe.DONE:
                try:
                    item.proxy_size = os.path.getsize(item.proxy)
                except OSError:
                    pass
            self._side_text(item, hit[1] if hit else None)
            if state != "making" and self._running:
                self.side_head.configure(text="JUST MADE" if state == pe.DONE
                                         else STATE_TEXT.get(state, state).upper())
        iid = self._iid_of.get(item.master)
        if iid is not None:
            try:
                self.tree.item(iid, values=self._values(item), tags=(state,))
            except tk.TclError:
                pass
        self._update_tiles()

    def _update_tiles(self) -> None:
        items = self.items
        done = sum(1 for i in items if i.state == pe.DONE)
        failed = sum(1 for i in items if i.state in (pe.FAILED, pe.BAD))
        todo = sum(1 for i in items if i.state in pe.TODO or i.state == "making")
        self.tiles["clips"].set(f"{len(items):,}")
        self.tiles["done"].set(f"{done:,}")
        self.tiles["todo"].set(f"{todo:,}")
        self.tiles["failed"].set(f"{failed:,}")
        self.tiles["written"].set(fmt_size(self._run_bytes) if self._run_bytes else "0")
        bits = []
        for category in self._categories():
            mine = [i for i in items if i.category == category]
            if mine:
                have = sum(1 for i in mine if i.state == pe.DONE)
                bits.append(f"{category} {have:,} / {len(mine):,}")
        self.per_category.configure(text="   ·   ".join(bits))

    # ── running ─────────────────────────────────────────────────────────

    def _queue(self, items=None, category=None, skip=()) -> list:
        category = self.category.get() if category is None else category
        return [item for item in (self.items if items is None else items)
                if item.state in pe.TODO and item.master not in skip
                and (category == ALL or item.category == category)]

    def start(self) -> None:
        if self._running or self._scanning:
            return
        if not self._scanned:
            self.scan()
            return
        queue = self._queue()
        if not queue:
            self._set_status("Nothing to make - every clip shown has a proxy.", T.OK)
            return
        self._running = True
        self._cancel.clear()
        self._run_bytes = 0
        self.start_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.category.configure(state="disabled")
        awake.hold("proxies")
        threading.Thread(target=self._run, args=(queue, self.category.get(),
                                                 self.cfg.premium_root, self._categories()),
                         daemon=True, name="proxy-run").start()

    def stop(self) -> None:
        if self._running:
            self._cancel.set()
            self.stop_btn.configure(state="disabled")
            self._set_status("Stopping…", T.WARN)

    def _run(self, queue: list, category: str, root: str, categories: list) -> None:
        """Work through `queue`, then look at the pool again and carry on
        with whatever arrived meanwhile, until a look finds nothing new.
        Each clip is tried once per run."""
        made = failed = 0
        tried: set = set()
        try:
            while queue and not self._cancel.is_set():
                one_made, one_failed = self._run_queue(queue, tried)
                made += one_made
                failed += one_failed
                if self._cancel.is_set():
                    break
                self.ui(self._set_status, "Looking for clips that arrived meanwhile…", ACCENT)
                gen = self._scan_gen
                items, _missing = self._look(root, categories, lambda found, gone: self.ui(
                    self._scan_listed, gen, found, gone))
                queue = self._queue(items, category, tried)
        except Exception as exc:
            self.ui(self._set_status, f"Stopped by an error: {exc}", T.FAIL)
        finally:
            awake.release("proxies")
            self.ui(self._run_finished, made, failed, self._cancel.is_set())

    def _run_queue(self, queue: list, tried: set) -> tuple:
        from .convert_engine import Cancelled
        from .library_db import db_connect, proxy_record
        total = sum(i.seconds or GUESS_SECONDS for i in queue)
        finished_seconds = 0.0
        started = time.monotonic()
        made = failed = 0
        for number, item in enumerate(queue, 1):
            if self._cancel.is_set():
                break
            tried.add(item.master)
            length = item.seconds or GUESS_SECONDS
            self.ui(self._begin_item, item, number, len(queue))

            def progress(snap, item=item, length=length, base=finished_seconds):
                self.ui(self._progress, item, snap, (base + snap["fraction"] * length)
                        / max(total, 1), base + snap["fraction"] * length, total,
                        started)

            try:
                ok, reason, stream = pe.make_proxy(item, self.cfg, progress, self._cancel)
            except Cancelled:
                self.ui(self._item_changed, item, pe.NEEDED, "stopped - not made")
                break
            except Exception as exc:
                ok, reason, stream = False, f"unexpected error: {exc}", None
            finished_seconds += length
            if not ok and reason == pe.NOT_READY:
                # Still arriving: not a failure, and nothing to remember.
                self.ui(self._item_changed, item, pe.WAITING, reason)
                continue
            state = pe.DONE if ok else pe.FAILED
            conn = db_connect()
            try:
                proxy_record(conn, item.master, pe.record_fields(item, state, reason, stream))
            finally:
                conn.close()
            if ok:
                made += 1
                try:
                    size = os.path.getsize(item.proxy)
                except OSError:
                    size = 0
                self.ui(self._wrote, size)
            else:
                failed += 1
            self.ui(self._item_changed, item, state, reason)
        return made, failed

    def _begin_item(self, item, number: int, count: int) -> None:
        self._current = item
        self._item_changed(item, "making", "")
        self.jobs.start("proxy", f"{item.category} / {item.name}  ·  {number:,} of {count:,}")
        self._show_side(item, f"NOW MAKING  ·  {number:,} OF {count:,}")
        self._set_status(f"Making {number:,} of {count:,}", ACCENT)

    def _progress(self, item, snap: dict, overall: float, done_s: float, total: float,
                  started: float) -> None:
        bits = [f"{snap['fraction']:.0%}"]
        if snap.get("fps"):
            bits.append(f"{snap['fps']} fps")
        if snap.get("speed"):
            bits.append(snap["speed"])
        self.jobs.set_progress("proxy", snap["fraction"], "  ·  ".join(bits), ACCENT)
        self.overall.set(overall)
        elapsed = time.monotonic() - started
        if elapsed > 20 and done_s > 0:
            rate = done_s / elapsed               # seconds of video per second
            self.tiles["eta"].set(fmt_time((total - done_s) / rate))

    def _wrote(self, size: int) -> None:
        self._run_bytes += size
        self._made_since_link = getattr(self, "_made_since_link", 0) + 1
        if self._made_since_link >= self.LINK_EVERY:
            self._made_since_link = 0
            self.link_in_resolve()
        self._update_tiles()

    def _run_finished(self, made: int, failed: int, stopped: bool) -> None:
        self._running = False
        self._current = None
        self.jobs.clear()
        self.overall.set(0)
        self.tiles["eta"].set("—")
        self.start_btn.configure(state="normal")
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.category.configure(state="normal")
        word = "Stopped" if stopped else "Finished"
        text = f"{word}: {made:,} made"
        if failed:
            text += f", {failed:,} failed - see the Failed list"
        self._set_status(text, T.WARN if failed else T.OK)
        toaster = getattr(self.app, "toaster", None)
        if toaster is not None:
            toaster.show(f"Proxies · {text}.", "fail" if failed else "ok")
        if made:
            self._made_since_link = 0
            self.link_in_resolve()

    # ── Resolve ─────────────────────────────────────────────────────────
    #
    # Resolve plays a clip's proxy only once one is linked to it. PAZ links
    # them when it adds clips, but clips dragged in by hand, or added
    # before their proxy existed, have none - so the whole open project is
    # gone through: after a run, every LINK_EVERY proxies during one, and
    # on the button. With Prefer Proxies on in Resolve's Playback menu,
    # that is proxies all the time without linking anything by hand.

    LINK_EVERY = 25

    def _proxy_for(self, path: str) -> str:
        """The proxy for a file in Resolve, if it is a 4K pool master."""
        root = self.cfg.premium_root
        if not root or not path or not path.lower().endswith(pe.MASTER_EXT):
            return ""
        category = os.path.dirname(os.path.normpath(path))
        if os.path.normcase(os.path.dirname(category)) != \
                os.path.normcase(os.path.normpath(root)):
            return ""
        return pe.proxy_path(path)

    def link_in_resolve(self, quiet: bool = True) -> None:
        """Link proxies in the open Resolve project, on a worker. `quiet`:
        say nothing unless something was linked - Resolve not being open
        is the normal case and not worth a word."""
        if getattr(self, "_linking", False):
            return
        self._linking = True
        if not quiet:
            self.link_btn.configure(state="disabled")
            self._set_status("Linking proxies in Resolve…", ACCENT)

        def work():
            from .resolve_api import link_proxies
            try:
                ok, message, linked = link_proxies(self._proxy_for)
            except Exception as exc:
                ok, message, linked = False, f"Resolve: {exc}", 0
            self.ui(self._linked, ok, message, linked, quiet)

        threading.Thread(target=work, daemon=True, name="proxy-link").start()

    def _linked(self, ok: bool, message: str, linked: int, quiet: bool) -> None:
        self._linking = False
        self.link_btn.configure(state="normal")
        if quiet and not linked:
            return
        first = message.split("\n")[0]
        if not self._running:
            self._set_status(first, T.OK if ok else T.WARN)
        toaster = getattr(self.app, "toaster", None)
        if toaster is not None:
            toaster.show(message if ok else first, "ok" if ok else "warn",
                         ms=5000 if ok else 9000)

    # ── a row ───────────────────────────────────────────────────────────

    def _selected(self):
        selection = self.tree.selection()
        return self._rows.get(selection[0]) if selection else None

    def _open_selected(self) -> None:
        item = self._selected()
        if item is None:
            return
        open_file(item.proxy if item.state == pe.DONE else item.master)

    def _menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        item = self._rows.get(iid)
        if item is None:
            return
        menu = popup_menu(self.root, activebackground=ACCENT_DEEP, activeforeground=ACCENT)
        has_proxy = os.path.exists(item.proxy)
        if has_proxy:
            menu.add_command(label="Play the proxy", command=lambda: open_file(item.proxy))
            menu.add_command(label="Show the proxy in its folder",
                             command=lambda: open_in_explorer(item.proxy))
        menu.add_command(label="Play the 4K file", command=lambda: open_file(item.master))
        menu.add_command(label="Show the 4K file in its folder",
                         command=lambda: open_in_explorer(item.master))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
