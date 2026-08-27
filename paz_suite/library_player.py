"""The embedded clip player for the Library tab.

Idle it shows the selected clip's thumbnail. Press Play and the shared
:class:`~paz_suite.player_engine.ClipPlayer` engine takes over — seek bar,
loop, speed and frame-accurate scrubbing, with real audio through a second
ffplay process. This module only builds the UI chrome (buttons, seek bar,
clock, volume) and wires it to the engine.
"""

from __future__ import annotations

import io
import os
import threading
import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageTk

from .theme import T, font
from .format import fmt_clock, fmt_len
from .config import THUMB_DIR
from .media import fit_frame, thumb_key, probe
from .player_engine import ClipPlayer, HAS_FFPLAY
from .mpv_player import MpvPlayer, available as mpv_available
from . import uithread


class InlinePlayer:
    # Starting geometry only. The player resizes with the window - on a 4K
    # screen a fixed small canvas is unreadably small next to 4K stills.
    VIEW_W, VIEW_H = 424, 238

    def __init__(self, parent, tab):
        self.tab = tab
        self.rec = None
        self._dragging = False
        self._peek_after = None
        self._peek_token = 0
        self._peek_busy = False
        self._peek_pending = None
        self._seek_job = None
        self._pending_pos = None
        self._last_seek_pos = None

        self.frame = ctk.CTkFrame(parent, fg_color="transparent")

        # Two surfaces stacked in one place. The canvas is what the
        # built-in engine draws on, and what shows a thumbnail or a line
        # of text when nothing is playing. `stage` is a plain frame whose
        # window id gets handed to mpv, which then draws into it directly.
        # Whichever one is in use is raised over the other.
        self.stack = tk.Frame(self.frame, width=self.VIEW_W, height=self.VIEW_H,
                              bg=T.INPUT, bd=0, highlightthickness=0)
        self.stack.pack()
        self.stack.pack_propagate(False)
        self.canvas = tk.Canvas(self.stack, width=self.VIEW_W, height=self.VIEW_H,
                                 bg=T.INPUT, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas.bind("<Button-1>", lambda e: self.toggle())
        self.stage = tk.Frame(self.stack, bg="black", bd=0, highlightthickness=0)
        self.stage.bind("<Button-1>", lambda e: self.toggle())
        # Placed once and left placed. mpv attaches to this window's id, so
        # it has to exist with real geometry before the engine is built,
        # and it must never be unmapped afterwards - taking the drawable
        # out from under mpv leaves it rendering into nothing. Visibility
        # is a matter of which surface is on top, not which one exists.
        self.stage.place(x=0, y=0, relwidth=1, relheight=1)
        self.frame.update_idletasks()
        # tk.Misc.lift, not self.canvas.lift: Canvas overrides lift() with
        # tag_raise, which raises canvas *items*, not the widget.
        tk.Misc.lift(self.canvas)

        self.engine = self._build_engine()
        self.engine.loop = tab.cfg.player_loop
        self.engine.volume = max(0, min(int(tab.cfg.player_volume), 100))
        self.engine.muted = bool(tab.cfg.player_muted) or not HAS_FFPLAY

        self.bar = tk.Canvas(self.frame, height=20, bg=T.SURFACE,
                              highlightthickness=0, bd=0,
                              cursor="hand2", width=self.VIEW_W)
        self.bar.pack(fill="x", pady=(4, 2))
        self.bar.bind("<Configure>", lambda e: self._draw_bar())
        self.bar.bind("<Button-1>", self._bar_press)
        self.bar.bind("<B1-Motion>", self._bar_drag)
        self.bar.bind("<ButtonRelease-1>", self._bar_release)
        self.bar.bind("<Motion>", self._bar_hover)
        self.bar.bind("<Leave>", self._bar_leave)

        controls = ctk.CTkFrame(self.frame, fg_color="transparent")
        controls.pack(fill="x")

        def cbtn(text, cmd, width=42, color=T.DIM):
            b = ctk.CTkButton(controls, text=text, width=width, height=26,
                               corner_radius=6, font=font(10), fg_color=T.BTN,
                               hover_color=T.BTN_HOV, text_color=color,
                               command=cmd)
            b.pack(side="left", padx=(0, 5))
            return b

        self.play_btn = cbtn("▶ Play", self.toggle, 72, T.ACCENT)
        cbtn("-5s", lambda: self.nudge(-5), 40)
        cbtn("+5s", lambda: self.nudge(5), 40)
        self.loop_btn = cbtn("Loop", self.toggle_loop, 48,
                              T.ACCENT if self.engine.loop else T.DIM)
        self.quality_btn = cbtn("4K", self.toggle_quality, 46)
        self.quality_btn.configure(state="disabled")
        self.speed_menu = ctk.CTkOptionMenu(
            controls, values=["0.5x", "1x", "1.5x", "2x"], width=64, height=26,
            font=font(10), corner_radius=6, fg_color=T.INPUT,
            button_color=T.LINE, button_hover_color=T.BTN_HOV,
            dropdown_fg_color=T.ELEVATED, dropdown_hover_color=T.ACCENT2_DEEP,
            dropdown_text_color=T.TEXT, dropdown_font=font(11), text_color=T.TEXT,
            command=lambda v: self._set_speed(float(v.rstrip("x"))))
        self.speed_menu.set("1x")
        self.speed_menu.pack(side="left", padx=(0, 5))
        self.clock = ctk.CTkLabel(controls, text="", font=font(9, mono=True),
                                   text_color=T.DIM)
        self.clock.pack(side="left", padx=(4, 0))

        self.volume_slider = ctk.CTkSlider(
            controls, from_=0, to=100, number_of_steps=100, width=64,
            height=14, button_color=T.ACCENT, button_hover_color=T.ACCENT_HOV,
            progress_color=T.ACCENT, fg_color=T.LINE,
            command=self._on_volume_drag)
        self.volume_slider.set(0 if self.engine.muted else self.engine.volume)
        self.volume_slider.pack(side="right", padx=(2, 8))
        self.mute_btn = ctk.CTkButton(
            controls, text="🔇" if self.engine.muted else "🔊",
            width=28, height=26, corner_radius=6, font=font(11),
            fg_color="transparent", hover_color=T.BTN_HOV,
            text_color=T.FAINT if self.engine.muted else T.TEXT,
            state="normal" if HAS_FFPLAY else "disabled",
            command=self.toggle_mute)
        self.mute_btn.pack(side="right", padx=(4, 0))
        if not HAS_FFPLAY:
            self.volume_slider.configure(state="disabled")

        self._volume_job = None
        self._progress_job = None
        self._show_idle_text("Select a clip")

    # ── which engine ────────────────────────────────────────────────────
    #
    # mpv when it is there and wanted, the built-in pipe player otherwise.
    # They present the same interface, so nothing below this asks which
    # one it is holding.

    def _build_engine(self):
        want = getattr(self.tab.cfg, "player_backend", "auto")
        if want != "builtin" and mpv_available():
            engine = MpvPlayer(
                self.stage, self.VIEW_W, self.VIEW_H,
                on_tick=self._on_tick, on_state=self._on_state,
                on_fail=self._on_fail, on_eof=None,
                post=lambda fn, *a: uithread.post(fn, *a))
            engine.vo = getattr(self.tab.cfg, "player_mpv_vo", "") or ""
            self.using_mpv = True
            return engine
        self.using_mpv = False
        return ClipPlayer(
            self.canvas, self.VIEW_W, self.VIEW_H,
            on_tick=self._on_tick, on_state=self._on_state,
            on_fail=self._on_fail)

    def _show_stage(self, on: bool) -> None:
        """Raise mpv's surface over the thumbnail canvas, or drop it back."""
        if not self.using_mpv:
            return
        self.stage.lift() if on else tk.Misc.lift(self.canvas)

    def fall_back_to_builtin(self, why: str) -> None:
        """Give up on mpv for the rest of the session and carry on with the
        engine that needs nothing installed."""
        if not self.using_mpv:
            return
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self._show_stage(False)
        self.engine = ClipPlayer(
            self.canvas, self.VIEW_W, self.VIEW_H,
            on_tick=self._on_tick, on_state=self._on_state,
            on_fail=self._on_fail)
        self.using_mpv = False
        self.engine.loop = self.tab.cfg.player_loop
        self.engine.volume = max(0, min(int(self.tab.cfg.player_volume), 100))
        self.engine.muted = bool(self.tab.cfg.player_muted) or not HAS_FFPLAY
        self.tab.set_status(why, T.WARN)
        if self.rec is not None:
            path, duration, fps = self._resolve_source(self.rec)
            self.engine.load(path, duration, fps)
            self._show_thumb(self.rec)

    # ── sizing ──────────────────────────────────────────────────────────

    def set_size(self, width: int, height: int = 0) -> None:
        width = max(int(width) // 2 * 2, 240)
        height = int(height) if height else int(width * 9 / 16) // 2 * 2
        height = max(height, 135)
        self.bar.configure(width=width)
        self.stack.configure(width=width, height=height)
        if self.rec is None or self.using_mpv:
            self.canvas.configure(width=width, height=height)
        self.engine.set_size(width, height)
        if self.rec is not None and not self.engine.playing:
            self._show_thumb(self.rec)
        self._draw_bar()

    # ── content switching ───────────────────────────────────────────────

    def show_rec(self, rec) -> None:
        """Selection changed: stop whatever is playing, show the new thumb."""
        self.engine.stop()
        self._peek_hide()
        self._last_seek_pos = None
        self.rec = rec
        if rec is None:
            self.engine.clear()
            self._show_idle_text("Select a clip")
            self.clock.configure(text="")
            self._draw_bar()
            self._update_quality_btn()
            return
        path, duration, fps = self._resolve_source(rec)
        self.engine.load(path, duration, fps)
        self.clock.configure(text=f"0:00.0 / {fmt_len(self.engine.duration)}")
        self._draw_bar()
        self._show_thumb(rec)
        self._update_quality_btn()
        # Warm the hover-scrub sheet now, while the user is still just
        # looking at the thumbnail - by the time they reach for the seek
        # bar it's usually already built, instead of the first several
        # seconds of scrubbing paying a slow per-hover fallback.
        self.tab.frames.prime_hover(path, duration)

    def _resolve_source(self, rec):
        """(path, duration, fps) to actually load for `rec` - the matching
        4K/60+ edit-pool copy when one exists and is preferred, else the
        indexed file itself. The pool copy isn't in the DB (it lives in a
        separate folder tree from the library scan), so its duration/fps
        come from a probe - cheap after the first look since probe() caches
        by path+mtime+size."""
        if rec.premium_path and self.tab.cfg.player_prefer_premium:
            info = probe(rec.premium_path)
            if info and info.duration:
                return rec.premium_path, info.duration, info.fps or 60.0
        return rec.path, rec.duration, rec.fps or 30.0

    def toggle_quality(self) -> None:
        if self.rec is None or not self.rec.premium_path:
            return
        self.tab.cfg.player_prefer_premium = not self.tab.cfg.player_prefer_premium
        self.tab.cfg.save()
        was_playing = self.engine.playing
        position = self.engine.position
        self._last_seek_pos = None
        path, duration, fps = self._resolve_source(self.rec)
        self.engine.load(path, duration, fps)
        self.engine.position = min(position, self.engine.duration) if self.engine.duration else 0.0
        self._draw_bar()
        self.clock.configure(
            text=f"{fmt_clock(self.engine.position)} / {fmt_len(self.engine.duration)}")
        if was_playing:
            self.engine.play()
        self._update_quality_btn()
        self.tab.frames.prime_hover(path, duration)

    def _update_quality_btn(self) -> None:
        if self.rec is None or not self.rec.premium_path:
            self.quality_btn.configure(state="disabled", text="4K", text_color=T.DIM)
            return
        prefer = self.tab.cfg.player_prefer_premium
        self.quality_btn.configure(
            state="normal", text="4K ✓" if prefer else "Original",
            text_color=T.ACCENT if prefer else T.DIM)

    def _show_idle_text(self, text: str):
        self._show_stage(False)
        self.canvas.delete("all")
        self.canvas.create_text(self.engine.view_w // 2, self.engine.view_h // 2,
                                text=text, fill=T.FAINT, font=(T.UI, 11))

    def _show_thumb(self, rec):
        if self.using_mpv:
            # mpv holds the first frame of the loaded clip, which is a
            # better still than our cached thumbnail and needs no work.
            self._show_stage(True)
            return
        try:
            with open(os.path.join(THUMB_DIR, thumb_key(rec.path)), "rb") as fh:
                image = Image.open(io.BytesIO(fh.read()))
            image = fit_frame(image, self.engine.view_w, self.engine.view_h,
                              self.tab.cfg.thumb_fit)
            photo = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            self.canvas.create_image(self.engine.view_w // 2, self.engine.view_h // 2,
                                     image=photo, anchor="center")
            self.canvas.image = photo   # keep a reference
            self.canvas.create_text(self.engine.view_w // 2, self.engine.view_h - 14,
                                    text="▶ play", fill=T.TEXT, font=(T.UI, 9))
        except Exception:
            self._show_idle_text("no thumbnail")

    # ── transport ───────────────────────────────────────────────────────

    @property
    def playing(self) -> bool:
        return self.engine.playing

    @property
    def position(self) -> float:
        return self.engine.position

    def play(self) -> None:
        if self.rec is None:
            return
        self.engine.play()

    def pause(self) -> None:
        self.engine.pause()

    def toggle(self) -> None:
        if self.rec is None:
            return
        self.engine.toggle()

    def toggle_loop(self) -> None:
        loop = self.engine.toggle_loop()
        self.tab.cfg.player_loop = loop
        self.loop_btn.configure(text_color=T.ACCENT if loop else T.DIM)

    def nudge(self, seconds: float) -> None:
        if self.rec:
            self.engine.nudge(seconds)
            self._draw_bar()

    def toggle_mute(self) -> None:
        muted = self.engine.toggle_mute()
        self.tab.cfg.player_muted = muted
        self.tab.cfg.save()
        self.mute_btn.configure(text="🔇" if muted else "🔊",
                                text_color=T.FAINT if muted else T.TEXT)
        self.volume_slider.set(0 if muted else self.engine.volume)

    def _on_volume_drag(self, value):
        volume = max(0, min(int(round(float(value))), 100))
        if self.engine.muted and volume > 0:
            self.mute_btn.configure(text="🔊", text_color=T.TEXT)
        if self._volume_job is not None:
            try:
                self.canvas.after_cancel(self._volume_job)
            except ValueError:
                pass
        self._volume_job = self.canvas.after(220, lambda: self._commit_volume(volume))

    def _commit_volume(self, volume: int):
        self._volume_job = None
        self.engine.set_volume(volume)
        self.tab.cfg.player_volume = self.engine.volume
        self.tab.cfg.player_muted = self.engine.muted
        self.tab.cfg.save()

    def _set_speed(self, value: float) -> None:
        setter = getattr(self.engine, "set_speed", None)
        if setter is not None:
            setter(value)
        else:
            self.engine.speed = value

    def _watch_progress(self) -> None:
        """While mpv says it is playing, check the clock is really moving.
        A video output that cannot draw leaves it running with nothing on
        screen; better to say so than to let you stare at a black panel."""
        if not self.using_mpv:
            return
        if self._progress_job is not None:
            try:
                self.canvas.after_cancel(self._progress_job)
            except ValueError:
                pass
            self._progress_job = None
        check = getattr(self.engine, "check_progress", None)
        if check is None:
            return
        problem = check()
        if problem:
            self.fall_back_to_builtin(problem)
            return
        if self.engine.playing:
            self._progress_job = self.canvas.after(1000, self._watch_progress)

    # ── engine callbacks ────────────────────────────────────────────────

    def _on_state(self, playing: bool) -> None:
        if playing:
            self._show_stage(True)
            self._watch_progress()
        self.play_btn.configure(text="⏸ Pause" if playing else "▶ Play")

    def _on_tick(self, position: float) -> None:
        self._draw_bar()
        self.clock.configure(text=f"{fmt_clock(position)} / {fmt_len(self.engine.duration)}")

    def _on_fail(self, message: str) -> None:
        self.tab.set_status(message, T.FAIL)

    # ── seek bar ────────────────────────────────────────────────────────

    def _draw_bar(self):
        c = self.bar
        c.delete("all")
        width = c.winfo_width()
        if width < 20:
            return
        y = 10
        c.create_line(2, y, width - 2, y, fill=T.LINE, width=4, capstyle="round")
        if self.engine.duration <= 0:
            return
        frac = max(0.0, min(self.engine.position / self.engine.duration, 1.0))
        px = 2 + frac * (width - 4)
        c.create_line(2, y, px, y, fill=T.ACCENT, width=4, capstyle="round")
        c.create_oval(px - 5, y - 5, px + 5, y + 5, fill=T.ACCENT_HOV, outline="")

    def _bar_press(self, event):
        if self.rec is None or self.engine.duration <= 0:
            return
        self._peek_hide()
        self._dragging = True
        # Reset per gesture, not per clip: the dedup check in _commit_seek
        # only exists to stop a plain click's own release from redundantly
        # re-seeking the exact spot the press already landed on. Without
        # clearing it here, that same guard would silently swallow a
        # second, entirely separate click at that same position later -
        # exactly the "ignores it until you click again" bug this whole
        # rewrite exists to fix.
        self._last_seek_pos = None
        # A plain click used to only move the displayed position and wait
        # for the release to actually seek - so a single click looked like
        # it did nothing until you clicked again (whatever the second
        # click's seek landed on was the first one you actually saw take
        # effect). Seeking immediately on press is both more correct (a
        # click IS a request to jump there) and removes that whole class
        # of "why did I need to click twice" confusion.
        self._scrub_to(event.x, commit=True)

    def _bar_drag(self, event):
        if not self._dragging:
            return
        self._scrub_to(event.x, commit=False)

    def _bar_release(self, _event):
        if not self._dragging:
            return
        self._dragging = False
        if self._seek_job is not None:
            try:
                self.bar.after_cancel(self._seek_job)
            except ValueError:
                pass
            self._seek_job = None
        # Land exactly where the mouse came up, even if the last throttled
        # seek during the drag hadn't fired yet.
        self._commit_seek(self._pending_pos)

    def _scrub_to(self, x: int, commit: bool) -> None:
        width = max(self.bar.winfo_width() - 4, 1)
        frac = max(0.0, min((x - 2) / width, 1.0))
        position = frac * self.engine.duration
        self._pending_pos = position
        # The bar and clock track the cursor on every event regardless of
        # whether this tick actually reseeks - that's what makes dragging
        # feel like it's tracking the mouse instead of catching up to it.
        self.engine.position = position
        self._draw_bar()
        self.clock.configure(
            text=f"{fmt_clock(position)} / {fmt_len(self.engine.duration)}")
        if commit:
            self._commit_seek(position)
            return
        # While dragging, the actual reseek (which respawns ffmpeg) is
        # throttled rather than fired on every pixel of motion - a fast
        # drag across the bar would otherwise spawn a process per pixel.
        if self._seek_job is None:
            self._seek_job = self.bar.after(70, self._flush_seek)

    def _flush_seek(self) -> None:
        self._seek_job = None
        self._commit_seek(self._pending_pos)

    def _commit_seek(self, position: float | None) -> None:
        if position is None:
            return
        if (self._last_seek_pos is not None
                and abs(position - self._last_seek_pos) < 0.01):
            return
        self._last_seek_pos = position
        self.engine.seek(position)

    # ── hover preview (same YouTube-style scrub bubble as the gallery) ────

    def _bar_hover(self, event):
        if self._dragging or self.rec is None or self.engine.duration <= 0:
            return
        if self._peek_after is not None:
            try:
                self.bar.after_cancel(self._peek_after)
            except ValueError:
                pass
        self._peek_after = self.bar.after(
            90, lambda: self._peek_fetch(event.x, event.x_root, event.y_root))

    def _bar_leave(self, _event=None):
        self._peek_hide()

    def _peek_fetch(self, x: int, x_root: int, y_root: int) -> None:
        self._peek_after = None
        rec = self.rec
        if rec is None or self.engine.duration <= 0:
            return
        width = max(self.bar.winfo_width() - 4, 1)
        frac = max(0.0, min((x - 2) / width, 1.0))
        moment = frac * self.engine.duration
        self._peek_token += 1
        token = self._peek_token
        request = (rec, moment, token, x_root, y_root)
        if self._peek_busy:
            # An extraction is already running - fast mouse movement used
            # to spawn a new ffmpeg call per debounce tick regardless, so
            # the preview fell further and further behind the cursor.
            # Only the latest hover position matters, so it just replaces
            # whatever was pending instead of queuing another call.
            self._peek_pending = request
            return
        self._peek_busy = True
        self._peek_run(request)

    def _peek_run(self, request) -> None:
        _rec, moment, token, x_root, y_root = request
        # Whichever file is actually loaded (original or the 4K/60 pool
        # copy) - so the hover preview matches what Play would show.
        path = self.engine.path
        duration = self.engine.duration
        frac = (moment / duration) if duration else 0.0

        def work():
            # hover_frame() crops a pre-built sprite sheet instead of
            # spawning ffmpeg per hover - see media.py for why.
            data = self.tab.frames.hover_frame(path, duration, frac)
            uithread.post(self._peek_done, data, moment, token, x_root, y_root)

        threading.Thread(target=work, daemon=True).start()

    def _peek_done(self, data, moment: float, token: int, x_root: int, y_root: int) -> None:
        self._peek_busy = False
        if token == self._peek_token:
            self._peek_show(data, moment, token, x_root, y_root)
        pending = self._peek_pending
        self._peek_pending = None
        if pending is not None:
            self._peek_busy = True
            self._peek_run(pending)

    def _peek_show(self, data, moment: float, token: int, x_root: int, y_root: int) -> None:
        if token != self._peek_token or self.rec is None or self._dragging:
            return
        fraction = (moment / self.engine.duration) if self.engine.duration else None
        self.tab.peek.show_frame(data, self.rec.name, fmt_clock(moment), x_root, y_root,
                                 fraction=fraction)

    def _peek_hide(self) -> None:
        self._peek_token += 1
        self._peek_pending = None
        if self._peek_after is not None:
            try:
                self.bar.after_cancel(self._peek_after)
            except ValueError:
                pass
            self._peek_after = None
        self.tab.peek.hide()
