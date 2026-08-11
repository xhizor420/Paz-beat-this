"""Library-tab dialog windows: hidden-tag management, the help reference,
the folder picker, and the library integrity verifier.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .theme import T, font
from .files import is_ignored_dir, open_in_explorer
from .convert_engine import verify
from . import uithread


class HiddenTagsWindow(ctk.CTkToplevel):
    """Restore tags that were muted from the sidebar."""

    def __init__(self, parent, tab):
        super().__init__(parent)
        self.tab = tab
        self.title("Hidden tags")
        self.geometry("420x520")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Hidden from the sidebar - never deleted, "
                                "always searchable, still shown on clips.",
                     font=font(10), text_color=T.FAINT, wraplength=380,
                     justify="left").grid(row=0, column=0, sticky="ew",
                                          padx=16, pady=(14, 6))
        self.list = ctk.CTkScrollableFrame(
            self, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.LINE, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        self.list.grid(row=1, column=0, sticky="nsew", padx=14)
        self.list.grid_columnconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=14, pady=12)
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(footer, text="Restore all", width=100, height=30,
                      corner_radius=7, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.ACCENT,
                      command=self._restore_all).grid(row=0, column=1)
        self._fill()

    def _fill(self):
        for child in self.list.winfo_children():
            child.destroy()
        hidden = sorted(self.tab.cfg.hidden_tags)
        if not hidden:
            ctk.CTkLabel(self.list, text="Nothing hidden.", font=font(11),
                         text_color=T.FAINT).grid(row=0, column=0, padx=12,
                                                   pady=12, sticky="w")
            return
        for row, name in enumerate(hidden):
            ctk.CTkLabel(self.list, text=name, font=font(11),
                         text_color=T.TEXT, anchor="w"
                         ).grid(row=row, column=0, sticky="ew", padx=(12, 4), pady=3)
            ctk.CTkButton(self.list, text="Restore", width=70, height=24,
                          corner_radius=6, font=font(10), fg_color=T.BTN,
                          hover_color=T.BTN_HOV, text_color=T.OK,
                          command=lambda n=name: self._restore(n)
                          ).grid(row=row, column=1, padx=(0, 10), pady=3)

    def _restore(self, name: str):
        self.tab.unhide_tag(name)
        self._fill()

    def _restore_all(self):
        self.tab.cfg.hidden_tags = []
        self.tab.cfg.save()
        self.tab._render_tagpanel()
        self._fill()


class HelpWindow(ctk.CTkToplevel):
    """What every button does, in one place."""

    SECTIONS = (
        ("Sync library", "Checks the chosen folders against the database and "
         "only processes what changed - new files get probed and "
         "thumbnailed, deleted ones dropped. First build is the slow one; "
         "Ctrl+Shift+R forces a full rebuild."),
        ("Fix missing / Verify", "Re-probes files with no duration or "
         "resolution, rebuilds absent thumbnails, then fetches tags for "
         "every uncached post ID - the number on the button is what's "
         "left. Right-click it for Verify library integrity, a slower "
         "full decode pass that catches corrupt files a quick probe can't."),
        ("Fetch e621 tags", "Resolves post IDs (the filename numbers) into "
         "artist / character / species / rating. A small batch runs "
         "automatically when the tab opens and after every sync, so new "
         "files get tagged without asking; pressing the button yourself "
         "runs a full pass instead, catching up every post that's due for "
         "a refresh, not just a small batch. Add an API key in Settings "
         "for fewer unavailable posts."),
        ("Score, 4K ✓, Ratio", "▲ is the e621 upvote score - sort by it or "
         "use the Top rated button. 4K ✓ means a 4K/60+ copy exists. Ratio "
         "(next to Random) is a one-click Portrait / Widescreen / Square "
         "filter, since aspect ratio isn't usually a tag."),
        ("Grid", "A contact sheet of twelve evenly-spaced frames from the "
         "selected clip. Click any frame to jump the player there."),
        ("Search", "Terms AND together, -term excludes. Prefixes: artist: "
         "character: species: rating: folder: id: is: used:. Wildcards: "
         "dragon*. is:untagged, is:noid, is:4k, is:portrait, is:widescreen, "
         "is:square are the useful specials; used:\"project name\" (quotes "
         "for names with spaces) finds clips marked used in the Vault tab. "
         "Click a tag to add it; right-click for exclude/hide."),
        ("Vault", "A separate tab for the moment after you finish an edit: "
         "paste a list of post IDs or filenames, it finds them in the "
         "library, and you mark the ones you actually used under a project "
         "name. Marked clips get a coloured border here in the gallery - a "
         "different colour per project, and a clip can carry marks from "
         "more than one - so browsing later shows at a glance what's "
         "already been spent. The PROJECTS group in the sidebar lists every "
         "project; click one to jump straight to its clips."),
        ("Player", "Scales with the window; Theater mode (Ctrl+T) gives it "
         "about half the window and collapses the tag rail. When a clip has "
         "a matching 4K/60+ copy in the edit pool, playback defaults to "
         "that instead of the original - the 4K button next to Loop "
         "switches back and remembers your choice."),
        ("Copying", "Right-click any clip > Copy for name, path, post ID, "
         "e621 URL, artist or all tags. Ctrl+C copies the name, "
         "Ctrl+Shift+C the full path."),
        ("Keys", "/ search · Enter or Space play/pause · ←→ seek 5s · "
         "R random · PgUp/PgDn pages · Ctrl+L collapse tags · Ctrl+C copy "
         "name · Ctrl+F search · F5 sync · Ctrl+O folders · Ctrl+T theater"),
    )

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Help")
        self.geometry("560x640")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        body = ctk.CTkScrollableFrame(
            self, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.LINE, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        body.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        body.grid_columnconfigure(0, weight=1)
        row = 0
        for title, text in self.SECTIONS:
            ctk.CTkLabel(body, text=title, font=font(12, "bold"),
                         text_color=T.ACCENT, anchor="w"
                         ).grid(row=row, column=0, sticky="ew", padx=14,
                                pady=(14 if row else 12, 2))
            row += 1
            ctk.CTkLabel(body, text=text, font=font(10), text_color=T.DIM,
                         wraplength=480, justify="left", anchor="w"
                         ).grid(row=row, column=0, sticky="ew", padx=14)
            row += 1
        self.bind("<Escape>", lambda e: self.destroy())


class FoldersWindow(ctk.CTkToplevel):
    """
    Picks exactly what the Library indexes: one root folder (defaults to
    the Convert tab's output folder, since that's almost always what you
    want), and which category subfolders inside it count. Everything
    unticked is invisible to the Library - not scanned, not probed, not
    thumbnailed.
    """

    def __init__(self, parent, tab):
        super().__init__(parent)
        self.tab = tab
        self.cfg = tab.cfg
        self._before = (self.cfg.library_root, sorted(self.cfg.library_subfolders),
                         self.cfg.library_recursive)

        self.title("Library folders")
        self.geometry("640x560")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(self, text="LIBRARY ROOT", font=font(10, "bold"),
                     text_color=T.FAINT, anchor="w"
                     ).grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 4))

        rowf = ctk.CTkFrame(self, fg_color="transparent")
        rowf.grid(row=1, column=0, sticky="ew", padx=16)
        rowf.grid_columnconfigure(0, weight=1)
        self.root_entry = ctk.CTkEntry(
            rowf, height=32, font=font(11, mono=True), fg_color=T.INPUT,
            border_color=T.LINE, border_width=1, text_color=T.TEXT)
        self.root_entry.insert(0, self.cfg.effective_library_root())
        self.root_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(rowf, text="Browse", width=76, height=32,
                      corner_radius=7, font=font(10), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.DIM,
                      command=self._browse).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(rowf, text="Rescan", width=70, height=32,
                      corner_radius=7, font=font(10), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.ACCENT2,
                      command=self._reload).grid(row=0, column=2, padx=(8, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(14, 0))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(body, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="CATEGORIES TO INDEX", font=font(10, "bold"),
                     text_color=T.FAINT, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(head, text="All", width=44, height=22, corner_radius=5,
                      font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=lambda: self._set_all(True)
                      ).grid(row=0, column=1, padx=(0, 6))
        ctk.CTkButton(head, text="None", width=52, height=22, corner_radius=5,
                      font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=lambda: self._set_all(False)
                      ).grid(row=0, column=2)

        self.list = ctk.CTkScrollableFrame(
            body, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.LINE, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        self.list.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.list.grid_columnconfigure(0, weight=1)

        self.recursive = ctk.CTkSwitch(
            self, text="Also index folders nested inside these",
            font=font(11), text_color=T.DIM, progress_color=T.ACCENT2,
            button_color=T.TEXT)
        (self.recursive.select() if self.cfg.library_recursive
         else self.recursive.deselect())
        self.recursive.grid(row=3, column=0, sticky="w", padx=20, pady=(12, 0))

        self.note = ctk.CTkLabel(self, text="", font=font(10),
                                  text_color=T.FAINT, anchor="w",
                                  wraplength=580, justify="left")
        self.note.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 0))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", padx=16, pady=14)
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(footer, text="Cancel", width=90, height=32,
                      corner_radius=7, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.DIM,
                      command=self.destroy).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(footer, text="Save folders", width=130, height=32,
                      corner_radius=7, font=font(11, "bold"),
                      fg_color=T.ACCENT2_DEEP, hover_color=T.BTN_HOV,
                      text_color=T.ACCENT2, command=self._save
                      ).grid(row=0, column=2)

        self.boxes: dict = {}
        self._reload()

    def _browse(self):
        chosen = filedialog.askdirectory(
            parent=self, initialdir=self.root_entry.get() or "/")
        if chosen:
            self.root_entry.delete(0, tk.END)
            self.root_entry.insert(0, os.path.normpath(chosen))
            self._reload()

    def _reload(self):
        for child in self.list.winfo_children():
            child.destroy()
        self.boxes = {}
        root = self.root_entry.get().strip()
        if not os.path.isdir(root):
            self.note.configure(
                text="That folder does not exist yet. Pick the converted "
                     "library (the Convert tab's output folder).")
            return
        try:
            names = sorted(n for n in os.listdir(root)
                            if os.path.isdir(os.path.join(root, n))
                            and not is_ignored_dir(n))
        except OSError as exc:
            self.note.configure(text=f"Could not read that folder: {exc}")
            return
        wanted = set(self.cfg.library_subfolders)
        for index, name in enumerate(names):
            count = self._count(os.path.join(root, name))
            box = ctk.CTkCheckBox(
                self.list, text=f"{name}     ({count} files)",
                font=font(11), text_color=T.TEXT, fg_color=T.ACCENT2,
                hover_color=T.ACCENT2_HOV, border_color=T.LINE,
                checkbox_width=18, checkbox_height=18)
            if not wanted or name in wanted:
                box.select()
            box.grid(row=index, column=0, sticky="w", padx=12, pady=4)
            self.boxes[name] = box
        self.note.configure(
            text=f"{len(names)} subfolders found. Only ticked ones are "
                 "scanned - the rest cost no time at all.")

    def _count(self, directory: str) -> int:
        ext = self.cfg.library_ext_set
        try:
            with os.scandir(directory) as entries:
                return sum(1 for e in entries if e.is_file()
                           and os.path.splitext(e.name)[1].lower() in ext)
        except OSError:
            return 0

    def _set_all(self, state: bool):
        for box in self.boxes.values():
            box.select() if state else box.deselect()

    def _save(self):
        root = self.root_entry.get().strip()
        self.cfg.library_root = root
        picked = [name for name, box in self.boxes.items() if box.get()]
        self.cfg.library_subfolders = picked
        self.cfg.library_recursive = bool(self.recursive.get())
        changed = (self.cfg.library_root, sorted(self.cfg.library_subfolders),
                   self.cfg.library_recursive) != self._before
        self.tab._folders_saved(changed)
        self.destroy()


class VerifyWindow(ctk.CTkToplevel):
    """
    Full decode pass over the library to catch corrupt or truncated files
    that a quick probe can't see - probing only reads container headers,
    so a file with a broken frame in the middle still probes clean.

    Runs a handful of ffmpeg decodes in parallel (like the duplicate
    finder's probing pass) and reports anything that fails. Nothing is
    touched unless you explicitly delete a broken file from the results.
    """

    def __init__(self, parent, tab):
        super().__init__(parent)
        self.tab = tab
        self.alive = True
        self._cancelled = threading.Event()
        self._broken = 0

        self.title("Verify library")
        self.geometry("760x600")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
        head.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(head, text="Starting…", font=font(11, mono=True),
                                    text_color=T.DIM, anchor="w")
        self.status.grid(row=0, column=0, sticky="w")
        self.stop_btn = ctk.CTkButton(
            head, text="Stop", width=70, height=28, corner_radius=7,
            font=font(11), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.WARN, command=self._stop)
        self.stop_btn.grid(row=0, column=1, padx=(10, 0))

        self.progress = ctk.CTkProgressBar(self, height=5, corner_radius=3,
                                            fg_color=T.LINE_SOFT, progress_color=T.ACCENT2)
        self.progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        self.progress.set(0)

        self.body = ctk.CTkScrollableFrame(
            self, fg_color=T.SURFACE, corner_radius=12,
            scrollbar_button_color=T.LINE, scrollbar_button_hover_color=T.FAINT)
        self.body.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.body.grid_columnconfigure(0, weight=1)

        threading.Thread(target=self._run, daemon=True).start()

    def _close(self):
        self.alive = False
        self._cancelled.set()
        self.destroy()

    def _stop(self):
        self._cancelled.set()
        self.stop_btn.configure(state="disabled", text="Stopping…")

    def ui(self, fn, *a, **k):
        if not self.alive:
            return
        uithread.post(fn, *a, **k)

    def _run(self):
        paths = [rec.path for rec in self.tab.records if os.path.exists(rec.path)]
        total = len(paths)
        if not total:
            self.ui(self.status.configure, text="Nothing to verify - the library is empty.")
            return
        workers = min(4, max(2, os.cpu_count() or 4))
        checked = 0

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(verify, path): path for path in paths}
            for future in as_completed(futures):
                if self._cancelled.is_set():
                    break
                path = futures[future]
                try:
                    ok, error = future.result()
                except Exception as exc:
                    ok, error = False, str(exc)
                checked += 1
                if not ok:
                    self._broken += 1
                    self.ui(self._add_broken, path, error or "decode failed")
                if checked % 3 == 0 or checked == total:
                    self.ui(self._tick, checked, total)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        stopped = self._cancelled.is_set()
        self.ui(self._done, checked, total, stopped)

    def _tick(self, checked: int, total: int) -> None:
        self.progress.set(checked / total if total else 0)
        self.status.configure(
            text=f"Verified {checked}/{total} · {self._broken} broken so far")

    def _done(self, checked: int, total: int, stopped: bool) -> None:
        self.stop_btn.configure(state="disabled", text="Stopped" if stopped else "Done")
        if stopped:
            self.status.configure(
                text=f"Stopped after {checked}/{total} · {self._broken} broken found",
                text_color=T.WARN)
        elif self._broken:
            self.status.configure(
                text=f"Checked {checked} clips · {self._broken} broken", text_color=T.WARN)
        else:
            self.status.configure(text=f"Checked {checked} clips · all clean", text_color=T.OK)

    def _add_broken(self, path: str, error: str) -> None:
        row = len(self.body.winfo_children())
        card = ctk.CTkFrame(self.body, fg_color=T.ELEVATED, corner_radius=8)
        card.grid(row=row, column=0, sticky="ew", padx=8, pady=4)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text=os.path.basename(path), font=font(11, "bold"),
                     text_color=T.FAIL, anchor="w"
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
        ctk.CTkLabel(card, text=error.splitlines()[0][:160], font=font(10, mono=True),
                     text_color=T.DIM, anchor="w"
                     ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        ctk.CTkButton(card, text="Show in folder", height=24, width=110, corner_radius=6,
                      font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=lambda p=path: open_in_explorer(p)
                      ).grid(row=0, column=1, rowspan=2, padx=(4, 4), pady=8)
        ctk.CTkButton(card, text="Delete", height=24, width=70, corner_radius=6,
                      font=font(10), fg_color=T.BTN, hover_color=T.FAIL_DEEP,
                      text_color=T.DIM, command=lambda p=path, c=card: self._delete(p, c)
                      ).grid(row=0, column=2, rowspan=2, padx=(0, 10), pady=8)

    def _delete(self, path: str, card) -> None:
        if not messagebox.askyesno(
                "Delete file", f"Delete this broken file?\n\n{path}", parent=self):
            return
        try:
            os.remove(path)
        except OSError as exc:
            messagebox.showerror("Could not delete", str(exc), parent=self)
            return
        card.destroy()
        self.tab.set_status(f"Deleted broken file: {os.path.basename(path)}", T.WARN)
