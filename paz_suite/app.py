"""PAZ Suite application shell: one window, a Convert/Library/Vault/Beat
This tabview, and the shared services (config, e621 cache, thumbnail
cache, toasts, hover peek) the tabs draw on. Also owns the keyboard-
shortcut dispatch, since several shortcuts mean different things on each
tab and must only fire for whichever one is currently visible.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from .theme import (T, BANNER_H, banner_photo, font, is_image_path,
                    mark_photo, mix, resolve_fonts)
from .config import AppConfig
from .e621 import E621Meta, APP_NAME, APP_VERSION
from .media import ThumbCache, set_probe_cache_limit
from .widgets import Toaster, PeekWindow, popup_menu, menu_rule
from .convert_tab import ConvertTab
from .library_tab import LibraryTab
from .vault_tab import VaultTab
from .beat_tab import BeatTab
from .settings_window import SettingsWindow
from . import uithread

TAB_NAMES = ("Convert", "Library", "Vault", "Beat This")


class PazApp:

    def __init__(self, root: ctk.CTk):
        self.root = root
        # Before any tab exists, so the worker threads the tabs start in
        # their constructors have somewhere safe to post results - those
        # run before root.mainloop() does. See uithread's module docstring.
        uithread.install(root)
        self.cfg = AppConfig.load()
        self.emeta = E621Meta()
        set_probe_cache_limit(self.cfg.probe_cache_limit)
        self.cache = ThumbCache(limit=self.cfg.frame_cache_limit)  # shared frame/thumbnail cache
        self.toaster = Toaster(root)
        self.peek = PeekWindow(root)
        self._icon = None
        self._header_icon = None

        self._apply_scaling()

        # Sized to the display rather than to a fixed number. 1760x1020 is
        # a good window on a 1080p screen and a postage stamp on a 4K one -
        # opening at a fixed size there left the gallery three narrow
        # columns wide with two thirds of the desktop unused. Still clamped
        # to what the screen can hold, so the title bar stays reachable.
        try:
            room_w = max(int(root.winfo_screenwidth()) - 80, 900)
            room_h = max(int(root.winfo_screenheight()) - 120, 620)
        except tk.TclError:
            room_w, room_h = 1760, 1020
        width = min(max(1760, int((room_w + 80) * 0.78)), room_w)
        height = min(max(1020, int((room_h + 120) * 0.82)), room_h)
        root.geometry(f"{width}x{height}")
        # Every panel below (gallery columns, the inspector/player, the
        # queue table) already recalculates its own layout on resize, so
        # this is a floor for legibility, not a hard requirement - the
        # window is just as usable maximized on a 4K display as tiled on a
        # 13" laptop screen.
        root.minsize(min(1180, room_w), min(700, room_h))
        root.configure(fg_color=T.BG)

        self._build_header()
        self._build_tabstrip()

        self.tabview = ctk.CTkTabview(
            root, fg_color=T.BG, corner_radius=0,
            text_color=T.TEXT, command=self._on_tab_changed)
        self.tabview.pack(fill="both", expand=True, padx=0, pady=0)
        for name in TAB_NAMES:
            self.tabview.add(name)
        # CTkTabview's own segmented button can't express the design's tab
        # strip - each tab carries its identity colour as a dot that stays
        # visible (just muted) while inactive, and one button can only have
        # a single text colour. So the real strip is _build_tabstrip()
        # above and CTkTabview is kept purely as the page container, with
        # its built-in switcher hidden rather than restyled.
        self.tabview._segmented_button.grid_forget()
        # Forgetting the button doesn't reclaim its space: CTkTabview holds
        # rows 0-2 open with minsize (outer spacing, overhang, button
        # height) whether or not anything occupies them, which leaves a
        # dead band under our own strip. Collapse them.
        for _row in (0, 1, 2):
            self.tabview.grid_rowconfigure(_row, weight=0, minsize=0)

        self.convert = ConvertTab(self.tabview.tab("Convert"), self)
        self.library = LibraryTab(self.tabview.tab("Library"), self)
        # Reads app.library.records to match a pasted list against the
        # index, so it must exist after Library has loaded its own.
        self.vault = VaultTab(self.tabview.tab("Vault"), self)
        self.beat = BeatTab(self.tabview.tab("Beat This"), self)

        if self.cfg.last_tab in TAB_NAMES:
            self.tabview.set(self.cfg.last_tab)

        self._apply_chrome()
        self._style_tabs()
        self._bind_keys()
        root.bind("<Configure>", self._on_root_configure, add="+")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── display scaling ───────────────────────────────────────────────────
    #
    # CustomTkinter scales its own widgets from the system DPI, but a
    # tk.Canvas gets none of that - so on a 4K screen the gallery kept
    # drawing 8pt badges and 10pt captions at their literal size while
    # every button around them grew. theme.pt()/px() read T.SCALE, which
    # is set here, once, before a single widget exists.

    SCALE_CHOICES = ("Auto", "100%", "125%", "150%", "175%", "200%")

    def _apply_scaling(self) -> None:
        choice = self.cfg.ui_scale if self.cfg.ui_scale in self.SCALE_CHOICES else "Auto"
        scale = self._detect_scale() if choice == "Auto" else int(choice.rstrip("%")) / 100
        scale = max(1.0, min(scale, 2.5))
        T.SCALE = scale
        try:
            ctk.set_widget_scaling(scale)
            # Deliberately NOT set_window_scaling: that multiplies every
            # geometry string, so asking for a 1760px window at 175% asks
            # for 3080px and the window manager quietly declines to show it
            # at all on anything smaller. Window sizes here are already in
            # real pixels; it is the contents that need to grow.
            ctk.set_window_scaling(1.0)
        except Exception:
            pass

    def _detect_scale(self) -> float:
        """A sensible scale for this display.

        Tk reports the DPI the window manager hands it, which on Windows
        already reflects the display-scaling setting - a 4K screen at 150%
        comes back as 144 and needs nothing further. But a 4K screen left
        at 100% reports a flat 96, and "the desktop isn't scaling" is not
        the same as "nothing needs scaling": every pixel really is half
        the size it would be on a 1080p panel, which is why the gallery
        captions were unreadable. So when the desktop asks for nothing,
        fall back to the panel's own width, which is what actually decides
        how big a pixel is.
        """
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
            width = int(self.root.winfo_screenwidth())
        except (tk.TclError, ValueError):
            return 1.0
        from_dpi = round(dpi / 96.0, 2) if dpi > 0 else 1.0
        if from_dpi >= 1.2:
            return from_dpi              # the desktop is already scaling
        if width >= 3400:                # 4K and wider
            return 1.5
        if width >= 2500:                # 1440p / ultrawide
            return 1.25
        return max(from_dpi, 1.0)

    # ── header (shared identity, above the tab strip) ──────────────────────
    #
    # Each tab still draws its own small colour-coded brand block (pink for
    # Convert, violet for Library, teal for Vault) so which mode you're in
    # is obvious at a glance without reading the tab label - this bar is
    # just the one piece of chrome no tab should have to own twice: the
    # suite's own name.

    def _build_header(self) -> None:
        self.header = tk.Canvas(self.root, height=BANNER_H, bg=T.BG,
                                highlightthickness=0, bd=0)
        self.header.pack(fill="x", side="top")
        self._banner_photo = None
        self._banner_job = None
        self._banner_width = 0
        self._header_icon = mark_photo(22, T.ACCENT)
        self._header_text = ""
        self._header_colour = T.OK
        self.header.bind("<Configure>", self._banner_resized)
        self.header.bind("<Button-3>", self._banner_menu)
        self._draw_header(self.root.winfo_width() or 1760)

    def _banner_resized(self, event) -> None:
        """Re-render on width changes only, and only after the drag stops.
        Rescaling a full-width picture on every Configure during a window
        drag is the one place in this app that can visibly lag."""
        if event.width == self._banner_width:
            return
        self._banner_width = event.width
        if self._banner_job is not None:
            try:
                self.root.after_cancel(self._banner_job)
            except ValueError:
                pass
        self._banner_job = self.root.after(
            120, lambda: self._draw_header(self._banner_width))

    def _draw_header(self, width: int) -> None:
        """Repaint the whole strip: picture, lockup, status.

        Everything is a canvas item over one composited background image
        rather than a row of packed widgets, because Tk has no widget
        transparency - a CTkLabel over a picture would sit on its own
        opaque rectangle and the banner would look like a mistake.
        """
        width = max(int(width), 320)
        self._banner_job = None
        try:
            self._banner_photo = banner_photo(self.cfg.banner_path, width, BANNER_H)
            self.header.delete("all")
            self.header.create_image(0, 0, image=self._banner_photo, anchor="nw")

            mid = BANNER_H // 2
            self.header.create_image(20, mid, image=self._header_icon, anchor="w")
            name = self.header.create_text(52, mid + 1, text="PAZ", anchor="w",
                                           fill=T.ACCENT,
                                           font=(T.DISPLAY, 19, "bold"))
            # Measured, not guessed: the display family is whatever
            # resolve_fonts() found installed, so "PAZ" is a different
            # width on every machine and a fixed offset collides with it.
            self.header.create_text(self.header.bbox(name)[2] + 9, mid + 4,
                                    text="S U I T E", anchor="w",
                                    fill=T.DIM, font=(T.MONO, 9))
            if self._header_text:
                item = self.header.create_text(
                    width - 20, mid, text=self._header_text, anchor="e",
                    fill=T.DIM, font=(T.MONO, 10))
                left = self.header.bbox(item)[0]
                self.header.create_oval(left - 15, mid - 4, left - 8, mid + 3,
                                        fill=self._header_colour, outline="")
        except tk.TclError:
            pass

    def set_header_status(self, text: str, colour: str = T.OK) -> None:
        """One live line in the identity bar - what the suite is doing
        right now, readable from whichever tab you happen to be on. An
        empty text hides the indicator entirely; a lone dot with nothing
        beside it just looks like a rendering fault."""
        if text == self._header_text and colour == self._header_colour:
            return
        self._header_text = text
        self._header_colour = colour
        self._draw_header(self._banner_width or self.root.winfo_width() or 1760)

    # ── banner picture ──────────────────────────────────────────────────
    #
    # The one place in the suite that shows a picture of your choosing.
    # It lives here, in the chrome, rather than anywhere in the gallery:
    # a clip's tile has a job (show that clip), and a picture standing in
    # for it makes the library harder to read, not nicer to look at. A
    # header strip has no such job, so it is free to be yours.

    def _banner_menu(self, event) -> None:
        menu = popup_menu(self.root)
        menu.add_command(label="Set header picture…", command=self.pick_banner)
        if self.cfg.banner_path:
            menu.add_command(label="Clear header picture", command=self.clear_banner)
        menu_rule(menu)
        menu.add_command(label="Settings…", command=self.open_settings)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def pick_banner(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Pick a picture for the header",
            initialdir=self.cfg.banner_dir or os.path.expanduser("~"),
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                       ("All files", "*.*")])
        if not path:
            return
        self.set_banner(path)

    def set_banner(self, path: str) -> None:
        if not is_image_path(path):
            self.toaster.show("That file isn't a picture the header can use.", "warn")
            return
        self.cfg.banner_path = path
        self.cfg.banner_dir = os.path.dirname(path)
        self.cfg.save()
        self._draw_header(self._banner_width or self.root.winfo_width() or 1760)
        self.toaster.show(f"Header picture set from {os.path.basename(path)}")

    def clear_banner(self) -> None:
        self.cfg.banner_path = ""
        self.cfg.save()
        self._draw_header(self._banner_width or self.root.winfo_width() or 1760)
        self.toaster.show("Header picture cleared")

    # ── chrome (window title / taskbar icon) ────────────────────────────────

    def _apply_chrome(self) -> None:
        self.root.title(f"{APP_NAME}  {APP_VERSION}")
        try:
            if self._icon is None:
                self._icon = mark_photo(32, T.ACCENT)
            self.root.iconphoto(False, self._icon)
        except tk.TclError:
            pass

    # Each tab has its own identity colour (pink for Convert, violet for
    # Library, teal for Vault, amber for Beat This) used throughout its own
    # widgets; the strip carries that colour as a dot per tab so every tab
    # is identifiable at rest, and lights the active one so the switcher
    # reads as "you are here" instead of one flat, generic control.
    _TAB_ACCENTS = {"Convert":   (T.ACCENT_DEEP, T.ACCENT),
                    "Library":   (T.ACCENT2_DEEP, T.ACCENT2),
                    "Vault":     (T.ACCENT3_DEEP, T.ACCENT3),
                    "Beat This": (T.ACCENT4_DEEP, T.ACCENT4)}

    def _build_tabstrip(self) -> None:
        strip = ctk.CTkFrame(self.root, fg_color=T.BG, corner_radius=0, height=44)
        strip.pack(fill="x", side="top")
        strip.pack_propagate(False)

        inner = ctk.CTkFrame(strip, fg_color="transparent")
        inner.pack(side="left", padx=(14, 0), pady=(6, 0))

        self._tab_widgets: dict = {}
        for name in TAB_NAMES:
            deep, bright = self._TAB_ACCENTS[name]
            holder = ctk.CTkFrame(inner, fg_color="transparent", corner_radius=9,
                                  height=32)
            holder.pack(side="left", padx=(0, 4))
            dot = ctk.CTkFrame(holder, width=8, height=8, corner_radius=2,
                               fg_color=mix(bright, T.BG, 0.55))
            dot.pack(side="left", padx=(13, 8), pady=12)
            label = ctk.CTkLabel(holder, text=name, font=font(13, "bold"),
                                 text_color=T.DIM)
            label.pack(side="left", padx=(0, 14))
            # Every piece of the tab is clickable, not just the text - a
            # 32px target that only responds on the glyphs feels broken.
            for widget in (holder, dot, label):
                widget.bind("<Button-1>", lambda e, n=name: self._select_tab(n))
                widget.configure(cursor="hand2")
            self._tab_widgets[name] = (holder, dot, label)

    def _select_tab(self, name: str) -> None:
        self.tabview.set(name)
        self._on_tab_changed()

    def _style_tabs(self) -> None:
        active = self.tabview.get()
        for name, (holder, dot, label) in self._tab_widgets.items():
            deep, bright = self._TAB_ACCENTS[name]
            selected = name == active
            holder.configure(fg_color=deep if selected else "transparent")
            dot.configure(fg_color=bright if selected else mix(bright, T.BG, 0.55))
            label.configure(text_color=bright if selected else T.DIM)

    def _on_tab_changed(self) -> None:
        self.cfg.last_tab = self.tabview.get()
        self.cfg.save()
        self._style_tabs()

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        self.library.on_root_resize()

    # ── settings ─────────────────────────────────────────────────────────

    def open_settings(self, initial_tab: str = "Encoding") -> None:
        SettingsWindow(self.root, self, initial_tab=initial_tab)

    def on_settings_saved(self) -> None:
        self._apply_chrome()
        set_probe_cache_limit(self.cfg.probe_cache_limit)
        self.cache.limit = self.cfg.frame_cache_limit
        self.convert.after_settings_saved()
        self.library.after_settings_saved()
        self.vault.after_settings_saved()
        self.beat.after_settings_saved()

    # ── keyboard dispatch ────────────────────────────────────────────────
    #
    # Convert and Library each bind their own row/tree-level shortcuts
    # locally (unaffected here). Everything below used to be bound
    # separately on each app's own root window; sharing one root means a
    # key like Escape or Space means something different depending on
    # which tab is showing, so every shared shortcut is dispatched by the
    # currently active tab instead of being bound twice.

    def _active(self) -> str:
        return self.tabview.get()

    def _bind_keys(self) -> None:
        root = self.root

        # Every tab currently showing gets exactly one of these - not just
        # Convert vs. "everything else", now that there are three tabs.
        root.bind("<Escape>", lambda e: (
            self.convert.key_stop() if self._active() == "Convert"
            else self.library.key_escape(e) if self._active() == "Library"
            else None))
        root.bind("<space>", lambda e: (
            self.convert.key_space(e) if self._active() == "Convert"
            else self.library.key_space(e) if self._active() == "Library"
            else None))
        root.bind("<F5>", lambda e: (
            self.convert.key_scan() if self._active() == "Convert"
            else self.library.key_sync() if self._active() == "Library"
            else self.vault.key_lookup() if self._active() == "Vault"
            else self.beat.key_analyze()))
        root.bind("<Control-f>", lambda e: (
            self.convert.key_find_search() if self._active() == "Convert"
            else self.library.key_find_search(e) if self._active() == "Library"
            else None))
        for key in ("g", "G"):
            root.bind(key, lambda e: (
                self.convert.key_grid(e) if self._active() == "Convert"
                else self.library.key_grid(e) if self._active() == "Library"
                else None))
        root.bind("<Left>", self._left)
        root.bind("<Right>", self._right)

        # Convert-only
        root.bind("<Control-Return>", lambda e: self._only("Convert", self.convert.key_start))
        for key in ("h", "H"):
            root.bind(key, lambda e: self._only_evt("Convert", self.convert.key_peek_toggle, e))
        root.bind("<Shift-Left>", lambda e: self._only(
            "Convert", lambda: self.convert.key_scrub(e, -10)))
        root.bind("<Shift-Right>", lambda e: self._only(
            "Convert", lambda: self.convert.key_scrub(e, 10)))

        # Library-only
        for key in ("r", "R"):
            root.bind(key, lambda e: self._only_evt("Library", self.library.key_random, e))
        root.bind("<Return>", lambda e: self._only_evt("Library", self.library.key_play, e))
        root.bind("<Control-o>", lambda e: self._only("Library", self.library.key_open_folders))
        root.bind("<Control-l>", lambda e: self._only("Library", self.library.key_toggle_sidebar))
        root.bind("<Control-t>", lambda e: self._only("Library", self.library.key_toggle_theater))
        root.bind("<Control-Shift-R>",
                  lambda e: self._only("Library", self.library.key_full_rebuild))
        root.bind("<Prior>", lambda e: self._only("Library", lambda: self.library.key_page(-1)))
        root.bind("<Next>", lambda e: self._only("Library", lambda: self.library.key_page(1)))
        root.bind("/", lambda e: self._only_evt("Library", self.library.key_find_search, e))
        root.bind("<Control-c>", lambda e: self._only_evt("Library", self.library.key_copy_name, e))
        root.bind("<Control-Shift-C>",
                  lambda e: self._only_evt("Library", self.library.key_copy_path, e))

    def _only(self, tab_name: str, fn) -> None:
        if self._active() == tab_name:
            fn()

    def _only_evt(self, tab_name: str, fn, event):
        if self._active() == tab_name:
            return fn(event)
        return None

    def _left(self, event):
        if self._active() == "Convert":
            self.convert.key_scrub(event, -1)
        elif self._active() == "Library":
            self.library.key_seek(event, -5)

    def _right(self, event):
        if self._active() == "Convert":
            self.convert.key_scrub(event, 1)
        elif self._active() == "Library":
            self.library.key_seek(event, 5)

    # ── shutdown ─────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if not self.convert.on_app_close():
            return
        self.library.on_app_close()
        self.cfg.save()
        self.root.destroy()


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    root = ctk.CTk()
    # Needs the root to exist (it asks Tk what's installed) but must run
    # before any widget is built, since T.UI/T.MONO are read at construction.
    resolve_fonts()
    root.configure(fg_color=T.BG)
    PazApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
