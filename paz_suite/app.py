"""PAZ Suite application shell: one window, a Convert/Library/Vault/Beat
This tabview, and the shared services (config, e621 cache, thumbnail
cache, toasts, hover peek) the tabs draw on. Also owns the keyboard-
shortcut dispatch, since several shortcuts mean different things on each
tab and must only fire for whichever one is currently visible.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from .theme import T, font, mark_photo
from .config import AppConfig
from .e621 import E621Meta, APP_NAME, APP_VERSION
from .media import ThumbCache, set_probe_cache_limit
from .widgets import Toaster, PeekWindow
from .convert_tab import ConvertTab
from .library_tab import LibraryTab
from .vault_tab import VaultTab
from .beat_tab import BeatTab
from .settings_window import SettingsWindow

TAB_NAMES = ("Convert", "Library", "Vault", "Beat This")


class PazApp:

    def __init__(self, root: ctk.CTk):
        self.root = root
        self.cfg = AppConfig.load()
        self.emeta = E621Meta()
        set_probe_cache_limit(self.cfg.probe_cache_limit)
        self.cache = ThumbCache(limit=self.cfg.frame_cache_limit)  # shared frame/thumbnail cache
        self.toaster = Toaster(root)
        self.peek = PeekWindow(root)
        self._icon = None
        self._header_icon = None

        root.geometry("1760x1020")
        # Every panel below (gallery columns, the inspector/player, the
        # queue table) already recalculates its own layout on resize, so
        # this is a floor for legibility, not a hard requirement - the
        # window is just as usable maximized on a 4K display as tiled on a
        # 13" laptop screen.
        root.minsize(1180, 700)
        root.configure(fg_color=T.BG)

        self._build_header()

        self.tabview = ctk.CTkTabview(
            root, fg_color=T.BG, corner_radius=10, anchor="w",
            segmented_button_fg_color=T.SURFACE,
            segmented_button_selected_color=T.ACCENT_DEEP,
            segmented_button_selected_hover_color=T.ACCENT_DEEP,
            segmented_button_unselected_color=T.SURFACE,
            segmented_button_unselected_hover_color=T.BTN_HOV,
            segmented_button_font=font(13, "bold"),
            text_color=T.TEXT, command=self._on_tab_changed)
        self.tabview.pack(fill="both", expand=True, padx=0, pady=(6, 0))
        for name in TAB_NAMES:
            self.tabview.add(name)
        # CTkTabview hardcodes a 26px-tall segmented button with no public
        # way to change it - going through the private attribute is the
        # only way to get a strip that reads as a real navigation control
        # instead of a small default widget dropped at the top of the page.
        self.tabview._segmented_button.configure(height=36)

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

    # ── header (shared identity, above the tab strip) ──────────────────────
    #
    # Each tab still draws its own small colour-coded brand block (pink for
    # Convert, violet for Library, teal for Vault) so which mode you're in
    # is obvious at a glance without reading the tab label - this bar is
    # just the one piece of chrome no tab should have to own twice: the
    # suite's own name.

    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self.root, fg_color=T.SURFACE, corner_radius=0, height=40)
        bar.pack(fill="x", side="top")
        bar.grid_propagate(False)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=16, pady=6)
        self._header_icon = mark_photo(18, T.ACCENT)
        tk.Label(left, image=self._header_icon, bg=T.SURFACE, bd=0
                 ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(left, text=APP_NAME, font=font(12, "bold"),
                     text_color=T.DIM).pack(side="left")

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
    # widgets; recolouring the shared tab strip to match whichever one is
    # active makes the switcher read as "you are here" instead of one flat,
    # generic control that looks the same no matter which tab is showing.
    _TAB_ACCENTS = {"Convert":   (T.ACCENT_DEEP, T.ACCENT),
                    "Library":   (T.ACCENT2_DEEP, T.ACCENT2),
                    "Vault":     (T.ACCENT3_DEEP, T.ACCENT3),
                    "Beat This": (T.ACCENT4_DEEP, T.ACCENT4)}

    def _style_tabs(self) -> None:
        active = self.tabview.get()
        deep, bright = self._TAB_ACCENTS.get(active, (T.ACCENT_DEEP, T.ACCENT))
        self.tabview._segmented_button.configure(
            selected_color=deep, selected_hover_color=deep)
        buttons = self.tabview._segmented_button._buttons_dict
        for name in TAB_NAMES:
            if name in buttons:
                buttons[name].configure(text_color=bright if name == active else T.DIM)

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
    root.configure(fg_color=T.BG)
    PazApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
