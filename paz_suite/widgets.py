"""Small reusable widgets shared by both tabs: cards, progress bars, stat
tiles, the floating hover-scrub preview, toast notifications, the active-job
panel and the coloured log.
"""

from __future__ import annotations

import io
import os
import time
import tkinter as tk
from datetime import datetime

import customtkinter as ctk
from PIL import Image, ImageTk

from .theme import T, button_style, font, label_style, mix, pt, px, round_pts
from .format import fmt_size


# Native tk.Menu is a Win32 HMENU: square corners, a 3D bevel on hover,
# and no way to round it. Every popup in the suite is this instead - a
# borderless window with a rounded card drawn on it - so right-click
# matches the rest of the chrome. The methods named like tk.Menu
# (add_command, add_cascade, tk_popup, …) are what the call sites already
# use, so restyling them is still a one-line change.

_MENU_KEY = "#FF00FE"        # chroma-key; not in the palette
_OPEN_MENUS: list = []


class PopupMenu:
    """A rounded, themed popup. Built like tk.Menu so existing call
    sites keep working; drawn like a card so it doesn't look like 1998.

    Compact and quiet: 24px rows, 10pt text, one hairline outline, and a
    neutral hover with a thin bar in the tab's colour - not a block of
    colour behind coloured text, which was the loudest thing on screen
    for something that is open for a second.

    It closes on any click that is not on it, on Escape, and when the app
    stops being the window in front - the last is the one the old menu
    missed: a click on another program never reaches Tk, and an
    always-on-top menu stayed up over whatever was clicked."""

    ROW_H = 24
    PAD_X = 6
    PAD_Y = 5
    MIN_W = 168

    def __init__(self, parent, **kw):
        self.parent = parent.parent if isinstance(parent, PopupMenu) else parent
        self.items: list = []
        # The tab's colour, for the hover bar. The deep shade some callers
        # pass is the old hover fill; the bar wants the bright one.
        self.accent = kw.get("activeforeground") or _ACCENT_FOR.get(
            kw.get("activebackground"), T.ACCENT)
        self._win = None
        self._canvas = None
        self._hover = -1
        self._sub = None
        self._sub_item = -1
        self._watch = None
        self._owner = None
        self._born = 0.0

    def add_command(self, label="", command=None, state="normal",
                    accelerator="", **_kw) -> None:
        self.items.append({"kind": "cmd", "label": label, "command": command,
                           "state": state or "normal", "accelerator": accelerator})

    def add_separator(self, **_kw) -> None:
        self.items.append({"kind": "sep"})

    def add_cascade(self, label="", menu=None, **_kw) -> None:
        self.items.append({"kind": "cascade", "label": label, "menu": menu,
                           "state": "normal", "accelerator": "›"})

    def entryconfigure(self, index, **kw) -> None:
        if not self.items:
            return
        if index == "end":
            index = len(self.items) - 1
        try:
            item = self.items[int(index)]
        except (TypeError, ValueError, IndexError):
            return
        if "label" in kw:
            item["label"] = kw["label"]
        if "state" in kw:
            item["state"] = kw["state"]
        if "command" in kw:
            item["command"] = kw["command"]

    def index(self, which) -> int | None:
        if which == "end":
            return len(self.items) - 1 if self.items else None
        return None

    def delete(self, start=0, end="end") -> None:
        if end == "end":
            self.items.clear()
        else:
            del self.items[int(start):int(end) + 1]

    def grab_release(self) -> None:
        pass

    def tk_popup(self, x: int, y: int) -> None:
        self._show(int(x), int(y))

    # ── draw ────────────────────────────────────────────────────────────

    def _row_count(self) -> int:
        return sum(1 for item in self.items if item["kind"] != "sep")

    def _metrics(self):
        face, small = _menu_fonts()
        widest = 0
        for item in self.items:
            if item["kind"] == "sep":
                continue
            w = face.measure(item["label"])
            extra = item.get("accelerator") or ""
            if extra:
                w += small.measure(extra) + px(18)
            widest = max(widest, w)
        width = max(px(self.MIN_W), widest + px(self.PAD_X) * 2 + px(22))
        height = px(self.PAD_Y) * 2
        for item in self.items:
            height += px(9) if item["kind"] == "sep" else px(self.ROW_H)
        return width, height

    def _show(self, x: int, y: int, owner=None) -> None:
        self.close()
        if not self.items:
            return
        root = self.parent.winfo_toplevel()
        self._owner = owner
        width, height = self._metrics()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        if x + width > screen_w - 8:
            x = max(8, screen_w - width - 8)
        if y + height > screen_h - 8:
            y = max(8, screen_h - height - 8)

        win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        keyed = False
        try:
            win.attributes("-topmost", True)
            win.attributes("-transparentcolor", _MENU_KEY)
            keyed = True
        except tk.TclError:
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
        hole = _MENU_KEY if keyed else T.ELEVATED
        win.configure(bg=hole)
        canvas = tk.Canvas(win, width=width, height=height, bg=hole,
                           highlightthickness=0, bd=0)
        canvas.pack()
        # One shape, one hairline. Two stacked fills drew a ragged double
        # edge where the rounded corners met the chroma key.
        canvas.create_polygon(_round_pts(0, 0, width - 1, height - 1, px(T.R_SM)),
                              fill=T.ELEVATED, outline=T.LINE, width=1)
        self._win = win
        self._canvas = canvas
        self._hover = -1
        self._paint_rows(width)
        canvas.bind("<Motion>", self._on_move)
        canvas.bind("<Leave>", self._on_leave)
        canvas.bind("<ButtonRelease-1>", self._on_click)
        win.bind("<Escape>", lambda e: self._close_all())
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.deiconify()
        win.lift()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        self._born = time.monotonic()
        _OPEN_MENUS.append(self)
        _install_dismiss(root)
        if owner is None:
            self._watch = win.after(_WATCH_MS, self._watch_front)

    def _paint_rows(self, width: int) -> None:
        face, small = _menu_fonts()
        c = self._canvas
        c.delete("row")
        y = px(self.PAD_Y)
        pad = px(self.PAD_X)
        row_h = px(self.ROW_H)
        for i, item in enumerate(self.items):
            if item["kind"] == "sep":
                c.create_line(pad + px(4), y + px(4), width - pad - px(4), y + px(4),
                              fill=T.LINE, tags=("row",))
                y += px(9)
                continue
            item["_y0"] = y
            item["_y1"] = y + row_h
            disabled = item.get("state") == "disabled"
            hover = i == self._hover and not disabled
            if hover:
                c.create_polygon(
                    _round_pts(pad - px(2), y + 1, width - pad + px(2), y + row_h - 1, px(5)),
                    fill=_HOVER, outline="", tags=("row",))
                c.create_line(pad + px(1), y + px(6), pad + px(1), y + row_h - px(6),
                              fill=self.accent, width=px(2), capstyle="round",
                              tags=("row",))
            fill = T.FAINT if disabled else T.TEXT
            c.create_text(pad + px(9), y + row_h // 2, text=item["label"],
                          anchor="w", fill=fill, font=face, tags=("row",))
            extra = item.get("accelerator") or ""
            if extra:
                c.create_text(width - pad - px(6), y + row_h // 2, text=extra,
                              anchor="e", fill=T.FAINT, font=small, tags=("row",))
            y += row_h

    def _item_at(self, y: int) -> int:
        for i, item in enumerate(self.items):
            if item["kind"] == "sep":
                continue
            if item.get("_y0", 0) <= y < item.get("_y1", 0):
                return i
        return -1

    def _on_move(self, event) -> None:
        i = self._item_at(event.y)
        if i == self._hover:
            if i >= 0 and self.items[i]["kind"] == "cascade":
                self._open_sub(i)
            return
        self._hover = i
        self._paint_rows(int(self._canvas.cget("width")))
        item = self.items[i] if i >= 0 else None
        if item and item["kind"] == "cascade":
            self._open_sub(i)
        elif self._sub is not None and (item is None or item["kind"] != "cascade"):
            self._close_sub()

    def _on_leave(self, _event=None) -> None:
        # Keep the highlight if the pointer moved into a submenu.
        if self._sub is not None:
            return
        if self._hover >= 0:
            self._hover = -1
            self._paint_rows(int(self._canvas.cget("width")))

    def _on_click(self, event) -> None:
        i = self._item_at(event.y)
        if i < 0:
            return
        item = self.items[i]
        if item.get("state") == "disabled":
            return
        if item["kind"] == "cascade":
            self._open_sub(i)
            return
        command = item.get("command")
        self._close_all()
        if command:
            command()

    def _open_sub(self, i: int) -> None:
        item = self.items[i]
        sub = item.get("menu")
        if sub is None or self._win is None:
            return
        if self._sub is sub and self._sub_item == i:
            return
        self._close_sub()
        self._sub = sub
        self._sub_item = i
        x = self._win.winfo_rootx() + self._win.winfo_width() - px(4)
        y = self._win.winfo_rooty() + item.get("_y0", 0) - px(self.PAD_Y)
        sub._show(x, y, owner=self)

    def _close_sub(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None
            self._sub_item = -1

    def _watch_front(self) -> None:
        """Close once another program is in front - a click there never
        reaches Tk, so nothing else would close it."""
        self._watch = None
        if self._win is None:
            return
        if not _app_in_front():
            self._close_all()
            return
        try:
            self._watch = self._win.after(_WATCH_MS, self._watch_front)
        except tk.TclError:
            pass

    def owns(self, widget) -> bool:
        """Whether `widget` is this menu or one of its open submenus."""
        name = str(widget)
        menu = self
        while menu is not None:
            if menu._win is not None and (name == str(menu._win)
                                          or name == str(menu._canvas)):
                return True
            menu = menu._sub
        return False

    def close(self) -> None:
        self._close_sub()
        if self._watch is not None and self._win is not None:
            try:
                self._win.after_cancel(self._watch)
            except (tk.TclError, ValueError):
                pass
        self._watch = None
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
        self._win = None
        self._canvas = None
        if self in _OPEN_MENUS:
            _OPEN_MENUS.remove(self)

    def _close_all(self) -> None:
        owner = self
        while owner._owner is not None:
            owner = owner._owner
        owner.close()


# ── menu plumbing ───────────────────────────────────────────────────────

_ACCENT_FOR = {}            # deep shade -> its bright accent, filled in below
_HOVER = ""                 # neutral hover fill, filled in below
_WATCH_MS = 200
_MENU_FONTS: list = []
_DISMISS_ROOTS: set = set()


def _menu_fonts():
    """(label, accelerator) fonts, made once - the old menu made two new
    font objects on every mouse move."""
    if not _MENU_FONTS:
        import tkinter.font as tkfont
        _MENU_FONTS.extend([tkfont.Font(family=T.UI, size=pt(10)),
                            tkfont.Font(family=T.UI, size=pt(9))])
    return _MENU_FONTS[0], _MENU_FONTS[1]


def _install_dismiss(root) -> None:
    """One app-wide click handler, installed once, that closes every open
    menu when a press lands anywhere that is not one. (Each menu used to
    bind its own and remove it from the wrong place, so they piled up.)"""
    key = str(root)
    if key in _DISMISS_ROOTS:
        return
    _DISMISS_ROOTS.add(key)

    def dismiss(event):
        # Runs after the clicked widget's own handlers, so a right-click
        # that has just opened a menu reaches here too - a menu that young
        # was opened by this very press and stays.
        now = time.monotonic()
        for menu in [m for m in _OPEN_MENUS if m._owner is None]:
            if now - menu._born < 0.25 or menu.owns(event.widget):
                continue
            menu.close()

    for seq in ("<ButtonPress-1>", "<ButtonPress-2>", "<ButtonPress-3>"):
        root.bind_all(seq, dismiss, add="+")


def _app_in_front() -> bool:
    """Whether the window in front belongs to this process. True wherever
    that cannot be asked (not Windows), so nothing closes by mistake."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return True


def _menu_palette() -> None:
    _ACCENT_FOR.update({T.ACCENT_DEEP: T.ACCENT, T.ACCENT2_DEEP: T.ACCENT2,
                        T.ACCENT3_DEEP: T.ACCENT3, T.ACCENT4_DEEP: T.ACCENT4,
                        T.WARN_DEEP: T.WARN, T.FAIL_DEEP: T.FAIL})
    global _HOVER
    _HOVER = mix(T.ELEVATED, T.TEXT, 0.08)


_menu_palette()


def _round_pts(x0, y0, x1, y1, r):
    return round_pts(x0, y0, x1, y1, r)


def popup_menu(parent, **kw) -> PopupMenu:
    """A right-click menu styled like the rest of the suite."""
    return PopupMenu(parent, **kw)


def menu_rule(menu) -> None:
    """A separator that matches the theme."""
    menu.add_separator()


def menu_header(menu, text: str) -> None:
    """A dim, non-clickable caption above a group of entries."""
    menu.add_command(label=text.upper(), state="disabled")


# ── tooltips ────────────────────────────────────────────────────────────
#
# How-to text used to be printed in the layout ("click timeline · ←→ step
# · Space play/pause") and sat there on every screen, for good, after the
# first time it had been read. It lives on the control it describes now,
# shown after a short hover, and the full list is the shortcuts sheet (F1).

TIP_DELAY_MS = 450


class _Tip:
    """One borderless window per root, moved and relabelled per widget -
    creating and destroying a toplevel per hover flickers on Windows."""

    def __init__(self, root):
        self.root = root
        self.win = None
        self.label = None
        self.owner = None
        self._show_job = None
        self._hide_job = None

    def _build(self):
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        self._keyed = False
        try:
            win.attributes("-topmost", True)
            win.attributes("-transparentcolor", _MENU_KEY)
            self._keyed = True
        except tk.TclError:
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
        hole = _MENU_KEY if self._keyed else T.ELEVATED
        win.configure(bg=hole)
        self.canvas = tk.Canvas(win, bg=hole, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.label = tk.Label(self.canvas, bg=T.ELEVATED, fg=T.TEXT,
                              font=(T.UI, pt(10)), justify="left",
                              wraplength=px(300))
        self.win = win

    def enter(self, widget, text):
        self._cancel("_hide_job")
        if self.owner is widget and self.win is not None and self.win.winfo_viewable():
            return
        self._cancel("_show_job")
        self.owner = widget
        self._show_job = self.root.after(TIP_DELAY_MS, lambda: self._show(widget, text))

    def leave(self, widget):
        self._cancel("_show_job")
        # Later rather than now: a CTk button is a canvas with a label on
        # top, and crossing from one to the other is a Leave and an Enter.
        self._cancel("_hide_job")
        self._hide_job = self.root.after(60, self.hide)

    def hide(self):
        self._cancel("_show_job")
        self._cancel("_hide_job")
        self.owner = None
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                pass

    def _cancel(self, name):
        job = getattr(self, name)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except (ValueError, tk.TclError):
                pass
            setattr(self, name, None)

    def _show(self, widget, text):
        self._show_job = None
        try:
            if not widget.winfo_viewable():
                return
            if self.win is None:
                self._build()
            self.label.configure(text=text() if callable(text) else text)
            self.label.update_idletasks()
            pad = px(10)
            lw, lh = self.label.winfo_reqwidth(), self.label.winfo_reqheight()
            w, h = lw + pad * 2, lh + pad * 2
            hole = _MENU_KEY if self._keyed else T.ELEVATED
            self.canvas.configure(width=w, height=h, bg=hole)
            self.canvas.delete("card")
            radius = px(T.R_LG)
            self.canvas.create_polygon(
                _round_pts(1, 1, w - 2, h - 2, radius),
                smooth=True, fill=T.LINE, tags="card")
            self.canvas.create_polygon(
                _round_pts(2, 2, w - 3, h - 3, max(radius - 1, 1)),
                smooth=True, fill=T.ELEVATED, tags="card")
            self.canvas.create_window(w // 2, h // 2, window=self.label, tags="card")
            x = widget.winfo_rootx() + (widget.winfo_width() - w) // 2
            y = widget.winfo_rooty() + widget.winfo_height() + px(6)
            screen_w = widget.winfo_screenwidth()
            x = max(4, min(x, screen_w - w - 4))
            if y + h > widget.winfo_screenheight() - 4:
                y = widget.winfo_rooty() - h - px(6)
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self.win.deiconify()
            self.win.lift()
        except tk.TclError:
            pass


def _tip_for(widget) -> _Tip:
    root = widget.winfo_toplevel()
    tip_ = getattr(root, "_paz_tip", None)
    if tip_ is None:
        tip_ = _Tip(root)
        root._paz_tip = tip_
    return tip_


def tip(widget, text) -> None:
    """Show `text` (or the result of calling it) under `widget` on hover."""
    def enter(_e=None):
        _tip_for(widget).enter(widget, text)

    def leave(_e=None):
        _tip_for(widget).leave(widget)

    def press(_e=None):
        _tip_for(widget).hide()

    for sequence, handler in (("<Enter>", enter), ("<Leave>", leave),
                              ("<ButtonPress>", press)):
        try:
            widget.bind(sequence, handler, add="+")
        except (TypeError, ValueError, NotImplementedError):
            pass


def ask_yes(parent, title: str, message: str, *, yes: str = "Yes",
            no: str = "Cancel", danger: bool = False) -> bool:
    """A confirm in the app's own window, instead of the system box.

    `danger` fills the yes button with the fail colour. Deletes use it.
    """
    from .theme import window_size
    answer = {"ok": False}
    host = parent.winfo_toplevel()
    win = ctk.CTkToplevel(host)
    win.title(title)
    win.configure(fg_color=T.BG)
    win.transient(host)
    win.resizable(False, False)
    # Tall enough for the wrapped message. A fixed height clipped the
    # longer confirms (verify, quit-while-encoding) under the buttons.
    lines = 0
    for para in message.split("\n"):
        lines += max(1, (len(para) + 47) // 48)
    win.geometry(window_size(win, 460, min(150 + lines * 20, 520)))

    ctk.CTkLabel(win, text=title, font=font(15, "bold", display=True),
                 text_color=T.TEXT, anchor="w").pack(
                     fill="x", padx=18, pady=(16, 6))
    ctk.CTkLabel(win, text=message, font=font(11), text_color=T.DIM,
                 justify="left", anchor="w", wraplength=px(410)).pack(
                     fill="x", padx=18)

    row = ctk.CTkFrame(win, fg_color="transparent")
    row.pack(fill="x", padx=18, pady=16)

    def accept(_event=None):
        answer["ok"] = True
        win.destroy()

    if danger:
        yes_btn = ctk.CTkButton(
            row, text=yes, width=120, height=32, corner_radius=T.R_SM,
            fg_color=T.FAIL, hover_color=mix(T.FAIL, "#FFFFFF", 0.18),
            text_color=T.BG, font=font(12, "bold"), command=accept)
    else:
        yes_btn = PrimaryButton(row, text=yes, width=120, height=32,
                                corner_radius=T.R_SM, command=accept)
    yes_btn.pack(side="right")
    if no:
        ctk.CTkButton(row, text=no, width=90, height=32, corner_radius=T.R_SM,
                      command=win.destroy, **button_style("text")).pack(
                          side="right", padx=(0, 8))

    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.bind("<Escape>", lambda e: win.destroy())
    win.bind("<Return>", accept)
    try:
        win.grab_set()
    except tk.TclError:
        pass
    win.after(60, win.lift)
    win.wait_window()
    return answer["ok"]


def tell(parent, title: str, message: str) -> None:
    """An error or notice, one button, same window as ask_yes."""
    ask_yes(parent, title, message, yes="OK", no="")


class PrimaryButton(ctk.CTkButton):
    """A panel's one main action - see theme.button_style.

    CTkButton has no disabled fill, so a disabled primary kept its bright
    fill under faint text: the one button that looked most pressable was
    the one that could not be pressed, and its label could not be read.
    This drops to the neutral fill whenever it is disabled."""

    def __init__(self, master, accent: str = "", **kwargs):
        self._accent = accent or T.ACCENT
        style = button_style("primary", self._accent)
        style.update(kwargs)
        super().__init__(master, **style)
        self._paint_state()

    def configure(self, require_redraw=False, **kwargs):
        if "fg_color" in kwargs:
            self._accent = kwargs.pop("fg_color")
            kwargs["hover_color"] = mix(self._accent, "#FFFFFF", 0.22)
        super().configure(require_redraw, **kwargs)
        self._paint_state()

    def _paint_state(self) -> None:
        on = self.cget("state") != "disabled"
        super().configure(fg_color=self._accent if on else T.BTN,
                          text_color=T.BG, text_color_disabled=T.FAINT)


# ── empty states ────────────────────────────────────────────────────────
#
# "Select a clip" in body text in the middle of a dark panel read as
# something that had failed to load. A faint icon over one short line
# reads as a place waiting for something.

EMPTY_INK = mix(T.FAINT, T.SURFACE, 0.45)   # quieter than the line under it

def draw_empty(canvas, cx: int, cy: int, icon_name: str, text: str,
               tag: str = "empty") -> None:
    """An empty state drawn onto a raw canvas, centred on (cx, cy)."""
    from . import icons
    size = px(30)
    canvas.create_image(cx, cy - px(12), image=icons.photo(icon_name, size, EMPTY_INK),
                        tags=(tag,))
    canvas.create_text(cx, cy + px(18), text=text, fill=T.FAINT,
                       font=(T.UI, pt(10)), tags=(tag,))


def empty_overlay(tree, icon_name: str, text: str) -> None:
    """The same empty state over a ttk.Treeview whenever it has no rows.
    A Treeview has no event for rows coming and going, so it is checked
    a few times a second - one get_children() call, nothing to notice."""
    from tkinter import ttk
    style = str(tree.cget("style") or "Treeview")
    bg = ttk.Style().lookup(style, "fieldbackground") or T.SURFACE
    width, height = px(340), px(96)
    canvas = tk.Canvas(tree.master, width=width, height=height, bg=bg,
                       highlightthickness=0, bd=0)
    draw_empty(canvas, width // 2, height // 2, icon_name, text)
    shown = [False]

    def check() -> None:
        try:
            empty = not tree.get_children()
        except tk.TclError:
            return
        if empty != shown[0]:
            shown[0] = empty
            if empty:
                canvas.place(in_=tree, relx=0.5, rely=0.5, anchor="center")
                # Not canvas.lift(): on a Canvas that raises canvas items.
                tk.Misc.lift(canvas)
            else:
                canvas.place_forget()
        tree.after(400, check)

    check()


# How far in a rounded well (radius T.R_SM) its square content sits, so
# the content's corners fall inside the curve - r * (1 - 1/sqrt 2) is the
# least that clears it.
WELL_INSET = 4


def rounded_well(widget, bg: str = "", **grid) -> ctk.CTkFrame:
    """Sit a square raw-Tk widget - a ttk table, a Text - in a rounded
    frame of its own colour, so its corners stop showing as hard ones
    inside a rounded card. `grid` is where the widget itself would have
    gone; the well takes that cell and the widget fills the well.

    The well is the widget's sibling and the widget is managed `in_` it,
    so nothing that holds the widget or its parent has to change."""
    if not bg:
        try:
            bg = str(widget.cget("bg"))
        except tk.TclError:
            from tkinter import ttk
            style = str(widget.cget("style") or "Treeview")
            bg = ttk.Style().lookup(style, "fieldbackground") or T.ROW
    well = ctk.CTkFrame(widget.master, fg_color=bg, corner_radius=T.R_SM,
                        border_width=0)
    well.grid(**grid)
    well.grid_rowconfigure(0, weight=1)
    well.grid_columnconfigure(0, weight=1)
    widget.grid(in_=well, row=0, column=0, sticky="nsew",
                padx=WELL_INSET, pady=WELL_INSET)
    tk.Misc.lift(widget)
    return well


class EmptyState(ctk.CTkFrame):
    """The same, as a widget for CTk containers."""

    def __init__(self, parent, icon_name: str, text: str, **kw):
        from . import icons
        kw.setdefault("fg_color", "transparent")
        super().__init__(parent, **kw)
        self.icon = ctk.CTkLabel(self, text="", image=icons.icon(icon_name, 28, EMPTY_INK))
        self.icon.pack(pady=(0, 6))
        self.text = ctk.CTkLabel(self, text=text, font=font(10), text_color=T.FAINT)
        self.text.pack()

    def set_text(self, text: str) -> None:
        self.text.configure(text=text)


class Card(ctk.CTkFrame):
    """A panel with a hairline border, optionally titled."""

    def __init__(self, parent, title: str = "", **kw):
        kw.setdefault("fg_color", T.SURFACE)
        kw.setdefault("corner_radius", T.R_LG)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", T.LINE)
        super().__init__(parent, **kw)
        self.body = self
        if title:
            self.grid_columnconfigure(0, weight=1)
            head = ctk.CTkLabel(self, text=title.upper(), **label_style(), anchor="w")
            head.grid(row=0, column=0, sticky="ew", padx=14, pady=(11, 0))


class Bar(ctk.CTkFrame):
    """Thin progress bar with an optional right-aligned readout."""

    def __init__(self, parent, height: int = 5, color: str = T.ACCENT, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self.grid_columnconfigure(0, weight=1)
        self._color = color
        self._track = ctk.CTkFrame(self, fg_color=T.LINE_SOFT, height=height,
                                    corner_radius=height // 2)
        self._track.grid(row=0, column=0, sticky="ew")
        self._track.grid_propagate(False)
        self._fill = ctk.CTkFrame(self._track, fg_color=color, height=height,
                                   corner_radius=height // 2)

    def set(self, fraction: float, color: str | None = None) -> None:
        if color and color != self._color:
            self._color = color
            self._fill.configure(fg_color=color)
        fraction = max(0.0, min(float(fraction), 1.0))
        if fraction <= 0.0:
            # Not drawn at all. A rounded fill has a minimum width of its
            # two corners, so an empty bar showed a dot of colour at its
            # left end - an idle Convert tab looked mid-encode.
            self._fill.place_forget()
            return
        self._fill.place(x=0, y=0, relheight=1.0, relwidth=fraction)

    def reset(self) -> None:
        self.set(0)


class MiniProgress(Bar):
    """A small progress bar that is only there while something runs.

    For background work - tag fetches, syncs, scans - that deserves a
    glance, not a band across the window. Hidden at 0, shown in between,
    and gone a moment after it reaches the end, so a finished job does not
    leave a full bar behind for the rest of the session.
    """

    LINGER_MS = 1500

    def __init__(self, parent, width: int = 120, height: int = 3,
                 color: str = T.ACCENT2, **kw):
        super().__init__(parent, height=height, color=color, width=width, **kw)
        # The frame's own height too, not just the track's: a CTkFrame left
        # to its default is 200px tall, and with propagation off this one
        # stretched the status row under the gallery to match.
        self.configure(height=height)
        self.grid_propagate(False)
        self._track.configure(width=width)
        self._shown = None
        self._hide_job = None
        self._geometry = None

    def place_in(self, **grid_kw) -> None:
        """Grid it, remembering where, and start hidden."""
        self._geometry = grid_kw
        self.grid(**grid_kw)
        self.grid_remove()
        self._shown = False

    def _show(self, on: bool) -> None:
        if on == self._shown:
            return
        self._shown = on
        if on:
            self.grid()
        else:
            self.grid_remove()

    def set(self, fraction: float, color: str | None = None) -> None:
        super().set(fraction, color)
        if self._hide_job is not None:
            try:
                self.after_cancel(self._hide_job)
            except ValueError:
                pass
            self._hide_job = None
        if fraction <= 0:
            self._show(False)
            return
        self._show(True)
        if fraction >= 1:
            self._hide_job = self.after(self.LINGER_MS, lambda: self._show(False))


class StatTile(ctk.CTkFrame):
    """One number, one label. The number is mono so tiles stay aligned."""

    def __init__(self, parent, label: str, color: str = T.TEXT, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=T.R_LG,
                          border_width=1, border_color=T.LINE, **kw)
        self.color = color
        self.value = ctk.CTkLabel(self, text="—", font=font(21, "bold", mono=True),
                                   text_color=T.DIM)
        self.value.pack(pady=(11, 0))
        ctk.CTkLabel(self, text=label.upper(), **label_style()).pack(pady=(2, 11))

    def set(self, text: str) -> None:
        # Colour only when there is something to count. A green 0 and a
        # red 0 side by side read as news when nothing has happened.
        quiet = str(text).strip() in ("", "0", "—", "-", "0.0")
        self.value.configure(text=text, text_color=T.DIM if quiet else self.color)


class PeekWindow:
    """
    A floating, borderless preview bubble that follows the mouse.

    Used for hovering a queue row, a gallery card, or the inspector
    timeline. It never takes focus and never appears in the taskbar. A
    dedicated progress strip sits between the frame and the caption,
    filled to match how far into the clip that frame is - the same
    "scrub the thumbnail" cue YouTube shows on hover. It's a strip of
    its own rather than an overlay on the frame, so it stays visible no
    matter what's in the video (a bar drawn over dark footage used to all
    but disappear).
    """

    # Hand-drawn, at 100%. The bubble is a raw tk.Canvas, which nothing
    # scales for us, so every one of these is run through px() on the way
    # out. Read each time rather than resolved once in __init__: the app
    # builds this object four lines BEFORE it applies the display
    # scaling, so anything worked out in the constructor is worked out
    # at 100% whatever the display is doing. (Measured, not assumed -
    # the first attempt did exactly that and produced a 380px bubble on
    # a 150% screen.) px() is a multiply and a round.
    DESIGN_W, DESIGN_H, DESIGN_CAP = 380, 214, 24
    DESIGN_BAR_H = 5
    DESIGN_EDGE = 3

    W = property(lambda self: px(self.DESIGN_W))
    H = property(lambda self: px(self.DESIGN_H))
    CAP = property(lambda self: px(self.DESIGN_CAP))
    BAR_H = property(lambda self: px(self.DESIGN_BAR_H))
    EDGE = property(lambda self: px(self.DESIGN_EDGE))

    def __init__(self, master):
        self.master = master
        self.win = None
        self.canvas = None
        self._img = None
        self._visible = False

    def _ensure(self):
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.master)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.win.withdraw()
        frame = tk.Frame(self.win, bg=T.LINE, bd=0)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, width=self.W, height=self.H + self.BAR_H + self.CAP,
                                 bg=T.INPUT, highlightthickness=0, bd=0)
        self.canvas.pack(padx=1, pady=1)

    def _place(self, x_root: int, y_root: int) -> None:
        w, h = self.W + 2, self.H + self.BAR_H + self.CAP + 2
        x = x_root + px(18)
        y = y_root - h - px(16)
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        if x + w > screen_w - px(8):
            x = x_root - w - px(18)
        if y < px(8):
            y = y_root + px(24)
        if y + h > screen_h - px(8):
            y = screen_h - h - px(8)
        self.win.geometry(f"{w}x{h}+{max(int(x), 0)}+{max(int(y), 0)}")

    def _progress_bar(self, fraction: float | None) -> None:
        y0, y1 = self.H, self.H + self.BAR_H
        self.canvas.create_rectangle(0, y0, self.W, y1, fill=T.SURFACE, outline="")
        if fraction is None:
            return
        fill_w = max(0.0, min(fraction, 1.0)) * self.W
        if fill_w <= 0:
            return
        self.canvas.create_rectangle(0, y0, fill_w, y1, fill=T.ACCENT, outline="")
        # a bright leading edge, like the scrub head on a real seek bar
        head = max(fill_w - self.EDGE, 0)
        self.canvas.create_rectangle(head, y0, fill_w, y1, fill=T.ACCENT_HOV, outline="")

    def _caption(self, title: str, sub: str) -> None:
        top = self.H + self.BAR_H
        y = top + self.CAP // 2
        self.canvas.create_rectangle(0, top, self.W, top + self.CAP,
                                      fill=T.ELEVATED, outline="")
        if title:
            self.canvas.create_text(px(10), y, text=title, fill=T.DIM,
                                     font=(T.UI, pt(9)), anchor="w")
        if sub:
            self.canvas.create_text(self.W - px(10), y, text=sub, fill=T.ACCENT,
                                     font=(T.MONO, pt(9)), anchor="e")

    def show_frame(self, data: bytes | None, title: str, sub: str,
                   x_root: int, y_root: int, fraction: float | None = None) -> None:
        self._ensure()
        self.canvas.delete("all")
        if data:
            try:
                image = Image.open(io.BytesIO(data))
                # thumbnail() only ever shrinks, so a frame smaller than
                # the bubble used to sit marooned in the middle of it at
                # its own tiny size. Fit to the box in both directions.
                scale = min(self.W / max(image.width, 1),
                            self.H / max(image.height, 1))
                if abs(scale - 1.0) > 0.01:
                    image = image.resize(
                        (max(int(image.width * scale), 1),
                         max(int(image.height * scale), 1)), Image.LANCZOS)
                self._img = ImageTk.PhotoImage(image)
                self.canvas.create_image(self.W // 2, self.H // 2,
                                          image=self._img, anchor="center")
            except Exception:
                data = None
        if not data:
            self.canvas.create_text(self.W // 2, self.H // 2, text="no frame",
                                     fill=T.FAINT, font=(T.UI, pt(10)))
        self._progress_bar(fraction)
        self._caption(title, sub)
        self._place(x_root, y_root)
        self.win.deiconify()
        self._visible = True

    def show_text(self, message: str, title: str,
                  x_root: int, y_root: int) -> None:
        self._ensure()
        self.canvas.delete("all")
        self._img = None
        self.canvas.create_text(self.W // 2, self.H // 2, text=message,
                                 fill=T.FAINT, font=(T.UI, pt(10)))
        self._progress_bar(None)
        self._caption(title, "")
        self._place(x_root, y_root)
        self.win.deiconify()
        self._visible = True

    def hide(self) -> None:
        if self.win is not None and self._visible:
            self.win.withdraw()
            self._visible = False


class BigPeekWindow(PeekWindow):
    """The Library's hover preview: the same bubble, larger. Its cards are
    small, and a 380-wide bubble read as tiny on a 4K screen. Not larger
    than this: the hover frame it shows is made 640 wide, and past about
    one and a half times that it goes soft."""
    DESIGN_W, DESIGN_H, DESIGN_CAP = 620, 349, 26


class Toaster:
    """Small self-dismissing notifications in the window's bottom-right corner."""

    COLOURS = {"ok": T.OK, "warn": T.WARN, "fail": T.FAIL,
               "info": T.DIM, "accent": T.ACCENT}

    def __init__(self, root):
        self.root = root
        self.win = None
        self.label = None
        self.stripe = None
        self._after = None

    def _ensure(self):
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.win.withdraw()
        outer = tk.Frame(self.win, bg=T.LINE, bd=0)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=T.ELEVATED, bd=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.stripe = tk.Frame(inner, bg=T.ACCENT, width=px(4))
        self.stripe.pack(side="left", fill="y")
        self.label = tk.Label(inner, bg=T.ELEVATED, fg=T.TEXT,
                               font=(T.UI, pt(10)), justify="left",
                               wraplength=px(330), padx=px(12), pady=px(10))
        self.label.pack(side="left", fill="both", expand=True)

    def show(self, text: str, level: str = "ok", ms: int = 4200) -> None:
        try:
            if not self.root.winfo_viewable():
                return
        except tk.TclError:
            return
        self._ensure()
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except ValueError:
                pass
        self.stripe.configure(bg=self.COLOURS.get(level, T.DIM))
        self.label.configure(text=text)
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        x = self.root.winfo_rootx() + self.root.winfo_width() - w - px(24)
        y = self.root.winfo_rooty() + self.root.winfo_height() - h - px(24)
        self.win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.win.deiconify()
        self._after = self.root.after(ms, self.hide)

    def hide(self) -> None:
        self._after = None
        if self.win is not None:
            self.win.withdraw()


class JobPanel(ctk.CTkFrame):
    """Live rows for whatever is encoding right now, one per worker.

    Only on screen while there is something to show. It used to sit there
    between runs as a full-width card reading "Idle" - the same word the
    status pill in the tab's header already says - taking room from the
    queue table (Convert) and the log (Beat This) below it. grid_remove()
    keeps the place it was gridded into, so it comes back to exactly
    that spot when a job starts.
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=T.R_LG,
                          border_width=1, border_color=T.LINE, **kw)
        self.grid_columnconfigure(0, weight=1)
        self._rows: dict = {}

    def grid(self, **kw):
        super().grid(**kw)
        if not self._rows:
            self.grid_remove()

    def _next_row(self) -> int:
        return len(self._rows) + 1

    def start(self, job_id: str, name: str) -> None:
        if job_id in self._rows:
            self.finish(job_id)

        holder = ctk.CTkFrame(self, fg_color="transparent")
        holder.grid(row=self._next_row(), column=0, sticky="ew", padx=14, pady=(9, 3))
        holder.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(holder, text=name, font=font(11), text_color=T.TEXT,
                              anchor="w")
        title.grid(row=0, column=0, sticky="ew")
        readout = ctk.CTkLabel(holder, text="starting", font=font(10, mono=True),
                                text_color=T.DIM, anchor="e")
        readout.grid(row=0, column=1, sticky="e", padx=(10, 0))

        bar = Bar(holder)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 6))

        self._rows[job_id] = {"holder": holder, "bar": bar, "readout": readout}
        super().grid()

    def set_progress(self, job_id: str, fraction: float, text: str,
                      color: str = T.ACCENT) -> None:
        row = self._rows.get(job_id)
        if not row:
            return
        row["bar"].set(fraction, color)
        row["readout"].configure(text=text)

    def finish(self, job_id: str) -> None:
        row = self._rows.pop(job_id, None)
        if row:
            row["holder"].destroy()
        if not self._rows:
            self.grid_remove()

    def clear(self) -> None:
        for job_id in list(self._rows):
            self.finish(job_id)


class LogView(ctk.CTkFrame):
    """Coloured log with an errors-only filter and copy/save."""

    LEVELS = {
        "info": T.DIM,
        "ok":   T.OK,
        "warn": T.WARN,
        "fail": T.FAIL,
        "head": T.ACCENT,
    }

    def __init__(self, parent, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=T.R_LG,
                          border_width=1, border_color=T.LINE, **kw)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._records: list = []
        self._errors_only = False

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 4))
        head.grid_columnconfigure(0, weight=1)
        self.header_label = ctk.CTkLabel(head, text="LOG", **label_style())
        self.header_label.grid(row=0, column=0, sticky="w")

        # Minor actions, so the text kind - see theme.button_style.
        self.filter_btn = ctk.CTkButton(
            head, text="Errors only", width=84, height=22, corner_radius=T.R_SM,
            command=self.toggle_filter, **button_style("text"))
        self.filter_btn.grid(row=0, column=1, padx=(0, 6))

        ctk.CTkButton(head, text="Clear", width=52, height=22, corner_radius=T.R_SM,
                      command=self.clear, **button_style("text")).grid(row=0, column=2)

        self.text = tk.Text(
            self, wrap=tk.WORD, font=(T.MONO, pt(10)), bg=T.INPUT, fg=T.DIM,
            # height is in lines, so it follows the font by itself;
            # the padding is pixels and does not.
            relief=tk.FLAT, padx=px(12), pady=px(9), borderwidth=0,
            insertbackground=T.DIM, selectbackground=T.ACCENT_DEEP,
            selectforeground=T.TEXT, state="disabled", height=8,
        )
        rounded_well(self.text, T.INPUT, row=1, column=0, sticky="nsew",
                     padx=(10, 0), pady=(0, 10))
        for name, colour in self.LEVELS.items():
            self.text.tag_configure(name, foreground=colour)
        self.text.tag_configure("stamp", foreground=T.FAINT)

        scroll = ctk.CTkScrollbar(self, command=self.text.yview, width=12,
                                   button_color=T.LINE, button_hover_color=T.FAINT)
        scroll.grid(row=1, column=1, sticky="ns", padx=(2, 6), pady=(0, 10))
        self.text.configure(yscrollcommand=scroll.set)

    def write(self, message: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._records.append((stamp, message, level))
        if self._errors_only and level not in ("fail", "warn"):
            return
        self._append(stamp, message, level)

    def _append(self, stamp: str, message: str, level: str) -> None:
        self.text.configure(state="normal")
        self.text.insert(tk.END, f"{stamp}  ", "stamp")
        self.text.insert(tk.END, message + "\n", level)
        self.text.see(tk.END)
        self.text.configure(state="disabled")

    def toggle_filter(self) -> None:
        self._errors_only = not self._errors_only
        self.filter_btn.configure(
            fg_color=T.ACCENT_DEEP if self._errors_only else "transparent",
            text_color=T.ACCENT if self._errors_only else T.DIM)
        self._rerender()

    def _rerender(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.configure(state="disabled")
        for stamp, message, level in self._records:
            if self._errors_only and level not in ("fail", "warn"):
                continue
            self._append(stamp, message, level)

    def clear(self) -> None:
        self._records = []
        self._rerender()

    def dump(self) -> str:
        return "\n".join(f"{s}  {m}" for s, m, _ in self._records)


class LibraryBar(ctk.CTkFrame):
    """
    One-line census of the whole library: how much footage is edit-ready
    versus still waiting on upscaling, in clips and gigabytes.
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=T.R_LG,
                          border_width=1, border_color=T.LINE, **kw)
        self.grid_columnconfigure(0, weight=1)
        self.text = ctk.CTkLabel(self, text="Library: scanning",
                                  font=font(10, mono=True), text_color=T.DIM,
                                  anchor="w")
        self.text.grid(row=0, column=0, sticky="ew", padx=14, pady=(9, 4))
        self.canvas = tk.Canvas(self, bg=T.SURFACE, highlightthickness=0,
                                 borderwidth=0, height=7)
        self.canvas.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        self.canvas.bind("<Configure>", lambda e: self._render())
        self._parts = []

    def set(self, pool_n, pool_b, wait_n, wait_b, unsorted_n) -> None:
        bits = [f"Edit pool {pool_n} clips · {fmt_size(pool_b)}",
                f"awaiting upscale {wait_n} · {fmt_size(wait_b)}"]
        if unsorted_n:
            bits.append(f"unsorted {unsorted_n}")
        self.text.configure(text="   |   ".join(bits))
        self._parts = [(pool_n, T.OK), (wait_n, T.WARN), (unsorted_n, T.FAINT)]
        self._render()

    def _render(self) -> None:
        c = self.canvas
        c.delete("all")
        width = c.winfo_width()
        total = sum(n for n, _ in self._parts)
        if width < 20 or not total:
            c.create_line(3, 3, width - 3, 3, fill=T.LINE_SOFT, width=3,
                          capstyle="round")
            return
        # Round ends on the bar as a whole, square joins between parts.
        h = 7
        r = h / 2
        parts = [(n, colour) for n, colour in self._parts if n]
        x = 0
        for i, (n, colour) in enumerate(parts):
            w = max(int(width * n / total), 2)
            x1 = width if i == len(parts) - 1 else min(x + w, width)
            left = x + (r if i == 0 else 0)
            right = x1 - (r if i == len(parts) - 1 else 0)
            if right > left:
                c.create_rectangle(left, 0, right, h, fill=colour, outline="")
            if i == 0:
                c.create_oval(x, 0, x + h, h, fill=colour, outline="")
            if i == len(parts) - 1:
                c.create_oval(x1 - h, 0, x1, h, fill=colour, outline="")
            x = x1 + 1
