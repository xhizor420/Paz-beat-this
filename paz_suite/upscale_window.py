"""The clips that still need a 4K 60 pass, per folder, ready to send to Topaz.

The right side is a browser of thumbnails in three sections - needs 60
fps, needs 4K, needs both - because that is what decides Topaz's
settings: one batch of interpolation, one of upscaling, one of both.
Inside a section, groups by resolution (the shorter side, so a portrait
1080x1920 sits with 1080p), highest frame rate first. Every heading's
"Select all" takes that whole batch in one click; Copy (Ctrl+C) or a
drag hands the files to Topaz. Nothing here tracks what was sent:
Topaz itself refuses a clip it already has.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk
from PIL import Image, ImageTk

from . import media, uithread, watchdog
from .convert_engine import clips_needing_upscale, measured
from .filedrag import copy_files, drag_files
from .files import open_in_explorer
from .format import fmt_len, fmt_size
from .tag_rail import fit_text
from .theme import T, font, label_style, pt, px, window_size
from .widgets import PrimaryButton, menu_rule, popup_menu, tell, tip

# Card measured at 100%. px() grows it with the rest of the window.
_CARD_W = 236
_IMG_H = 133
_TEXT_H = 76
_GAP = 12
_PAD = 12
_INSET = 8
_HEAD_H = 36

_HINT = ("Select all on a group, or click and Shift-click clips - then Copy "
         "and paste into Topaz, or drag them onto it. Double-click shows a "
         "clip in its folder.")


# ── words ────────────────────────────────────────────────────────────────

def _stats(clip) -> str:
    """Length and size, the two numbers that say how long an upscale takes."""
    length = fmt_len(clip.duration) if clip.duration else ""
    size = fmt_size(clip.size) if clip.size else ""
    if length and size:
        return f"{length} · {size}"
    return length or size or "—"


def _fps_text(fps: float) -> str:
    """30, 60, 23.98 - whole rates without the decimals."""
    if abs(fps - round(fps)) < 0.05:
        return f"{round(fps)} fps"
    return f"{fps:.2f} fps"


def _spec(clip, measuring: bool = False) -> str:
    """Width x height and frame rate."""
    bits = []
    if clip.width and clip.height:
        bits.append(f"{clip.width} × {clip.height}")
    elif clip.height:
        bits.append(f"{clip.height}p")
    if clip.fps:
        bits.append(_fps_text(clip.fps))
    if bits:
        return " · ".join(bits)
    return "Measuring…" if measuring else "Size unknown"


def resolution(clip) -> int:
    """The short side - what "1080p" means for a portrait clip as much as
    a landscape one, and what Sorting's Minimum short side measures."""
    width, height = int(clip.width or 0), int(clip.height or 0)
    if width and height:
        return min(width, height)
    return height


# The bands a group is one of. Exact sizes made twenty groups of a clip
# or two each - 1080, 1072, 1088 - and a group's Select all was no use.
# A band starts a little under its number, since a 1072 or 2152 is a
# cropped 1080 or 4K (1072, 2152), not the size below. Only a little:
# a 2000x2000 is not 4K, and must not sit under a 4K heading.
BANDS = (2160, 1440, 1080, 720, 480)
BAND_SLACK = 0.97


def band(clip) -> int:
    """The band a clip's short side falls in: 2160, 1440, 1080, 720, 480,
    1 for anything smaller, 0 when not measured yet."""
    side = resolution(clip)
    if not side:
        return 0
    return next((b for b in BANDS if side >= b * BAND_SLACK), 1)


def _height_label(height: int) -> str:
    if not height:
        return "Not measured"
    if height == 1:
        return "Under 480p"
    return "4K" if height == 2160 else f"{height}p"


def grouped(clips: list) -> list[tuple[int, list]]:
    """(band, clips) groups, highest first; in each, highest frame rate
    first, then the larger picture, then name. Not measured comes last."""
    groups: dict[int, list] = {}
    for clip in clips:
        groups.setdefault(band(clip), []).append(clip)
    out = []
    for height in sorted(groups, key=lambda h: (h == 0, -h)):
        members = sorted(groups[height],
                         key=lambda c: (-float(c.fps or 0), -resolution(c),
                                        c.name.lower()))
        out.append((height, members))
    return out


# What Topaz has to do, in the order the sections come: interpolation
# only, upscaling only, both. The values are GapClip.need.
NEEDS = ("60 fps", "4K", "4K and 60")


def need_title(need: str) -> str:
    return f"Needs {need}" if need else "Not measured yet"


def sections(clips: list) -> list[tuple[str, list]]:
    """[(need, [(band, clips), ...]), ...] - a section per kind of Topaz
    job, each grouped by resolution (see grouped). Clips not measured
    yet, whose need is unknown, come last."""
    by_need: dict[str, list] = {}
    for clip in clips:
        by_need.setdefault(clip.need or "", []).append(clip)
    order = [need for need in NEEDS if need in by_need]
    order += sorted(need for need in by_need if need and need not in NEEDS)
    if "" in by_need:
        order.append("")
    return [(need, grouped(by_need[need])) for need in order]


def _library_details(tab) -> dict:
    """Path -> (width, height, fps, duration) from the library, if it is loaded."""
    library = getattr(getattr(tab, "app", None), "library", None)
    records = getattr(library, "records", None) or []
    details = {}
    for rec in records:
        path = getattr(rec, "path", "")
        if path:
            details[os.path.normcase(path)] = (
                getattr(rec, "width", 0) or 0,
                getattr(rec, "height", 0) or 0,
                getattr(rec, "fps", 0) or 0,
                getattr(rec, "duration", 0) or 0,
            )
    return details


class UpscaleWindow(ctk.CTkToplevel):
    """The four folders, and the clips in each that are not 4K 60 yet."""

    def __init__(self, parent, tab):
        super().__init__(parent)
        self.tab = tab
        self.title("Needs 4K 60")
        self.geometry(window_size(self, 1280, 840))
        self.configure(fg_color=T.BG)
        self.minsize(860, 520)
        self.transient(parent)
        self.after(120, self.lift)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._clips: dict = {}
        self._folder = ""
        self._buttons: dict = {}
        self._drag_at = None
        self._dragging = False
        self._did_drag = False
        self._pressed = ""
        self._drag_ok = False
        self._user_pick = False
        self._selected: set[str] = set()
        self._anchor = ""
        self._order: list[str] = []
        self._slots: dict = {}
        self._links: list = []          # (box, paths) per group heading
        self._photos: dict = {}
        self._blank = None
        self._blank_size = (0, 0)
        self._thumb_gen = 0
        self._canvas_w = 0
        self._filter_job = None
        self._copied_job = None
        self._measuring: set[str] = set()
        self._alive = True
        self._name_font = tkfont.Font(family=T.UI, size=pt(11))
        self._spec_font = tkfont.Font(family=T.UI, size=pt(12), weight="bold")
        self._stat_font = tkfont.Font(family=T.UI, size=pt(11))
        self._need_font = tkfont.Font(family=T.UI, size=pt(10))
        self._section_font = tkfont.Font(family=T.UI, size=pt(14), weight="bold")
        self._head_font = tkfont.Font(family=T.UI, size=pt(12), weight="bold")
        self._link_font = tkfont.Font(family=T.UI, size=pt(11))

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(16, 10))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Needs 4K 60", font=font(16),
                     text_color=T.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self.count = ctk.CTkLabel(head, text="Looking through the folders",
                                  font=font(11), text_color=T.FAINT, anchor="e")
        self.count.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            head, text="By what Topaz has to do, then resolution - highest "
                       "frame rate first. Each heading selects its whole batch.",
            font=font(11), text_color=T.DIM, anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        side = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_LG,
                            border_width=1, border_color=T.LINE)
        side.grid(row=1, column=0, sticky="nsew", padx=(14, 8), pady=(0, 8))
        ctk.CTkLabel(side, text="FOLDERS", **label_style()).pack(
            anchor="w", padx=14, pady=(14, 8))
        self._side = side
        for folder in tab.cfg.subfolders:
            self._add_folder(folder)

        card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_LG,
                            border_width=1, border_color=T.LINE)
        card.grid(row=1, column=1, sticky="nsew", padx=(0, 14), pady=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 8))
        bar.grid_columnconfigure(0, weight=1)
        self.folder_title = ctk.CTkLabel(
            bar, text="", font=font(13), text_color=T.TEXT, anchor="w")
        self.folder_title.grid(row=0, column=0, sticky="w")
        self.filter = ctk.CTkEntry(
            bar, placeholder_text="Filter", width=170, height=30,
            font=font(11), corner_radius=T.R_SM, fg_color=T.INPUT,
            border_color=T.LINE, border_width=1, text_color=T.TEXT)
        self.filter.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.filter.bind("<KeyRelease>", self._filter_soon)
        # The window's one main action, so the filled button - in the
        # window's own orange.
        self.copy_btn = PrimaryButton(
            bar, T.WARN, text="Copy", width=140, height=30, corner_radius=T.R_SM,
            command=self._copy, state="disabled")
        self.copy_btn.grid(row=0, column=2, sticky="e")
        tip(self.copy_btn,
            "Puts the selected clips on the clipboard, like Copy in Explorer. "
            "Paste them into Topaz with Ctrl+V. (Ctrl+C)")

        self.canvas = tk.Canvas(
            card, bg=T.SURFACE, highlightthickness=0, bd=0, takefocus=1)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(0, 10))
        scroll = ctk.CTkScrollbar(card, command=self.canvas.yview, width=12,
                                  button_color=T.LINE, button_hover_color=T.FAINT)
        scroll.grid(row=1, column=1, sticky="ns", padx=(2, 8), pady=(0, 10))
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<ButtonPress-1>", self._drag_press)
        self.canvas.bind("<B1-Motion>", self._drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._drag_release)
        self.canvas.bind("<Double-1>", self._open)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Motion>", self._hover)
        # On the window, so they work wherever focus is - except in the
        # filter box, where Ctrl+A and Ctrl+C belong to the text.
        for key in ("<Control-a>", "<Control-A>"):
            self.bind(key, self._select_all)
        for key in ("<Control-c>", "<Control-C>"):
            self.bind(key, self._copy_key)
        self.bind("<Escape>", self._escape)

        self.hint = ctk.CTkLabel(self, text=_HINT, font=font(11),
                                 text_color=T.FAINT, anchor="w")
        self.hint.grid(row=2, column=0, columnspan=2, sticky="ew",
                       padx=18, pady=(0, 14))

        if tab.cfg.subfolders:
            self._show(tab.cfg.subfolders[0])
        threading.Thread(target=self._scan, daemon=True, name="upscale-list").start()

    def destroy(self):
        self._alive = False
        self._thumb_gen += 1
        super().destroy()

    def _in_filter(self) -> bool:
        """Whether typing is going to the filter box. Focus lands on the
        tk Entry inside the CTkEntry, never on the CTkEntry itself."""
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            return False
        return focus is not None and str(focus).startswith(str(self.filter))

    def _add_folder(self, folder: str) -> None:
        row = ctk.CTkFrame(self._side, fg_color="transparent", corner_radius=T.R_SM,
                           height=40)
        row.pack(fill="x", padx=8, pady=2)
        row.pack_propagate(False)
        name = ctk.CTkLabel(row, text=folder, font=font(13), text_color=T.TEXT, anchor="w")
        name.pack(side="left", padx=(12, 8))
        count = ctk.CTkLabel(row, text="", font=font(12), text_color=T.FAINT, anchor="e")
        count.pack(side="right", padx=(8, 12))
        for widget in (row, name, count):
            widget.bind("<Button-1>", lambda _e, f=folder: self._pick(f))
        self._buttons[folder] = (row, name, count)

    def _mark_folder(self, folder: str) -> None:
        for name, (row, label, count) in self._buttons.items():
            on = name == folder
            row.configure(fg_color=T.WARN_DEEP if on else "transparent")
            label.configure(text_color=T.TEXT)
            count.configure(text_color=T.WARN if on else T.FAINT)

    # ── finding and measuring ────────────────────────────────────────────

    def _scan(self) -> None:
        cfg = self.tab.cfg
        try:
            found = clips_needing_upscale(cfg, _library_details(self.tab))
        except OSError:
            found = {folder: [] for folder in cfg.subfolders}
        unknown = [clip for clips in found.values() for clip in clips
                   if not (clip.height or clip.fps)]
        uithread.post(self._scanned, found, {clip.path for clip in unknown})
        self._measure(unknown)

    def _measure(self, clips: list) -> None:
        """ffprobe the clips the library had no numbers for, a few at a
        time back to the window. The folder on screen goes first."""
        current = self._folder
        clips.sort(key=lambda clip: clip.folder != current)
        cfg = self.tab.cfg
        batch: dict = {}
        sent = time.monotonic()
        for clip in clips:
            if not self._alive:
                return
            info = media.probe(clip.path)
            if info is None:
                batch[clip.path] = clip
            else:
                batch[clip.path] = measured(cfg, clip, info.width, info.height,
                                            info.fps, info.duration)
            if len(batch) >= 16 or time.monotonic() - sent > 0.5:
                uithread.post(self._measured, batch)
                batch, sent = {}, time.monotonic()
        uithread.post(self._measured, batch)

    def _scanned(self, found: dict, measuring: set) -> None:
        if not self._alive:
            return
        self._clips = found
        self._measuring = set(measuring)
        self._update_counts()
        pick = self._folder
        if not self._user_pick:
            pick = next((folder for folder in self.tab.cfg.subfolders
                         if found.get(folder)), pick)
        if pick:
            self._show(pick)

    def _measured(self, batch: dict) -> None:
        """Fill in ffprobe's numbers. A clip that turns out to be 4K 60
        already (None) leaves the list."""
        if not self._alive:
            return
        touched = False
        for path, clip in batch.items():
            self._measuring.discard(path)
            for folder, clips in self._clips.items():
                for index, old in enumerate(clips):
                    if old.path != path:
                        continue
                    if clip is None:
                        del clips[index]
                    else:
                        clips[index] = clip
                    touched = touched or folder == self._folder
                    break
        self._update_counts()
        if touched:
            # Regrouped in place: the scroll position and the selection
            # stay where they are, and thumbnails already made are reused.
            self._refresh()

    def _update_counts(self) -> None:
        found = self._clips
        total = sum(len(clips) for clips in found.values())
        for folder, (_row, _label, count) in self._buttons.items():
            count.configure(text=str(len(found.get(folder) or [])))
        text = f"{total} clip{'' if total == 1 else 's'} need 4K 60"
        if self._measuring:
            text += f"  ·  measuring {len(self._measuring)}"
        self.count.configure(text=text)
        self._update_title()

    def _update_title(self) -> None:
        if not self._folder:
            return
        if not self._clips:
            self.folder_title.configure(text=self._folder)
            return
        n = len(self._clips.get(self._folder) or [])
        self.folder_title.configure(text=f"{self._folder}  ·  {n}")

    # ── what is on screen ────────────────────────────────────────────────

    def _pick(self, folder: str) -> None:
        self._user_pick = True
        self._show(folder)

    def _show(self, folder: str) -> None:
        self._folder = folder
        self._mark_folder(folder)
        self._update_title()
        self.canvas.yview_moveto(0)
        self._fill()

    def _visible(self) -> list:
        """The folder's clips that pass the filter, in on-screen order."""
        clips = list(self._clips.get(self._folder) or [])
        query = self.filter.get().strip().lower()
        if query:
            clips = [clip for clip in clips if query in clip.name.lower()]
        return [clip for _need, groups in sections(clips)
                for _height, members in groups for clip in members]

    def _filter_soon(self, _event=None) -> None:
        """Refill once typing pauses, not on every key."""
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except ValueError:
                pass
        self._filter_job = self.after(150, self._filter_now)

    def _filter_now(self) -> None:
        self._filter_job = None
        self._fill()

    def _fill(self) -> None:
        clips = self._visible()
        self._order = [clip.path for clip in clips]
        self._selected &= set(self._order)
        self._layout()
        self._selection_changed()
        self._start_thumbs(clips)

    def _refresh(self) -> None:
        """Lay out again where the view is, keeping selection and pictures."""
        self._order = [clip.path for clip in self._visible()]
        self._selected &= set(self._order)
        self._layout()
        self._selection_changed()

    def _card_box(self) -> tuple[int, int, int, int]:
        card_w = px(_CARD_W)
        img_h = px(_IMG_H)
        card_h = img_h + px(_TEXT_H) + px(_INSET)
        return card_w, card_h, img_h, px(_INSET)

    def _on_canvas_configure(self, event) -> None:
        if event.width < 20 or abs(event.width - self._canvas_w) < 2:
            return
        self._canvas_w = event.width
        self._layout()

    def _layout(self) -> None:
        clips = self._visible()
        self.canvas.delete("all")
        self._slots.clear()
        self._links = []
        width = max(self.canvas.winfo_width(), self._canvas_w, 1)
        if not clips:
            self.canvas.create_text(
                width // 2, px(80), text="Nothing here needs 4K 60.",
                fill=T.FAINT, font=self._name_font)
            self.canvas.configure(scrollregion=(0, 0, width, px(160)))
            return
        card_w, card_h, img_h, inset = self._card_box()
        gap = px(_GAP)
        pad = px(_PAD)
        head_h = px(_HEAD_H)
        # As many whole columns as fit, with an even gap, and the block
        # of them centred. Sharing the spare width out between the columns
        # opened a hole in the middle of a two-column row; packing them
        # left left a strip down the right with the headings' "Select
        # all" stranded out in it.
        cols = max(1, (width - pad * 2 + gap) // (card_w + gap))
        gap_x = gap
        grid_w = cols * card_w + (cols - 1) * gap
        left = max(pad, (width - grid_w) // 2)
        img_w = card_w - inset * 2
        text_w = card_w - inset * 2
        index = 0
        y = pad
        section_h = px(_HEAD_H + 10)
        for number, (need, groups) in enumerate(sections(clips)):
            if number:
                y += px(14)             # air between one Topaz job and the next
            everything = [clip.path for _h, members in groups for clip in members]
            n = len(everything)
            self._draw_heading(
                f"{need_title(need)}  ·  {n} clip{'' if n == 1 else 's'}",
                everything, y, left, left + grid_w, section_h, section=True)
            y += section_h
            for height, members in groups:
                paths = [clip.path for clip in members]
                m = len(paths)
                self._draw_heading(
                    f"{_height_label(height)}  ·  {m} clip{'' if m == 1 else 's'}",
                    paths, y, left, left + grid_w, head_h)
                y += head_h
                for k, clip in enumerate(members):
                    col, row = k % cols, k // cols
                    x = left + col * (card_w + gap_x)
                    top = y + row * (card_h + gap)
                    self._draw_card(clip, index, x, top, card_w, card_h,
                                    img_w, img_h, inset, text_w)
                    index += 1
                rows = (len(members) + cols - 1) // cols
                y += rows * (card_h + gap) + px(8)
        self.canvas.configure(scrollregion=(0, 0, width, y + pad))

    def _draw_heading(self, label: str, paths: list, y: int, left: int,
                      right: int, head_h: int, section: bool = False) -> None:
        """ "1080p · 12 clips" on the left, ruled across to "Select all 12"
        on the right - the whole group, one Topaz batch, in one click. A
        section heading ("Needs 60 fps · 120 clips") is the same, larger,
        in the window's orange, with a solid rule."""
        mid = y + head_h // 2
        n = len(paths)
        title_font = self._section_font if section else self._head_font
        self.canvas.create_text(left, mid, anchor="w", text=label,
                                fill=T.WARN if section else T.TEXT, font=title_font)
        everything = all(path in self._selected for path in paths)
        link = "Clear" if everything else (f"Select all {n}" if n > 1 else "Select")
        link_item = self.canvas.create_text(
            right, mid, anchor="e", text=link, fill=T.WARN,
            font=self._link_font, tags=("link",))
        x0, y0, x1, y1 = self.canvas.bbox(link_item)
        self._links.append(((x0 - px(6), y0 - px(4), x1 + px(6), y1 + px(4)), paths))
        rule_x0 = left + title_font.measure(label) + px(12)
        rule_x1 = x0 - px(12)
        if rule_x1 > rule_x0:
            self.canvas.create_line(rule_x0, mid, rule_x1, mid,
                                    fill=T.WARN_DEEP if section else T.LINE,
                                    width=2 if section else 1)

    def _draw_card(self, clip, index, x, y, card_w, card_h, img_w, img_h,
                   inset, text_w) -> None:
        on = clip.path in self._selected
        rect = self.canvas.create_rectangle(
            x, y, x + card_w, y + card_h,
            fill=T.WARN_DEEP if on else T.ROW,
            outline=T.WARN if on else T.LINE,
            width=2 if on else 1)
        photo = self._photos.get(clip.path) or self._placeholder(img_w, img_h)
        image = self.canvas.create_image(
            x + inset, y + inset, anchor="nw", image=photo)
        text_top = y + inset + img_h

        # Line one: the picture, the thing the groups are built from.
        self.canvas.create_text(
            x + inset, text_top + px(16), anchor="w",
            text=fit_text(_spec(clip, clip.path in self._measuring),
                          text_w, self._spec_font.measure),
            fill=T.TEXT, font=self._spec_font)
        # Line two: the name. Line three: length and size.
        self.canvas.create_text(
            x + inset, text_top + px(38), anchor="w",
            text=fit_text(clip.name, text_w, self._name_font.measure),
            fill=T.DIM, font=self._name_font)
        self.canvas.create_text(
            x + inset, text_top + px(58), anchor="w",
            text=fit_text(_stats(clip), text_w, self._stat_font.measure),
            fill=T.FAINT, font=self._stat_font)
        self._slots[clip.path] = {
            "rect": rect, "image": image, "index": index,
            "box": (x, y, x + card_w, y + card_h), "clip": clip,
        }

    def _placeholder(self, width: int, height: int):
        if self._blank is not None and self._blank_size == (width, height):
            return self._blank
        rgb = tuple(int(T.INPUT.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        image = Image.new("RGB", (max(width, 2), max(height, 2)), rgb)
        radius = min(px(8), width // 2, height // 2)
        self._blank = ImageTk.PhotoImage(media.round_corners(image, radius, T.ROW))
        self._blank_size = (width, height)
        return self._blank

    def _paint_selection(self) -> None:
        for path, slot in self._slots.items():
            on = path in self._selected
            self.canvas.itemconfigure(
                slot["rect"],
                fill=T.WARN_DEEP if on else T.ROW,
                outline=T.WARN if on else T.LINE,
                width=2 if on else 1)

    # ── thumbnails ───────────────────────────────────────────────────────

    def _start_thumbs(self, clips: list) -> None:
        self._thumb_gen += 1
        gen = self._thumb_gen
        _card_w, _card_h, img_h, inset = self._card_box()
        img_w = px(_CARD_W) - inset * 2
        radius = min(px(8), img_w // 2, img_h // 2)
        cfg = self.tab.cfg
        fit = getattr(cfg, "thumb_fit", "contain") or "contain"
        # The library's own width: these files are the library's
        # thumbnails too, and one made smaller here would be a soft card
        # there until the sharpen pass made it again.
        stored_w = max(int(getattr(cfg, "thumb_width", 720) or 720), img_w)
        ordered = self._visible_first(clips)
        threading.Thread(
            target=self._thumbs,
            args=(gen, ordered, img_w, img_h, radius, fit, stored_w),
            daemon=True, name="upscale-thumbs").start()

    def _visible_first(self, clips: list) -> list:
        top = self.canvas.canvasy(0)
        bottom = top + max(self.canvas.winfo_height(), 1)
        shown, rest = [], []
        for clip in clips:
            box = self._slots.get(clip.path, {}).get("box")
            if box and box[1] < bottom + px(40) and box[3] > top - px(40):
                shown.append(clip)
            else:
                rest.append(clip)
        return shown + rest

    def _thumbs(self, gen: int, clips: list, img_w: int, img_h: int,
                radius: int, fit: str, stored_w: int) -> None:
        for clip in clips:
            if gen != self._thumb_gen:
                return
            if clip.path in self._photos:
                continue
            dest = os.path.join(media.THUMB_DIR, media.thumb_key(clip.path))
            if not os.path.isfile(dest):
                media.make_thumb(clip.path, clip.duration or 0, stored_w)
            if gen != self._thumb_gen or not os.path.isfile(dest):
                continue
            try:
                with open(dest, "rb") as fh:
                    data = fh.read()
                image = media.tile_image(
                    data, img_w, img_h, fit, radius=radius, clear=True)
            except Exception:
                continue
            if gen != self._thumb_gen or image is None:
                return
            uithread.post(self._show_thumb, gen, clip.path, image)

    def _show_thumb(self, gen: int, path: str, image) -> None:
        if gen != self._thumb_gen or not self._alive:
            return
        try:
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return
        self._photos[path] = photo
        slot = self._slots.get(path)
        if not slot:
            return
        try:
            self.canvas.itemconfigure(slot["image"], image=photo)
        except Exception:
            return
        if len(self._photos) > 240:
            keep = set(self._order)
            for old in list(self._photos):
                if old not in keep:
                    self._photos.pop(old, None)

    # ── selecting ────────────────────────────────────────────────────────

    def _hit(self, event) -> str:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        for path, slot in self._slots.items():
            x0, y0, x1, y1 = slot["box"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return path
        return ""

    def _link_hit(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        for (x0, y0, x1, y1), paths in self._links:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return paths
        return None

    def _hover(self, event) -> None:
        cursor = "hand2" if self._link_hit(event) is not None else ""
        if str(self.canvas.cget("cursor")) != cursor:
            self.canvas.configure(cursor=cursor)

    def _selection_changed(self) -> None:
        n = len(self._selected)
        if self._copied_job is not None:
            return                      # "Copied" is still showing
        if n:
            self.hint.configure(
                text=f"{n} selected. Copy and paste into Topaz, or drag them onto it.")
            self.copy_btn.configure(state="normal",
                                    text=f"Copy {n} clip{'' if n == 1 else 's'}")
        else:
            self.hint.configure(text=_HINT)
            self.copy_btn.configure(state="disabled", text="Copy")

    def _selected_paths(self) -> list[str]:
        return [path for path in self._order if path in self._selected]

    def _select_group(self, paths: list, add: bool) -> None:
        """A group heading's link: the whole group, or clear it if it is
        all selected already. Ctrl adds it to what is selected."""
        if all(path in self._selected for path in paths):
            self._selected -= set(paths)
        elif add:
            self._selected |= set(paths)
        else:
            self._selected = set(paths)
        if paths:
            self._anchor = paths[0]
        self._layout()                  # the links say Clear / Select all
        self._selection_changed()

    def _drag_press(self, event) -> None:
        # Focus follows a click, not the pointer: taking it on hover pulled
        # it out of the filter box mid-word.
        self.canvas.focus_set()
        self._did_drag = False
        shift = bool(event.state & 0x0001)
        ctrl = bool(event.state & 0x0004)
        group = self._link_hit(event)
        if group is not None:
            self._drag_at = None
            self._drag_ok = False
            self._pressed = ""
            self._select_group(group, add=ctrl or shift)
            return
        path = self._hit(event)
        self._drag_at = (event.x, event.y)
        self._pressed = path
        self._drag_ok = bool(path)
        if not path:
            if not ctrl:
                self._selected.clear()
                self._paint_selection()
                self._repaint_links()
                self._selection_changed()
            return
        if shift and self._anchor in self._order and path in self._order:
            start = self._order.index(self._anchor)
            end = self._order.index(path)
            lo, hi = sorted((start, end))
            self._selected = set(self._order[lo:hi + 1])
        elif ctrl:
            if path in self._selected:
                self._selected.discard(path)
            else:
                self._selected.add(path)
            self._anchor = path
        elif path not in self._selected:
            self._selected = {path}
            self._anchor = path
        self._paint_selection()
        self._repaint_links()
        self._selection_changed()

    def _repaint_links(self) -> None:
        """Clear / Select all on each heading, after a click on a card."""
        for item, ((_box), paths) in zip(self.canvas.find_withtag("link"), self._links):
            n = len(paths)
            everything = all(path in self._selected for path in paths)
            text = "Clear" if everything else (f"Select all {n}" if n > 1 else "Select")
            self.canvas.itemconfigure(item, text=text)

    def _drag_release(self, event) -> None:
        if self._did_drag or self._dragging:
            return
        path = self._pressed
        shift = bool(event.state & 0x0001)
        ctrl = bool(event.state & 0x0004)
        if path and not shift and not ctrl and len(self._selected) > 1 and path in self._selected:
            self._selected = {path}
            self._anchor = path
            self._paint_selection()
            self._repaint_links()
            self._selection_changed()

    def _select_all(self, _event=None):
        if self._in_filter():
            return None
        self._selected = set(self._order)
        if self._order:
            self._anchor = self._order[0]
        self._paint_selection()
        self._repaint_links()
        self._selection_changed()
        return "break"

    # ── handing over ─────────────────────────────────────────────────────

    def _drag_motion(self, event) -> None:
        if self._dragging or not self._drag_at or not self._drag_ok:
            return
        x0, y0 = self._drag_at
        if abs(event.x - x0) < 12 and abs(event.y - y0) < 12:
            return
        paths = self._selected_paths()
        self._drag_at = None
        self._did_drag = True
        if not paths:
            return
        self._dragging = True
        # Windows runs the drag in its own loop until the button comes up;
        # the watchdog would log that as the window freezing.
        watchdog.modal(True)
        try:
            result = drag_files(paths)
        finally:
            watchdog.modal(False)
            self._dragging = False
        if result == "dropped":
            self._flash(f"Sent {len(paths)} clip{'' if len(paths) == 1 else 's'} to Topaz")
        elif not result:
            tell(self, "Needs 4K 60",
                 "Couldn't drag those files. Copy and paste works too.")

    def _copy(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        if copy_files(paths):
            n = len(paths)
            self._flash(f"Copied {n} clip{'' if n == 1 else 's'}. Paste into "
                        "Topaz with Ctrl+V.", button="Copied ✓")
        else:
            tell(self, "Needs 4K 60",
                 "Couldn't put those files on the clipboard. Another program "
                 "may be holding it; try again.")

    def _flash(self, text: str, button: str = "") -> None:
        """Say it happened - in the hint, and on the button for a moment."""
        if self._copied_job is not None:
            try:
                self.after_cancel(self._copied_job)
            except ValueError:
                pass
        self.hint.configure(text=text, text_color=T.OK)
        if button:
            self.copy_btn.configure(text=button)
        self._copied_job = self.after(2200, self._flash_done)

    def _flash_done(self) -> None:
        self._copied_job = None
        if not self._alive:
            return
        self.hint.configure(text_color=T.FAINT)
        self._selection_changed()

    def _copy_key(self, _event=None):
        if self._in_filter():
            return None
        self._copy()
        return "break"

    def _copy_paths_text(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(paths))
        self._flash(f"Copied {len(paths)} path{'' if len(paths) == 1 else 's'} as text.")

    def _menu(self, event) -> None:
        path = self._hit(event)
        if not path:
            return
        if path not in self._selected:
            self._selected = {path}
            self._anchor = path
            self._paint_selection()
            self._repaint_links()
            self._selection_changed()
        paths = self._selected_paths()
        n = len(paths)
        menu = popup_menu(self)
        menu.add_command(label=f"Copy {n} clip{'' if n == 1 else 's'}      Ctrl+C",
                         command=self._copy)
        menu.add_command(label="Copy paths as text", command=self._copy_paths_text)
        menu_rule(menu)
        menu.add_command(label="Show in folder",
                         command=lambda p=path: open_in_explorer(p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open(self, event) -> None:
        path = self._hit(event)
        if path:
            open_in_explorer(path)

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")

    def _escape(self, _event=None):
        if self._in_filter() and self.filter.get():
            self.filter.delete(0, "end")
            self._fill()
            return "break"
        self.destroy()
        return "break"
