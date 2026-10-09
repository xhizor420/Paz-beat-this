"""The keyboard-shortcuts sheet: every key the app answers, by tab.

How-to lines used to be printed on the screens themselves ("click
timeline · ←→ step · Space play/pause · G grid"). They are tooltips on
the controls now, and this is the one place the whole list lives. F1
opens it on whichever tab is showing.
"""
from __future__ import annotations

import customtkinter as ctk

from .theme import T, font, px, window_size
from .widgets import tip
from . import icons

TAB_ORDER = ("Convert", "Library", "Vault", "Beat This")

# (group, ((keys, what), ...)) per tab. Keep in step with PazApp._bind_keys
# and each tab's own local bindings.
SHORTCUTS = {
    "Convert": (
        ("Queue", (
            ("F5", "Scan the source folder"),
            ("Ctrl+Enter", "Start converting"),
            ("Esc", "Stop"),
            ("Ctrl+F", "Search the queue"),
            ("H", "Hover previews on or off"),
        )),
        ("Preview", (
            ("Space", "Play / pause"),
            ("← →", "Step one second"),
            ("Shift+← →", "Step ten seconds"),
            ("G", "Contact sheet of the selected clip"),
            ("Click the timeline", "Jump there; drag to scrub"),
        )),
    ),
    "Library": (
        ("Playing", (
            ("Space / Enter", "Play / pause"),
            ("← →", "Seek five seconds"),
            ("Shift+← →", "Seek one second"),
            (",  .", "Step one frame"),
            ("Home / End", "Start / end of the clip"),
            ("0 – 9", "Jump that tenth of the way in"),
            ("M", "Mute"),
        )),
        ("Picking", (
            ("↑ ↓", "Previous / next clip"),
            ("PgUp / PgDn", "Previous / next page"),
            ("V", "Mark used in the current project"),
            ("Shift+V", "Mark used in another project"),
            ("R", "Random clip"),
            ("G", "Contact sheet"),
            ("Ctrl+A", "Select everything on the page"),
        )),
        ("Everything else", (
            ("/  or  Ctrl+F", "Search"),
            ("Esc", "Clear"),
            ("Ctrl+C", "Copy the clip's name"),
            ("Ctrl+Shift+C", "Copy its full path"),
            ("Ctrl+L", "Show or hide tags"),
            ("Ctrl+T", "Theater mode"),
            ("Ctrl+O", "Library folders"),
            ("F5", "Sync the library"),
            ("Ctrl+Shift+R", "Full rebuild"),
        )),
    ),
    "Vault": (
        ("Vault", (
            ("F5", "Look up the pasted list"),
        )),
    ),
    "Beat This": (
        ("Beat This", (
            ("F5", "Analyze the song"),
        )),
    ),
}

ACCENTS = {"Convert": "ACCENT", "Library": "ACCENT2", "Vault": "ACCENT3",
           "Beat This": "ACCENT4"}


class ShortcutsWindow(ctk.CTkToplevel):
    """One per root: asking again brings the open one forward on that tab."""

    def __init__(self, parent, tab_name: str = "Library", on_help=None):
        super().__init__(parent)
        self.title("Keyboard shortcuts")
        self.geometry(window_size(self, 520, 600))
        self.minsize(px(380), px(320))
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._on_help = on_help

        self.picker = ctk.CTkSegmentedButton(
            self, values=list(TAB_ORDER), height=30, font=font(11),
            fg_color=T.SURFACE, selected_color=T.LINE,
            selected_hover_color=T.LINE, unselected_color=T.SURFACE,
            unselected_hover_color=T.BTN_HOV, text_color=T.TEXT,
            command=self.show)
        self.picker.grid(row=0, column=0, sticky="w", padx=14, pady=(14, 8))

        self.body = ctk.CTkScrollableFrame(
            self, fg_color=T.SURFACE, corner_radius=12, border_width=1,
            border_color=T.LINE, scrollbar_button_color=T.LINE,
            scrollbar_button_hover_color=T.FAINT)
        self.body.grid(row=1, column=0, sticky="nsew", padx=14)
        self.body.grid_columnconfigure(1, weight=1)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=14, pady=10)
        if on_help is not None:
            ctk.CTkButton(footer, text="Full help", width=90, height=28,
                          corner_radius=7, font=font(11), fg_color="transparent",
                          hover_color=T.BTN_HOV, text_color=T.DIM,
                          image=icons.icon("help", 14, T.DIM), compound="left",
                          command=self._help).pack(side="left")
        ctk.CTkLabel(footer, text="F1 opens this from any tab", font=font(9),
                     text_color=T.FAINT).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<F1>", lambda e: self.destroy())
        self.show(tab_name)

    def _help(self) -> None:
        on_help = self._on_help
        self.destroy()
        on_help()

    def show(self, tab_name: str) -> None:
        if tab_name not in SHORTCUTS:
            tab_name = "Library"
        self.picker.set(tab_name)
        accent = getattr(T, ACCENTS[tab_name])
        for child in self.body.winfo_children():
            child.destroy()
        row = 0
        for group, keys in SHORTCUTS[tab_name]:
            ctk.CTkLabel(self.body, text=group.upper(), font=font(9, "bold"),
                         text_color=T.FAINT, anchor="w"
                         ).grid(row=row, column=0, columnspan=2, sticky="w",
                                padx=14, pady=(14 if row else 12, 4))
            row += 1
            for combo, what in keys:
                ctk.CTkLabel(self.body, text=combo, font=font(10, mono=True),
                             text_color=accent, anchor="w"
                             ).grid(row=row, column=0, sticky="w", padx=(14, 16), pady=2)
                ctk.CTkLabel(self.body, text=what, font=font(11),
                             text_color=T.DIM, anchor="w"
                             ).grid(row=row, column=1, sticky="w", padx=(0, 14), pady=2)
                row += 1


def open_shortcuts(root, tab_name: str, on_help=None) -> ShortcutsWindow:
    window = getattr(root, "_paz_shortcuts", None)
    try:
        if window is not None and window.winfo_exists():
            window.show(tab_name)
            window.deiconify()
            window.lift()
            window.focus_force()
            return window
    except Exception:
        pass
    window = ShortcutsWindow(root, tab_name, on_help)
    root._paz_shortcuts = window
    return window


def shortcuts_button(parent, tab_name: str, on_help=None) -> ctk.CTkButton:
    """A quiet keyboard icon that opens the sheet on `tab_name`."""
    button = ctk.CTkButton(
        parent, text="", width=28, height=24, corner_radius=6,
        fg_color="transparent", hover_color=T.BTN_HOV,
        image=icons.icon("keyboard", 15, T.FAINT),
        command=lambda: open_shortcuts(parent.winfo_toplevel(), tab_name, on_help))
    tip(button, "Keyboard shortcuts  ·  F1")
    return button
