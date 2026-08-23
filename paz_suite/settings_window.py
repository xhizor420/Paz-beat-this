"""One settings dialog for the whole suite — Convert's folders/encoding/
sorting, Library's indexing/display/player, and the shared e621 + app-wide
preferences all live here now instead of two separate windows with
overlapping fields.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .theme import T, font
from .config import AppConfig, CONFIG_PATH
from .convert_engine import GPU_ENCODERS
from .media import available_encoders
from . import beat_engine as be


class SettingsWindow(ctk.CTkToplevel):

    def __init__(self, parent, app, initial_tab: str = "Encoding"):
        super().__init__(parent)
        self.app = app
        self.cfg: AppConfig = app.cfg
        self.title("Settings")
        self.geometry("820x720")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        tabs = ctk.CTkTabview(
            self, fg_color=T.SURFACE, segmented_button_fg_color=T.INPUT,
            segmented_button_selected_color=T.ACCENT_DEEP,
            segmented_button_selected_hover_color=T.ACCENT_DEEP,
            segmented_button_unselected_color=T.INPUT,
            segmented_button_unselected_hover_color=T.BTN_HOV,
            text_color=T.TEXT, corner_radius=12)
        tabs.grid(row=0, column=0, sticky="nsew", padx=14, pady=(14, 8))
        for name in ("Convert Folders", "Encoding", "Sorting", "Library", "e621 & App"):
            tabs.add(name)

        self.fields = {}
        self._build_convert_folders(self._scrollable(tabs.tab("Convert Folders")))
        self._build_encoding(self._scrollable(tabs.tab("Encoding")))
        self._build_sorting(self._scrollable(tabs.tab("Sorting")))
        self._build_library(self._scrollable(tabs.tab("Library")))
        self._build_app(self._scrollable(tabs.tab("e621 & App")))
        self._build_footer()

        if initial_tab in ("Convert Folders", "Encoding", "Sorting", "Library", "e621 & App"):
            tabs.set(initial_tab)

    def _scrollable(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        inner = ctk.CTkScrollableFrame(
            tab, fg_color="transparent",
            scrollbar_button_color=T.LINE, scrollbar_button_hover_color=T.FAINT)
        inner.grid(row=0, column=0, sticky="nsew")
        return inner

    # ── shared field builders ────────────────────────────────────────────
    #
    # Every row in every tab goes through one of these, so the label column,
    # the control column and the explanatory text all line up down the whole
    # dialog instead of each section drifting a few pixels.

    LABEL_W = 178          # widest label is "Gallery tile width (target)"
    HINT_W = 460           # wraps inside the panel rather than off its edge

    def _label(self, parent, text: str, row: int):
        return ctk.CTkLabel(parent, text=text, font=font(11), text_color=T.DIM,
                            anchor="w", width=self.LABEL_W, justify="left"
                            ).grid(row=row, column=0, sticky="w", padx=(4, 10),
                                   pady=4)

    def _section(self, parent, text: str, row: int) -> None:
        """A caption with a hairline under it. Bare captions left the tabs
        reading as one long undifferentiated column of fields."""
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.grid(row=row, column=0, columnspan=3, sticky="ew",
                  padx=4, pady=(18 if row else 4, 8))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text=text.upper(), font=font(10, "bold"),
                     text_color=T.ACCENT2).grid(row=0, column=0, sticky="w")
        # Plain tk.Frame, not CTkFrame: CTk draws a frame as a rounded
        # rectangle on its own canvas and a 1px-tall one comes out empty.
        rule = tk.Frame(head, height=1, bg=T.LINE, bd=0, highlightthickness=0)
        rule.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=(3, 0))

    def _hint(self, parent, row: int, text: str, column: int = 1, span: int = 2) -> None:
        ctk.CTkLabel(parent, text=text, font=font(10), text_color=T.FAINT,
                     wraplength=self.HINT_W, justify="left", anchor="w"
                     ).grid(row=row, column=column, columnspan=span, sticky="w",
                            pady=(0, 8))

    def _path_row(self, parent, row: int, key: str, label: str) -> None:
        self._label(parent, label, row)
        entry = ctk.CTkEntry(parent, height=32, font=font(11, mono=True),
                             fg_color=T.INPUT, border_color=T.LINE, border_width=1,
                             text_color=T.TEXT)
        entry.insert(0, getattr(self.cfg, key))
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        ctk.CTkButton(parent, text="Browse", width=70, height=30, corner_radius=6,
                      font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=lambda e=entry: self._browse(e)
                      ).grid(row=row, column=2, padx=(6, 4), pady=3)
        self.fields[key] = entry

    def _browse(self, entry) -> None:
        chosen = filedialog.askdirectory(parent=self, initialdir=entry.get() or "/")
        if chosen:
            entry.delete(0, tk.END)
            entry.insert(0, os.path.normpath(chosen))

    def _entry(self, parent, row: int, key: str, label: str, secret: bool = False,
               width: int | None = None) -> None:
        self._label(parent, label, row)
        kw = {"width": width} if width else {}
        entry = ctk.CTkEntry(parent, height=32, font=font(11, mono=True),
                             fg_color=T.INPUT, border_color=T.LINE, border_width=1,
                             text_color=T.TEXT, show="•" if secret else "", **kw)
        entry.insert(0, str(getattr(self.cfg, key)))
        entry.grid(row=row, column=1, sticky="ew" if not width else "w",
                   padx=(0, 4), pady=4)
        self.fields[key] = entry

    def _switch(self, parent, row: int, key: str, label: str) -> None:
        widget = ctk.CTkSwitch(parent, text=label, font=font(11), text_color=T.DIM,
                               progress_color=T.ACCENT, button_color=T.TEXT)
        widget.select() if getattr(self.cfg, key) else widget.deselect()
        widget.grid(row=row, column=0, columnspan=3, sticky="w", padx=4, pady=6)
        self.fields[key] = widget

    def _number(self, parent, row: int, key: str, label: str, low: float, high: float) -> None:
        self._label(parent, label, row)
        entry = ctk.CTkEntry(parent, width=100, height=32, font=font(11, mono=True),
                             fg_color=T.INPUT, border_color=T.LINE, border_width=1,
                             text_color=T.TEXT)
        entry.insert(0, str(getattr(self.cfg, key)))
        entry.grid(row=row, column=1, sticky="w", pady=4)
        self.fields[key] = (entry, low, high)

    def _choice(self, parent, row: int, key: str, label: str, values: list) -> None:
        self._label(parent, label, row)
        menu = ctk.CTkOptionMenu(
            parent, values=values, width=150, height=32, font=font(11),
            fg_color=T.INPUT, button_color=T.LINE, button_hover_color=T.BTN_HOV,
            dropdown_fg_color=T.ELEVATED, dropdown_hover_color=T.ACCENT2_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11), text_color=T.TEXT)
        # A stored value that is no longer offered (renamed, or dropped in a
        # later version) would otherwise show as a dead entry that saves
        # itself straight back; fall back to the first, recommended choice.
        current = str(getattr(self.cfg, key))
        menu.set(current if current in values else values[0])
        menu.grid(row=row, column=1, sticky="w", pady=4)
        self.fields[key] = menu

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(footer, text="Cancel", width=90, height=32, corner_radius=7,
                      font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self.destroy
                      ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(footer, text="Save settings", width=130, height=32,
                      corner_radius=7, font=font(11, "bold"), fg_color=T.BTN_GO,
                      hover_color=T.BTN_GO_H, text_color="#FFFFFF",
                      command=self._save).grid(row=0, column=2)

    # ── tabs ──────────────────────────────────────────────────────────────

    def _build_convert_folders(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        self._section(tab, "Locations", 0)
        self._path_row(tab, 1, "source_root", "Source")
        self._path_row(tab, 2, "output_root", "Converted")
        self._path_row(tab, 3, "premium_root", "4K 60+")
        self._path_row(tab, 4, "upscale_root", "Needs work")
        self._path_row(tab, 5, "log_dir", "Reports")
        self._hint(tab, 6, "The Library tab indexes the Converted folder by "
                          "default (change it under the Library tab here if "
                          "your library lives somewhere else).", column=1)

        self._section(tab, "Categories", 7)
        ctk.CTkLabel(tab, text="Subfolders", font=font(11), text_color=T.DIM,
                     anchor="w").grid(row=8, column=0, sticky="w", padx=4)
        subs = ctk.CTkEntry(tab, height=30, font=font(11, mono=True), fg_color=T.INPUT,
                            border_color=T.LINE, border_width=1, text_color=T.TEXT)
        subs.insert(0, ", ".join(self.cfg.subfolders))
        subs.grid(row=8, column=1, columnspan=2, sticky="ew", padx=(0, 4), pady=3)
        self.fields["subfolders"] = subs
        self._hint(tab, 9, "Category folder names, comma separated - the same "
                          "list both tabs use to organise the library.")

        ctk.CTkLabel(tab, text="Extensions", font=font(11), text_color=T.DIM,
                     anchor="w").grid(row=10, column=0, sticky="w", padx=4)
        exts = ctk.CTkEntry(tab, height=30, font=font(11, mono=True), fg_color=T.INPUT,
                            border_color=T.LINE, border_width=1, text_color=T.TEXT)
        exts.insert(0, ", ".join(self.cfg.source_extensions))
        exts.grid(row=10, column=1, columnspan=2, sticky="ew", padx=(0, 4), pady=3)
        self.fields["source_extensions"] = exts

    def _build_encoding(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        have = available_encoders()

        self._section(tab, "Codec", 0)
        self._choice(tab, 1, "codec", "Video codec", ["h264", "hevc", "av1"])

        gpu_name = GPU_ENCODERS.get(self.cfg.codec, "")
        available = (not have) or gpu_name in have
        ctk.CTkLabel(
            tab, text=f"GPU encoder: {gpu_name} " +
                      ("detected" if available else "not found in this ffmpeg build"),
            font=font(10, mono=True), text_color=T.OK if available else T.WARN
        ).grid(row=2, column=1, sticky="w", pady=(0, 4))

        self._switch(tab, 3, "use_gpu", "Use GPU when available")

        self._section(tab, "Quality", 4)
        self._number(tab, 5, "gpu_quality", "GPU quality (cq)", 0, 51)
        self._number(tab, 6, "cpu_quality", "CPU quality (crf)", 0, 51)
        self._choice(tab, 7, "cpu_preset", "CPU preset",
                    ["ultrafast", "veryfast", "fast", "medium", "slow", "veryslow"])

        self._section(tab, "Frame rate for editing", 8)
        self._hint(tab, 9, "This is real behavior, not just a label: sources "
                          "reading 58.5 fps up to just under your Minimum fps "
                          "(Sorting tab, 60 by default) are actually re-encoded "
                          "at exactly that rate - 59.94 is NTSC's way of "
                          "writing 60, off by a rounding hair, not a genuinely "
                          "slower clip. The output file really is 60.000 fps, "
                          "not 59.94 counted as close enough. Sources already "
                          "at or above the target are left untouched.")
        self._switch(tab, 10, "force_cfr", "Force constant frame rate")
        self._switch(tab, 11, "edit_gop", "1-second keyframes (smooth scrubbing)")
        self._switch(tab, 12, "loop_short", "Loop very short clips")
        self._number(tab, 13, "loop_min", "...to at least (s)", 1, 60)

        self._section(tab, "Audio", 14)
        self._choice(tab, 15, "audio_mode", "Audio", ["keep", "mute", "none"])
        self._hint(tab, 16, "mute = silent track (uniform in editors), "
                           "none = no audio stream at all")

        self._section(tab, "Throughput", 17)
        self._number(tab, 18, "workers", "Parallel files", 1, 8)
        self._number(tab, 19, "stall_timeout", "Stall timeout (s)", 15, 3600)
        self._switch(tab, 20, "verify_output", "Verify each result by decoding it")
        self._hint(tab, 21, "Catches corrupt output. Roughly doubles run time.")

    def _build_sorting(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        self._section(tab, "Rules", 0)
        ctk.CTkLabel(
            tab, text="Files that meet both thresholds go to the 4K 60+ folder.\n"
                     "Everything else goes to the needs-work folder.",
            font=font(11), text_color=T.DIM, justify="left"
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 8))

        self._number(tab, 2, "min_height", "Minimum short side", 240, 8192)
        self._hint(tab, 3, "Checked against whichever of width/height is "
                          "smaller, not literally the height - a 2160-wide, "
                          "3840-tall phone-orientation clip meets a 2160 "
                          "threshold exactly like a 3840x2160 landscape one "
                          "does.")
        self._number(tab, 4, "min_fps", "Minimum fps", 1, 480)
        self._hint(tab, 5, "58.5 up to just under this is resampled to exactly "
                          "this value during conversion (see Encoding > Frame "
                          "rate for editing) - a real re-encode, not a rule "
                          "that just looks the other way. A source's fps must "
                          "genuinely meet this number, resampled or native, to "
                          "count as fast enough.")

        self._section(tab, "Delivery", 6)
        self._choice(tab, 7, "transfer_mode", "Move files by", ["copy", "move", "hardlink"])
        self._hint(tab, 8, "Hardlink is instant and uses no extra space, but "
                          "only within one drive.")

        self._switch(tab, 9, "sort_enabled", "Sort after converting")
        self._switch(tab, 10, "sort_existing", "Also sort outputs from earlier runs")
        self._switch(tab, 11, "gap_check_enabled",
                    "Check for upscale gaps after Start (convert -> sort -> "
                    "double-check)")

    def _build_library(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        self._section(tab, "Indexing scope", 0)
        summary = self.app.library._folders_summary() if self.app.library else ""
        self._label(tab, "Indexed folders", 1)
        ctk.CTkLabel(tab, text=summary or "None selected", font=font(11),
                     text_color=T.ACCENT2 if summary else T.FAINT, anchor="w",
                     wraplength=self.HINT_W, justify="left"
                     ).grid(row=1, column=1, columnspan=2, sticky="w", pady=4)
        ctk.CTkButton(tab, text="Change folders…", width=150, height=32, corner_radius=7,
                      font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self._change_library_folders
                      ).grid(row=2, column=1, sticky="w", pady=(2, 4))

        self._section(tab, "Display", 3)
        self._label(tab, "Thumbnail fit", 4)
        fit = ctk.CTkSegmentedButton(
            tab, values=["contain", "cover"], font=font(11), height=32, corner_radius=7,
            fg_color=T.INPUT, selected_color=T.ACCENT_DEEP,
            selected_hover_color=T.ACCENT_DEEP, unselected_color=T.INPUT,
            unselected_hover_color=T.BTN_HOV, text_color=T.DIM, border_width=1)
        fit.set(self.cfg.thumb_fit if self.cfg.thumb_fit in ("contain", "cover") else "contain")
        fit.grid(row=4, column=1, sticky="w", pady=4)
        self.fields["thumb_fit"] = fit
        self._hint(tab, 5, "contain shows the whole frame with a blurred backdrop "
                          "- nothing is ever cut off. cover fills the tile edge "
                          "to edge.")
        self._number(tab, 6, "page_size", "Clips per page", 12, 200)
        self._number(tab, 7, "thumb_width", "Thumbnail width", 240, 960)
        self._hint(tab, 8, "Changing thumbnail width needs a full rebuild "
                          "(Ctrl+Shift+R) to take effect on existing clips.")
        self._number(tab, 9, "card_width", "Gallery tile width (target)", 120, 480)
        self._hint(tab, 10, "A target, not an exact size: the gallery fits as "
                           "many whole columns of at least this width as the "
                           "window allows, then stretches them evenly to fill "
                           "it - so the number you see may differ from what "
                           "you typed. Takes effect immediately.")

        self._section(tab, "Player", 11)
        self._switch(tab, 12, "player_loop", "Loop clips by default")
        self._switch(tab, 13, "theater", "Theater mode (viewer takes ~half the window)")

        self._section(tab, "Tagging", 14)
        self._switch(tab, 15, "library_autofetch",
                    "Fetch missing tags automatically after a sync")
        self._number(tab, 16, "library_stale_refresh_budget",
                    "Soft-refresh budget per fetch", 0, 2000)
        self._hint(tab, 17, "Every tag fetch also quietly re-checks up to this "
                           "many already-tagged posts that are \"due\" - fresh "
                           "posts get re-checked every few days for new votes/"
                           "tags, old ones every few months - so scores stay "
                           "current without ever re-fetching the whole library "
                           "at once. 0 turns this off (right-click Fetch e621 "
                           "tags still lets you force a bigger catch-up pass).")

        self._section(tab, "Performance", 18)
        self._number(tab, 19, "probe_cache_limit", "ffprobe results cached", 2000, 500000)
        self._hint(tab, 20, "In-memory only, a few hundred bytes each - raise "
                           "this as your library grows so browsing doesn't "
                           "keep re-reading files ffprobe already looked at.")
        self._number(tab, 21, "frame_cache_limit", "Scrub/hover frames cached", 500, 200000)
        self._hint(tab, 22, "On-disk JPEGs in your temp folder, a few KB each "
                           "- this is the hover-preview/scrub cache, separate "
                           "from the permanent one-per-clip gallery thumbnails.")

    def _change_library_folders(self):
        library = getattr(self.app, "library", None)
        if library is not None:
            from .library_windows import FoldersWindow
            FoldersWindow(self, library)

    def _build_app(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        self._section(tab, "e621 account", 0)
        ctk.CTkLabel(tab, text="An API key is optional, but it lifts the "
                              "anonymous limits and lets lookups see posts "
                              "hidden from logged-out users.\nGet one at "
                              "e621.net > Account > Manage API Access.",
                     font=font(10), text_color=T.FAINT, justify="left",
                     wraplength=520).grid(row=1, column=0, columnspan=3, sticky="w",
                                          padx=10, pady=(0, 8))
        self._entry(tab, 2, "e621_user", "Username")
        self._entry(tab, 3, "e621_key", "API key", secret=True)
        self._switch(tab, 4, "e621_enabled", "Enable e621 lookups")
        self._number(tab, 5, "e621_fetch_delay", "Seconds between requests", 0.5, 5.0)
        self._hint(tab, 6, "0.6s comfortably respects e621's rate limit. Lower "
                          "it only if you know your account allows more.")

        self._section(tab, "Watch mode", 7)
        self._switch(tab, 8, "watch_resume", "Re-arm watch mode on launch")
        self._hint(tab, 9, "Off = watch always starts disarmed, and only "
                          "runs after you flip it on for the session.")

        self._section(tab, "Interface", 10)
        self._switch(tab, 11, "hover_peek", "Hover peek on the Convert queue")
        self._number(tab, 12, "filmstrip_frames", "Filmstrip frames", 4, 16)

        # Also reachable by right-clicking the strip itself, but that is not
        # a thing anyone discovers on their own.
        self._label(tab, "Header picture", 13)
        self.banner_label = ctk.CTkLabel(
            tab, text=self._banner_summary(), font=font(11, mono=True),
            text_color=T.ACCENT2 if self.cfg.banner_path else T.FAINT,
            anchor="w", wraplength=self.HINT_W, justify="left")
        self.banner_label.grid(row=13, column=1, columnspan=2, sticky="w", pady=4)
        banner_row = ctk.CTkFrame(tab, fg_color="transparent")
        banner_row.grid(row=14, column=1, columnspan=2, sticky="w", pady=(2, 4))
        ctk.CTkButton(banner_row, text="Choose picture…", width=140, height=32,
                      corner_radius=7, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.DIM,
                      command=self._pick_banner).pack(side="left", padx=(0, 8))
        ctk.CTkButton(banner_row, text="Clear", width=70, height=32,
                      corner_radius=7, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.DIM,
                      command=self._clear_banner).pack(side="left")
        self._hint(tab, 15, "Your own picture across the top strip, behind the "
                            "PAZ mark. Wide pictures suit it best - it is a "
                            "76px band, cropped to fill from just above centre.")

        # The Beat This tab deliberately has no model picker - there is one
        # right answer and putting it in the way of pressing Analyze only
        # invites picking something worse. These are the escape hatches for
        # the rare machine that needs them.
        self._section(tab, "Beat This", 16)
        self._choice(tab, 17, "beat_checkpoint", "Model", list(be.CHECKPOINTS))
        self._hint(tab, 18, "Leave this alone unless you have a reason. "
                            + be.CHECKPOINT_NOTES.get(be.DEFAULT_CHECKPOINT, ""))
        self._choice(tab, 19, "beat_device", "Run on", list(be.DEVICE_CHOICES))
        self._switch(tab, 20, "beat_float16", "float16 (faster on recent GPUs, "
                                              "slightly less precise)")
        self._switch(tab, 21, "beat_dbn", "DBN postprocessing (needs madmom)")
        self._hint(tab, 22, "DBN is off for a reason: the paper this tracker "
                            "comes from is called \"Accurate Beat Tracking "
                            "Without DBN Postprocessing\". It is here for "
                            "comparison, not for quality.")

    def _banner_summary(self) -> str:
        path = self.cfg.banner_path
        return os.path.basename(path) if path else "None - using the default sweep"

    def _pick_banner(self) -> None:
        self.app.pick_banner()
        self._refresh_banner()

    def _clear_banner(self) -> None:
        self.app.clear_banner()
        self._refresh_banner()

    def _refresh_banner(self) -> None:
        self.banner_label.configure(
            text=self._banner_summary(),
            text_color=T.ACCENT2 if self.cfg.banner_path else T.FAINT)
        self.lift()

    # ── save ──────────────────────────────────────────────────────────────

    def _save(self) -> None:
        for key, widget in self.fields.items():
            try:
                if key in ("subfolders", "source_extensions"):
                    parts = [p.strip() for p in widget.get().split(",") if p.strip()]
                    if key == "source_extensions":
                        parts = [p if p.startswith(".") else "." + p for p in parts]
                    setattr(self.cfg, key, parts)
                elif isinstance(widget, tuple):
                    entry, low, high = widget
                    current = getattr(self.cfg, key)
                    value = max(low, min(float(entry.get()), high))
                    setattr(self.cfg, key, type(current)(value))
                elif isinstance(widget, ctk.CTkSwitch):
                    setattr(self.cfg, key, bool(widget.get()))
                else:
                    value = widget.get()
                    setattr(self.cfg, key, value.strip() if isinstance(value, str) else value)
            except (ValueError, TypeError):
                continue
        error = self.cfg.save()
        if error:
            messagebox.showerror("Could not save settings",
                                 f"Writing {CONFIG_PATH} failed:\n\n{error}", parent=self)
            return
        self.app.on_settings_saved()
        self.destroy()
