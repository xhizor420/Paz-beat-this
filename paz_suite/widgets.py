"""Small reusable widgets shared by both tabs: cards, progress bars, stat
tiles, the floating hover-scrub preview, toast notifications, the active-job
panel and the coloured log.
"""

from __future__ import annotations

import io
import tkinter as tk
from datetime import datetime

import customtkinter as ctk
from PIL import Image, ImageTk

from .theme import T, font
from .format import fmt_size


class Card(ctk.CTkFrame):
    """A panel with a hairline border, optionally titled."""

    def __init__(self, parent, title: str = "", **kw):
        kw.setdefault("fg_color", T.SURFACE)
        kw.setdefault("corner_radius", 10)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", T.LINE)
        super().__init__(parent, **kw)
        self.body = self
        if title:
            self.grid_columnconfigure(0, weight=1)
            head = ctk.CTkLabel(self, text=title.upper(), font=font(10, "bold"),
                                 text_color=T.FAINT, anchor="w")
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
        self._fill.place(x=0, y=0, relheight=1.0, relwidth=0)

    def set(self, fraction: float, color: str | None = None) -> None:
        if color and color != self._color:
            self._color = color
            self._fill.configure(fg_color=color)
        self._fill.place(x=0, y=0, relheight=1.0,
                          relwidth=max(0.0, min(float(fraction), 1.0)))

    def reset(self) -> None:
        self.set(0)


class StatTile(ctk.CTkFrame):
    """One number, one label. The number is mono so tiles stay aligned."""

    def __init__(self, parent, label: str, color: str = T.TEXT, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=12,
                          border_width=1, border_color=T.LINE, **kw)
        self.value = ctk.CTkLabel(self, text="--", font=font(21, "bold", mono=True),
                                   text_color=color)
        self.value.pack(pady=(11, 0))
        ctk.CTkLabel(self, text=label.upper(), font=font(9, "bold"),
                     text_color=T.FAINT).pack(pady=(2, 11))

    def set(self, text: str) -> None:
        self.value.configure(text=text)


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

    W, H, CAP = 380, 214, 24
    BAR_H = 5

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
        x = x_root + 18
        y = y_root - h - 16
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        if x + w > screen_w - 8:
            x = x_root - w - 18
        if y < 8:
            y = y_root + 24
        if y + h > screen_h - 8:
            y = screen_h - h - 8
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
        head = max(fill_w - 3, 0)
        self.canvas.create_rectangle(head, y0, fill_w, y1, fill=T.ACCENT_HOV, outline="")

    def _caption(self, title: str, sub: str) -> None:
        top = self.H + self.BAR_H
        y = top + self.CAP // 2
        self.canvas.create_rectangle(0, top, self.W, top + self.CAP,
                                      fill=T.ELEVATED, outline="")
        if title:
            self.canvas.create_text(10, y, text=title, fill=T.DIM,
                                     font=(T.UI, 9), anchor="w")
        if sub:
            self.canvas.create_text(self.W - 10, y, text=sub, fill=T.ACCENT,
                                     font=(T.MONO, 9), anchor="e")

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
                                     fill=T.FAINT, font=(T.UI, 10))
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
                                 fill=T.FAINT, font=(T.UI, 10))
        self._progress_bar(None)
        self._caption(title, "")
        self._place(x_root, y_root)
        self.win.deiconify()
        self._visible = True

    def hide(self) -> None:
        if self.win is not None and self._visible:
            self.win.withdraw()
            self._visible = False


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
        self.stripe = tk.Frame(inner, bg=T.ACCENT, width=4)
        self.stripe.pack(side="left", fill="y")
        self.label = tk.Label(inner, bg=T.ELEVATED, fg=T.TEXT,
                               font=(T.UI, 10), justify="left",
                               wraplength=330, padx=12, pady=10)
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
        x = self.root.winfo_rootx() + self.root.winfo_width() - w - 24
        y = self.root.winfo_rooty() + self.root.winfo_height() - h - 24
        self.win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.win.deiconify()
        self._after = self.root.after(ms, self.hide)

    def hide(self) -> None:
        self._after = None
        if self.win is not None:
            self.win.withdraw()


class JobPanel(ctk.CTkFrame):
    """Live rows for whatever is encoding right now, one per worker."""

    def __init__(self, parent, **kw):
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=12,
                          border_width=1, border_color=T.LINE, **kw)
        self.grid_columnconfigure(0, weight=1)
        self._rows: dict = {}
        self.idle = ctk.CTkLabel(self, text="Idle", font=font(11),
                                  text_color=T.FAINT, anchor="w")
        self.idle.grid(row=0, column=0, sticky="ew", padx=14, pady=12)

    def _next_row(self) -> int:
        return len(self._rows) + 1

    def start(self, job_id: str, name: str) -> None:
        if job_id in self._rows:
            self.finish(job_id)
        self.idle.grid_remove()

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
            self.idle.grid()

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
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=12,
                          border_width=1, border_color=T.LINE, **kw)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._records: list = []
        self._errors_only = False

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 4))
        head.grid_columnconfigure(0, weight=1)
        self.header_label = ctk.CTkLabel(head, text="LOG", font=font(10, "bold"),
                                          text_color=T.FAINT)
        self.header_label.grid(row=0, column=0, sticky="w")

        self.filter_btn = ctk.CTkButton(
            head, text="Errors only", width=84, height=22, corner_radius=5,
            font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
            text_color=T.DIM, command=self.toggle_filter)
        self.filter_btn.grid(row=0, column=1, padx=(0, 6))

        ctk.CTkButton(head, text="Clear", width=52, height=22, corner_radius=5,
                      font=font(10), fg_color=T.BTN, hover_color=T.BTN_HOV,
                      text_color=T.DIM, command=self.clear).grid(row=0, column=2)

        self.text = tk.Text(
            self, wrap=tk.WORD, font=(T.MONO, 10), bg=T.INPUT, fg=T.DIM,
            relief=tk.FLAT, padx=12, pady=9, borderwidth=0,
            insertbackground=T.DIM, selectbackground=T.ACCENT_DEEP,
            selectforeground=T.TEXT, state="disabled", height=8,
        )
        self.text.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))
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
            fg_color=T.ACCENT_DEEP if self._errors_only else T.BTN,
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
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=12,
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
            c.create_rectangle(0, 2, width, 5, fill=T.LINE_SOFT, outline="")
            return
        x = 0
        for n, colour in self._parts:
            if not n:
                continue
            w = max(int(width * n / total), 2)
            c.create_rectangle(x, 0, x + w, 7, fill=colour, outline="")
            x += w + 1
