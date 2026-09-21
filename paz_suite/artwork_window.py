"""Pick a picture for a slot, and say what will happen to it.

The point of this window is that nobody has a 1760 × 76 picture lying
around. It states the shape the slot needs, reports the resolution of
whatever was picked, says in words what is about to be cropped away, and
then lets that crop be moved and tightened while showing the result at the
size it will actually appear.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageTk

from . import artwork
from .theme import T, font, px, pt, window_size

# The source preview, which the crop rectangle is dragged around on.
VIEW_W, VIEW_H = 520, 300


class ArtworkWindow(ctk.CTkToplevel):
    def __init__(self, parent, app, slot: artwork.Slot,
                 saved: dict = None, on_save=None):
        """`saved` and `on_save` let a slot live somewhere other than the
        config - a project's cover is stored with the project. Left out,
        the slot reads and writes the config under its own key."""
        super().__init__(parent)
        self.app = app
        self.cfg = app.cfg
        self.slot = slot
        self._on_save = on_save
        saved = dict(saved) if saved else artwork.read_slot(self.cfg, slot.key)
        if slot.treatments and "blur" not in saved:
            saved.update(artwork.read_treatments(self.cfg, slot.key))
        self.blur = artwork.clamp(saved.get("blur", artwork.BLUR_DEFAULT),
                                  0.0, artwork.BLUR_MAX)
        self.dim = artwork.clamp(saved.get("dim", artwork.DIM_DEFAULT),
                                 0.0, artwork.DIM_MAX)
        self.path = saved.get("path", "")
        self.zoom = artwork.clamp(saved.get("zoom", 1.0),
                                  artwork.ZOOM_MIN, artwork.ZOOM_MAX)
        self.fx = artwork.clamp(saved.get("fx", 0.5), 0.0, 1.0)
        self.fy = artwork.clamp(saved.get("fy", 0.5), 0.0, 1.0)
        self._source = None          # the picture, full size
        self._view = None            # it, fitted into the preview area
        self._view_box = (0, 0, VIEW_W, VIEW_H)
        self._photo = None
        self._result_photo = None
        self._drag_from = None

        self.title(f"{slot.title} picture")
        # Tall enough for the whole stack: heading, the source with its
        # crop on it, what was picked, the zoom, and the result at the
        # slot's own shape. It was short by about thirty pixels, which cut
        # the bottom off the one preview that shows what you are actually
        # going to get.
        extra = 60 if slot.treatments else 0
        self.geometry(window_size(self, 620, 830 + extra))
        self.minsize(px(480), px(640 + extra))
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self.after(120, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build()
        self._load_source()
        self.bind("<Escape>", lambda e: self.destroy())

    # ── layout ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=px(16), pady=(px(14), px(6)))
        head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(head, text=self.slot.title, font=font(15, "bold"),
                     text_color=T.ACCENT2, anchor="w"
                     ).grid(row=0, column=0, sticky="w")
        # The size the slot needs, said before anything is picked.
        ctk.CTkLabel(head, text=f"Wants {self.slot.wanted()}",
                     font=font(11, mono=True), text_color=T.DIM, anchor="w"
                     ).grid(row=1, column=0, sticky="w", pady=(px(2), 0))
        if self.slot.note:
            ctk.CTkLabel(head, text=self.slot.note, font=font(10),
                         text_color=T.FAINT, anchor="w", justify="left",
                         wraplength=px(520)
                         ).grid(row=2, column=0, sticky="w", pady=(px(4), 0))

        body = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=12,
                            border_width=1, border_color=T.LINE)
        body.grid(row=1, column=0, sticky="nsew", padx=px(16), pady=px(10))
        body.grid_columnconfigure(0, weight=1)

        # The source, with the crop drawn on it.
        self.view = tk.Canvas(body, width=px(VIEW_W), height=px(VIEW_H),
                              bg=T.INPUT, highlightthickness=0, bd=0,
                              cursor="fleur")
        self.view.grid(row=0, column=0, padx=px(12), pady=(px(12), px(6)))
        self.view.bind("<Button-1>", self._press)
        self.view.bind("<B1-Motion>", self._drag)
        self.view.bind("<MouseWheel>", self._wheel)
        self.view.bind("<Button-4>", lambda e: self._nudge_zoom(0.1))
        self.view.bind("<Button-5>", lambda e: self._nudge_zoom(-0.1))

        # What they actually picked.
        self.detail = ctk.CTkLabel(body, text="No picture chosen",
                                   font=font(11, mono=True), text_color=T.DIM,
                                   anchor="w", justify="left",
                                   wraplength=px(520))
        self.detail.grid(row=1, column=0, sticky="ew", padx=px(12))

        zoom_row = ctk.CTkFrame(body, fg_color="transparent")
        zoom_row.grid(row=2, column=0, sticky="ew", padx=px(12), pady=px(8))
        ctk.CTkLabel(zoom_row, text="Zoom", font=font(10),
                     text_color=T.FAINT).pack(side="left", padx=(0, px(8)))
        self.zoom_slider = ctk.CTkSlider(
            zoom_row, from_=artwork.ZOOM_MIN, to=artwork.ZOOM_MAX,
            number_of_steps=60, height=px(14),
            button_color=T.ACCENT2, button_hover_color=T.ACCENT2_HOV,
            progress_color=T.ACCENT2, fg_color=T.LINE, command=self._set_zoom)
        self.zoom_slider.set(self.zoom)
        self.zoom_slider.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(zoom_row, text="Reset", width=px(56), height=px(24),
                      corner_radius=6, font=font(10), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.DIM,
                      command=self._reset_crop).pack(side="left", padx=(px(8), 0))

        # All three controls together, above the preview they change.
        # Blur and dim used to sit underneath it, which split the controls
        # around the thing they were controlling.
        if self.slot.treatments:
            self._soften_row(body, 3)

        ctk.CTkLabel(body, text="HOW IT WILL LOOK", font=font(9, "bold"),
                     text_color=T.FAINT, anchor="w"
                     ).grid(row=4, column=0, sticky="w", padx=px(12))
        self.result = tk.Canvas(body, bg=T.INPUT, highlightthickness=0, bd=0,
                                height=px(132))
        self.result.grid(row=5, column=0, sticky="ew",
                         padx=px(12), pady=(px(4), px(12)))
        # It is drawn to the canvas's real width, which is not known until
        # the window has been laid out.
        self.result.bind("<Configure>", lambda e: self._paint_result())

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=px(16), pady=(0, px(14)))
        ctk.CTkButton(foot, text="Choose picture…", height=px(30),
                      corner_radius=8, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.TEXT,
                      command=self._choose).pack(side="left")
        ctk.CTkButton(foot, text="Remove", height=px(30), width=px(80),
                      corner_radius=8, font=font(11), fg_color=T.BTN,
                      hover_color=T.BTN_HOV, text_color=T.FAINT,
                      command=self._remove).pack(side="left", padx=(px(8), 0))
        ctk.CTkButton(foot, text="Use it", height=px(30), width=px(96),
                      corner_radius=8, font=font(11, "bold"),
                      fg_color=T.ACCENT2_DEEP, hover_color=T.BTN_HOV,
                      text_color=T.ACCENT2,
                      command=self._save).pack(side="right")

    def _soften_row(self, body, row: int) -> None:
        """Blur and dim. A backdrop is the only slot with these, because
        it is the only one that has to disappear."""
        holder = ctk.CTkFrame(body, fg_color="transparent")
        holder.grid(row=row, column=0, sticky="ew", padx=px(12), pady=(0, px(6)))
        holder.grid_columnconfigure(1, weight=1)
        for index, (label, value, hi, setter) in enumerate((
                ("Blur", self.blur, artwork.BLUR_MAX, self._set_blur),
                ("Dim", self.dim, artwork.DIM_MAX, self._set_dim))):
            ctk.CTkLabel(holder, text=label, font=font(10), text_color=T.FAINT,
                         width=px(38), anchor="w"
                         ).grid(row=index, column=0, sticky="w", pady=px(2))
            slider = ctk.CTkSlider(
                holder, from_=0.0, to=hi, number_of_steps=60, height=px(14),
                button_color=T.ACCENT2, button_hover_color=T.ACCENT2_HOV,
                progress_color=T.ACCENT2, fg_color=T.LINE, command=setter)
            slider.set(value)
            slider.grid(row=index, column=1, sticky="ew", pady=px(2))

    def _set_blur(self, value) -> None:
        self.blur = artwork.clamp(value, 0.0, artwork.BLUR_MAX)
        self._paint_result()

    def _set_dim(self, value) -> None:
        self.dim = artwork.clamp(value, 0.0, artwork.DIM_MAX)
        self._paint_result()

    # ── the picture ─────────────────────────────────────────────────────

    def _choose(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=f"Picture for the {self.slot.title.lower()}",
            initialdir=self.cfg.banner_dir or os.path.expanduser("~"),
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                       ("All files", "*.*")])
        if not path:
            return
        if not artwork.is_image(path):
            self.detail.configure(text="That is not a picture this can read.",
                                  text_color=T.FAIL)
            return
        self.path = path
        self.cfg.banner_dir = os.path.dirname(path)
        self._reset_crop()
        self._load_source()

    def _remove(self) -> None:
        self.path = ""
        self._source = None
        self._reset_crop()
        self._load_source()

    def _reset_crop(self) -> None:
        self.zoom = 1.0
        self.fx = 0.5
        self.fy = 0.34 if self.slot.flexible_width else 0.5
        if hasattr(self, "zoom_slider"):
            self.zoom_slider.set(self.zoom)
        self._repaint()

    def _load_source(self) -> None:
        self._source = None
        if self.path and os.path.isfile(self.path):
            try:
                with Image.open(self.path) as raw:
                    self._source = raw.convert("RGB")
            except Exception:
                self._source = None
        if self._source is None:
            self.detail.configure(
                text="No picture chosen" if not self.path
                else "That is not a picture this can read.",
                text_color=T.DIM if not self.path else T.FAIL)
        else:
            self.detail.configure(
                text=f"{os.path.basename(self.path)}\n"
                     f"{artwork.describe(self.path, self.slot)}",
                text_color=T.DIM)
        self._repaint()

    # ── interaction ─────────────────────────────────────────────────────

    def _set_zoom(self, value) -> None:
        self.zoom = artwork.clamp(value, artwork.ZOOM_MIN, artwork.ZOOM_MAX)
        self._repaint()

    def _nudge_zoom(self, delta: float) -> None:
        self.zoom_slider.set(artwork.clamp(self.zoom + delta,
                                           artwork.ZOOM_MIN, artwork.ZOOM_MAX))
        self._set_zoom(self.zoom + delta)

    def _wheel(self, event) -> None:
        self._nudge_zoom(0.1 if event.delta > 0 else -0.1)

    def _press(self, event) -> None:
        self._drag_from = (event.x, event.y, self.fx, self.fy)

    def _drag(self, event) -> None:
        if self._drag_from is None or self._source is None:
            return
        x0, y0, fx0, fy0 = self._drag_from
        vx, vy, vw, vh = self._view_box
        if vw <= 1 or vh <= 1:
            return
        # Dragging moves the picture under the crop, so the focal point
        # travels the other way.
        self.fx = artwork.clamp(fx0 - (event.x - x0) / vw, 0.0, 1.0)
        self.fy = artwork.clamp(fy0 - (event.y - y0) / vh, 0.0, 1.0)
        self._repaint()

    # ── drawing ─────────────────────────────────────────────────────────

    def _repaint(self) -> None:
        self._paint_source()
        self._paint_result()

    def _paint_source(self) -> None:
        c = self.view
        c.delete("all")
        width, height = px(VIEW_W), px(VIEW_H)
        if self._source is None:
            c.create_text(width // 2, height // 2,
                          text="Choose a picture", fill=T.FAINT,
                          font=(T.UI, pt(11)))
            self._view_box = (0, 0, width, height)
            return
        # The whole picture, fitted into the preview with room around it.
        sw, sh = self._source.size
        scale = min(width / sw, height / sh)
        vw, vh = max(int(sw * scale), 1), max(int(sh * scale), 1)
        vx, vy = (width - vw) // 2, (height - vh) // 2
        self._view = ImageTk.PhotoImage(self._source.resize((vw, vh), Image.LANCZOS))
        c.create_image(vx, vy, image=self._view, anchor="nw")
        self._view_box = (vx, vy, vw, vh)

        # The crop, in the preview's coordinates. Everything outside it is
        # dimmed rather than hidden, so the part being thrown away is still
        # visible while it is being chosen.
        box = artwork.crop_box(self._source.size, self.slot.size,
                               self.zoom, self.fx, self.fy)
        rx0 = vx + box[0] * vw / sw
        ry0 = vy + box[1] * vh / sh
        rx1 = vx + box[2] * vw / sw
        ry1 = vy + box[3] * vh / sh
        shade = T.BG
        for area in ((vx, vy, vx + vw, ry0), (vx, ry1, vx + vw, vy + vh),
                     (vx, ry0, rx0, ry1), (rx1, ry0, vx + vw, ry1)):
            if area[2] > area[0] and area[3] > area[1]:
                c.create_rectangle(*area, fill=shade, outline="", stipple="gray50")
        c.create_rectangle(rx0, ry0, rx1, ry1, outline=T.ACCENT2, width=2)

    def _paint_result(self) -> None:
        c = self.result
        c.delete("all")
        width = max(c.winfo_width(), px(360))
        height = max(c.winfo_height(), px(120))
        if self._source is None:
            c.create_text(width // 2, height // 2, text="—", fill=T.FAINT,
                          font=(T.UI, pt(11)))
            return
        # Shown at the slot's shape, as large as fits - this is the picture
        # exactly as the app will draw it.
        aspect = self.slot.aspect
        pw = width - px(8)
        ph = int(pw / aspect)
        if ph > height - px(8):
            ph = height - px(8)
            pw = int(ph * aspect)
        out = artwork.render(self.path, (max(pw, 1), max(ph, 1)),
                             self.zoom, self.fx, self.fy)
        if out is None:
            return
        if self.slot.treatments:
            # Blur is a radius in pixels at full width, so it has to be
            # scaled down with the preview or the preview lies about how
            # soft the result will be.
            scale = max(pw, 1) / max(self.slot.size[0], 1)
            out = artwork.treat(out, self.blur * scale, self.dim, T.BG)
        self._result_photo = ImageTk.PhotoImage(out)
        c.create_image(width // 2, height // 2, image=self._result_photo)
        c.create_rectangle(width // 2 - pw // 2, height // 2 - ph // 2,
                           width // 2 + pw // 2, height // 2 + ph // 2,
                           outline=T.LINE)

    # ── done ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        crop = {"path": self.path, "zoom": self.zoom,
                "fx": self.fx, "fy": self.fy,
                "blur": self.blur, "dim": self.dim}
        if self._on_save is not None:
            self._on_save(crop)
        else:
            artwork.write_slot(self.cfg, self.slot.key, self.path,
                               self.zoom, self.fx, self.fy)
            if self.slot.treatments:
                artwork.write_treatments(self.cfg, self.slot.key,
                                         self.blur, self.dim)
            self.cfg.save()
            applied = getattr(self.app, "artwork_changed", None)
            if applied is not None:
                applied(self.slot.key)
        self.destroy()
