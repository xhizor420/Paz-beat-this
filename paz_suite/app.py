"""PAZ Suite application shell: one window, a Convert/Library/Vault/Beat
This tabview, and the shared services (config, e621 cache, thumbnail
cache, toasts, hover peek) the tabs draw on. Also owns the keyboard-
shortcut dispatch, since several shortcuts mean different things on each
tab and must only fire for whichever one is currently visible.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk

import customtkinter as ctk
from PIL import ImageTk

from .theme import (T, BANNER_H, banner_image, faster_corners, round_pts,
                    mix, px, pt, resolve_fonts)
from .config import AppConfig, CONFIG_DIR
from .e621 import E621Meta, APP_NAME, APP_VERSION
from .media import ThumbCache, set_probe_cache_limit
from .widgets import Toaster, PeekWindow, popup_menu, menu_rule
from .convert_tab import ConvertTab
from .library_tab import LibraryTab
from .vault_tab import VaultTab
from .beat_tab import BeatTab
from .settings_window import SettingsWindow
from .library_windows import HelpWindow
from .shortcuts import open_shortcuts
from .watchdog import Watchdog
from . import watchdog
from . import appicon, artwork, audio_out, heap, icons, libmpv_player, uithread, vlc_player, winsys

TAB_NAMES = ("Convert", "Library", "Vault", "Beat This")


class HeaderSlot:
    """Stands where a label in a tab's own title bar used to be. The
    title bars are gone - their contents live in the one header now -
    but the code that kept them current still calls configure()."""

    def __init__(self, repaint):
        self._repaint = repaint
        self.text = ""
        self.text_color = T.DIM
        self.fg_color = None

    def configure(self, text=None, text_color=None, fg_color=None, **_ignored):
        if text is not None:
            self.text = text
        if text_color is not None:
            self.text_color = text_color
        if fg_color is not None:
            self.fg_color = fg_color
        self._repaint()


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

        # Both before anything that could ask px() or pt() a question,
        # or draw a rounded corner. _apply_scaling used to sit below the
        # two lines after it, and anything either of them worked out in
        # its constructor was worked out at 100% whatever the display
        # was doing - which is how the hover bubble came to be 380
        # pixels wide on a 150% screen.
        faster_corners()
        self._apply_scaling()

        self.toaster = Toaster(root)
        self.peek = PeekWindow(root)
        self._icon = None

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
        # What was asked for, and when. The window is first shown at its
        # minimum size and reaches this a moment later; tabs that lay
        # themselves out to the window wait for it - see
        # LibraryTab._window_ready.
        self.requested_size = (width, height)
        self.started_at = time.monotonic()
        # Every panel below (gallery columns, the inspector/player, the
        # queue table) already recalculates its own layout on resize, so
        # this is a floor for legibility, not a hard requirement - the
        # window is just as usable maximized on a 4K display as tiled on a
        # 13" laptop screen.
        root.minsize(min(1180, room_w), min(700, room_h))
        root.configure(fg_color=T.BG)

        self._build_header()

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

        # One tab now; the rest once the window is up. See _tab_object.
        self._tabs: dict = {}
        if self.cfg.last_tab in TAB_NAMES:
            self._show_tab(self.cfg.last_tab)
        self._tab_object(self.tabview.get())
        # set() does not run the change callback, so the tab we open on
        # would never get its first-look call. Give it one, after the
        # window is up rather than in the middle of building it.
        self.root.after(200, self._first_look)
        # Hidden tabs are built in pauses in input - see _build_rest. Only
        # a timestamp is kept per event, so watching every widget is free.
        self._last_input = time.monotonic()
        for sequence in ("<Motion>", "<KeyPress>", "<ButtonPress>", "<MouseWheel>"):
            root.bind_all(sequence, self._note_input, add="+")
        self.root.after(700, self._build_rest)

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
        """Make our scale factor and CustomTkinter's the SAME number.

        CTk's effective widget scaling is not what you pass to
        set_widget_scaling - it is that value multiplied by the monitor's
        DPI factor (ScalingTracker.get_widget_scaling). On Linux that DPI
        factor is hard-coded to 1, so passing 1.5 gives 1.5 and everything
        agrees. On Windows at 150% it is 1.5, so passing 1.5 gives 2.25:
        every CTk widget half again as big as asked for, every measured
        size handed back to CTk half again too wide, and a layout that
        runs off the right-hand edge of the window. Which is why this app
        looked right on the machine it was measured on and wrong on the
        machine it runs on.

        So: ask CTk what the monitor is already doing, and pass it only
        the difference. T.SCALE (which px() and pt() use for the raw-Tk
        canvases) then equals CTk's effective scaling on every platform,
        and unscaled() is exact rather than approximately right.
        """
        choice = self.cfg.ui_scale if self.cfg.ui_scale in self.SCALE_CHOICES else "Auto"
        scale = self._detect_scale() if choice == "Auto" else int(choice.rstrip("%")) / 100
        scale = max(1.0, min(scale, 2.5))
        T.SCALE = scale
        try:
            dpi = self._ctk_dpi_scaling()
            # Only the shortfall: CTk multiplies by `dpi` itself.
            ctk.set_widget_scaling(max(scale / dpi, 0.4) if dpi else scale)
            # Deliberately NOT set_window_scaling: that multiplies every
            # geometry string, so asking for a 1760px window at 175% asks
            # for 3080px and the window manager quietly declines to show it
            # at all on anything smaller. Window sizes here are already in
            # real pixels; it is the contents that need to grow.
            ctk.set_window_scaling(1.0 / dpi if dpi else 1.0)
        except Exception:
            pass

    def _ctk_dpi_scaling(self) -> float:
        """What CustomTkinter is already scaling by, before we ask for
        anything. 1.0 where it does not scale by itself."""
        try:
            dpi = float(ctk.ScalingTracker.get_window_dpi_scaling(self.root))
        except Exception:
            return 1.0
        return dpi if 0.5 <= dpi <= 4.0 else 1.0

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

    def _band(self) -> int:
        """The header strip's height in real pixels.

        BANNER_H is a hand-drawn number and this is a raw tk.Canvas, so
        nothing scaled it: on a 4K screen at 150% the suite's own name
        sat in a 58-pixel band in 19pt type while every tab below it had
        grown by half. Read rather than stored, because T.SCALE is
        settled before any of this is built and the picture is rendered
        to whatever this returns.
        """
        return px(BANNER_H)

    def _build_header(self) -> None:
        self.header = tk.Canvas(self.root, height=self._band(), bg=T.BG,
                                highlightthickness=0, bd=0)
        self.header.pack(fill="x", side="top")
        self._banner_photo = None
        self._banner_job = None
        self._banner_width = 0
        self._banner_key = None
        self._banner_cache: dict = {}
        self._header_text = ""
        self._tab_status: dict = {}
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
        """Repaint the strip. The picture is made off this thread.

        Compositing a full-width header out of a photograph costs about a
        tenth of a second, and this is the one piece of chrome that every
        status line in the app writes into - so doing it here would put
        that tenth of a second into every encode tick and every tag fetch
        reply. The picture is rendered on a worker and cached against
        everything that could change it; the strip keeps showing the
        previous one until the new one lands, which at a header's aspect
        ratio is not a visible difference.
        """
        width = max(int(width), 320)
        self._banner_job = None
        self._banner_width = width
        # The tab is part of the key: with no picture set, the strip glows
        # in the colour of the tab that is open - see theme._default_banner.
        key = (artwork.slot_key(self.cfg, "banner"), width, self._active_tab_name())
        if key != self._banner_key:
            self._banner_key = key
            cached = self._banner_cache.get(key)
            if cached is not None:
                # Made once per tab and width, so switching tabs swaps the
                # glow at once rather than after a render.
                self._banner_photo = cached
            else:
                threading.Thread(target=self._build_banner, args=(key,),
                                 daemon=True).start()
        self._paint_header()

    def _active_tab_name(self) -> str:
        tabview = getattr(self, "tabview", None)
        if tabview is not None:
            try:
                return tabview.get()
            except Exception:
                pass
        return self.cfg.last_tab if self.cfg.last_tab in TAB_NAMES else TAB_NAMES[0]

    def _build_banner(self, key: tuple) -> None:
        """Decode, crop, scale and scrim the header picture. Worker."""
        slot, width, tab = key
        deep, bright = self._TAB_ACCENTS.get(tab, (T.ACCENT_DEEP, T.ACCENT))
        try:
            picture = banner_image(slot[0], width, self._band(),
                                   slot[2], slot[3], slot[4],
                                   accent=bright, accent_deep=deep)
        except Exception:
            picture = None
        uithread.post(self._banner_ready, key, picture)

    def _banner_ready(self, key: tuple, picture) -> None:
        try:
            photo = ImageTk.PhotoImage(picture) if picture is not None else None
        except Exception:
            return
        if photo is not None:
            # A tab's strip at a width; a window resize starts a fresh set.
            if len(self._banner_cache) > 8:
                self._banner_cache.clear()
            self._banner_cache[key] = photo
        if key != self._banner_key:
            return                      # another tab or width got there first
        self._banner_photo = photo
        self._paint_header()

    def _paint_header(self) -> None:
        """The canvas items, over the cached picture.

        Everything is a canvas item over one composited background image
        rather than a row of packed widgets, because Tk has no widget
        transparency - a CTkLabel over a picture would sit on its own
        opaque rectangle and the banner would look like a mistake.
        """
        try:
            c = self.header
            c.delete("all")
            if self._banner_photo is not None:
                c.create_image(0, 0, image=self._banner_photo, anchor="nw")
            mid = self._band() // 2
            active = self._active_tab_name()
            deep, bright = self._TAB_ACCENTS.get(active, (T.ACCENT_DEEP, T.ACCENT))
            c.create_image(px(18), mid, image=self._mark_for(bright), anchor="w")
            name = c.create_text(px(48), mid + px(1), text="PAZ", anchor="w",
                                 fill=bright, font=(T.DISPLAY, pt(17), "bold"))
            # Measured, not guessed: the display family is whatever
            # resolve_fonts() found installed, so "PAZ" is a different
            # width on every machine and a fixed offset collides with it.
            x = c.bbox(name)[2] + px(28)
            for tab in TAB_NAMES:
                x = self._paint_tab(tab, x, mid, tab == active) + px(4)
            self._tabs_end = x
            self._paint_header_buttons(mid)
            self._paint_header_status()
        except tk.TclError:
            pass

    TAB_H = 30

    def _paint_tab(self, name: str, x: int, mid: int, selected: bool) -> int:
        """One tab as canvas items, so the header picture shows round it.
        Returns where the next one starts."""
        c = self.header
        deep, bright = self._TAB_ACCENTS[name]
        tag = f"tab:{name}"
        label = c.create_text(x + px(30), mid, text=name, anchor="w",
                              fill=bright if selected else T.DIM,
                              font=(T.UI, pt(12), "bold"), tags=(tag, f"{tag}:text"))
        right = c.bbox(label)[2] + px(14)
        half = px(self.TAB_H) // 2
        pill = self._round_rect(x, mid - half, right, mid + half, px(7),
                                fill=deep if selected else "", outline="",
                                tags=(tag, f"{tag}:pill"))
        c.tag_lower(pill, label)
        dot = mix(bright, T.BG, 0 if selected else 0.55)
        c.create_rectangle(x + px(13), mid - px(4), x + px(21), mid + px(4),
                           fill=dot, outline="", tags=(tag,))
        if not selected:
            c.tag_bind(tag, "<Enter>",
                       lambda e, t=tag: c.itemconfigure(f"{t}:text", fill=T.TEXT))
            c.tag_bind(tag, "<Leave>",
                       lambda e, t=tag: c.itemconfigure(f"{t}:text", fill=T.DIM))
        # A transparent hit area over the whole pill, so the gaps between
        # the dot and the word still answer a click.
        c.create_rectangle(x, mid - half, right, mid + half, fill="", outline="",
                           tags=(tag,))
        c.tag_bind(tag, "<Button-1>", lambda e, n=name: self._select_tab(n))
        return right

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        return self.header.create_polygon(round_pts(x0, y0, x1, y1, r), **kw)

    def _mark_for(self, colour: str):
        marks = self.__dict__.setdefault("_marks", {})
        if colour not in marks:
            from PIL import ImageTk
            marks[colour] = ImageTk.PhotoImage(appicon.header_mark(px(24), colour))
        return marks[colour]

    def _paint_header_buttons(self, mid: int) -> None:
        """Shortcuts/help and Settings, at the right-hand end."""
        c = self.header
        width = max(self._banner_width or self.root.winfo_width() or 1760, 320)
        x = width - px(16)
        self._status_right = x
        for key, icon_name, action in (("help", "help", self._shortcuts),
                                       ("settings", "settings", self._open_tab_settings)):
            tag = f"hbtn:{key}"
            size = px(32)
            left = x - size
            bg = self._round_rect(left, mid - size // 2, x, mid + size // 2, px(7),
                                  fill="", outline="", tags=(tag, f"{tag}:bg"))
            c.create_image((left + x) // 2, mid, tags=(tag,),
                           image=icons.photo(icon_name, px(17), T.DIM))
            c.tag_bind(tag, "<Enter>", lambda e, b=bg: c.itemconfigure(b, fill=T.BTN_HOV))
            c.tag_bind(tag, "<Leave>", lambda e, b=bg: c.itemconfigure(b, fill=""))
            c.tag_bind(tag, "<Button-1>", lambda e, a=action: a())
            x = left - px(6)
        self._status_right = x - px(10)

    def _open_tab_settings(self) -> None:
        tab = self._tab_object(self._active_tab_name(), build=False)
        opener = getattr(tab, "_open_settings", None)
        (opener or self.open_settings)()

    def _paint_header_status(self) -> None:
        """The live lines and their dots, right-aligned before the buttons:
        the open tab's own (Convert's queue readout and run state), then
        the suite's. Tagged so a status change can replace them without
        touching the picture underneath."""
        try:
            c = self.header
            c.delete("hstatus")
            mid = self._band() // 2
            x = getattr(self, "_status_right", None)
            if x is None:
                return
            floor = getattr(self, "_tabs_end", 0) + px(16)
            slots = self._tab_status.get(self._active_tab_name(), {})
            parts = []
            if self._header_text:
                parts.append((self._header_text, T.DIM, self._header_colour))
            pill = slots.get("pill")
            if pill and pill.text:
                parts.append((pill.text, pill.text_color, pill.fg_color))
            readout = slots.get("readout")
            if readout and readout.text:
                parts.append((readout.text, readout.text_color, None))
            for text, colour, dot in parts:
                item = c.create_text(x, mid, text=text, anchor="e", fill=colour,
                                     font=(T.MONO, pt(10)), tags=("hstatus",))
                left = c.bbox(item)[0]
                if left < floor:
                    c.delete(item)          # no room beside the tabs
                    break
                if dot:
                    c.create_oval(left - px(15), mid - px(4), left - px(8), mid + px(3),
                                  fill=dot, outline="", tags=("hstatus",))
                    left -= px(15)
                x = left - px(22)
        except tk.TclError:
            pass

    def status_slot(self, tab: str, key: str) -> "HeaderSlot":
        """A stand-in for a label in a tab's old title bar. Configuring it
        (text, text_color, fg_color - from any code that used to configure
        the label) redraws that part of the shared header."""
        slot = HeaderSlot(self._paint_header_status)
        self._tab_status.setdefault(tab, {})[key] = slot
        return slot

    def set_header_status(self, text: str, colour: str = T.OK) -> None:
        """One live line in the identity bar - what the suite is doing
        right now, readable from whichever tab you happen to be on. An
        empty text hides the indicator entirely; a lone dot with nothing
        beside it just looks like a rendering fault.

        Called from encode ticks and tag-fetch replies, so it redraws two
        canvas items and nothing else - never the picture.
        """
        if text == self._header_text and colour == self._header_colour:
            return
        self._header_text = text
        self._header_colour = colour
        self._paint_header_status()

    # ── banner picture ──────────────────────────────────────────────────
    #
    # The one place in the suite that shows a picture of your choosing.
    # It lives here, in the chrome, rather than anywhere in the gallery:
    # a clip's tile has a job (show that clip), and a picture standing in
    # for it makes the library harder to read, not nicer to look at. A
    # header strip has no such job, so it is free to be yours.

    def _banner_menu(self, event) -> None:
        menu = popup_menu(self.root)
        for slot in artwork.SLOTS:
            has = bool(artwork.read_slot(self.cfg, slot.key)["path"])
            menu.add_command(
                label=f"{slot.title}…{'  ✓' if has else ''}",
                command=lambda s=slot: self.open_artwork(s))
        menu_rule(menu)
        menu.add_command(label="Settings…", command=self.open_settings)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_artwork(self, slot) -> None:
        """The picker for one artwork slot. It says what size the slot
        wants, what was actually handed over, and lets the crop be placed
        rather than guessed at."""
        from .artwork_window import ArtworkWindow
        if isinstance(slot, str):
            slot = artwork.SLOTS_BY_KEY[slot]
        ArtworkWindow(self.root, self, slot)

    def artwork_changed(self, key: str) -> None:
        """A slot was just set or cleared. Redraw whatever uses it."""
        if key == "banner":
            self._draw_header(self._banner_width or self.root.winfo_width() or 1760)
        elif key == "wallpaper":
            library = self._tab_object("Library", build=False)
            if library is not None:
                library.refresh_wallpaper()
        elif key == "icon":
            self._icon = None
            self._apply_chrome()
        name = artwork.SLOTS_BY_KEY[key].title
        set_ = artwork.read_slot(self.cfg, key)["path"]
        self.toaster.show(f"{name} picture {'set' if set_ else 'cleared'}")

    # ── chrome (window title / taskbar icon) ────────────────────────────────

    def _apply_chrome(self) -> None:
        self.root.title(f"{APP_NAME}  {APP_VERSION}")
        if self._icon is None:
            # The user's own square from Settings > Artwork if they set
            # one, the drawn PAZ mark otherwise - see appicon.
            try:
                picture = artwork.render_slot(self.cfg, "icon", (256, 256))
            except Exception:
                picture = None
            try:
                self._icon = appicon.install(self.root, picture)
            except Exception:
                self._icon = ""

    # Each tab has its own identity colour (pink for Convert, violet for
    # Library, teal for Vault, amber for Beat This) used throughout its own
    # widgets; the strip carries that colour as a dot per tab so every tab
    # is identifiable at rest, and lights the active one so the switcher
    # reads as "you are here" instead of one flat, generic control.
    _TAB_ACCENTS = {"Convert":   (T.ACCENT_DEEP, T.ACCENT),
                    "Library":   (T.ACCENT2_DEEP, T.ACCENT2),
                    "Vault":     (T.ACCENT3_DEEP, T.ACCENT3),
                    "Beat This": (T.ACCENT4_DEEP, T.ACCENT4)}

    def _show_tab(self, name: str) -> None:
        """Make `name` the tabview's current tab and grid it.

        What CTkTabview.set() does, minus the timer it leaves behind:
        set(name) also schedules, 100ms later, "hide every tab except
        `name`". Switch twice inside those 100ms - a quick second click, a
        shortcut straight after a click - and the first switch's timer
        fires last and hides the tab you actually ended up on: a black
        panel under the right tab button. The Windows test run hit it.
        The other tabs are hidden here, at once, by _select_tab instead.

        Falls back to set() on a CustomTkinter without these internals.
        """
        tv = self.tabview
        try:
            if name not in tv._tab_dict:
                raise ValueError(name)
            tv._current_name = name
            tv._segmented_button.set(name)
            tv._set_grid_current_tab()
        except AttributeError:
            tv.set(name)

    def _select_tab(self, name: str) -> None:
        self._show_tab(name)
        # The tabs stay laid out in the one cell, stacked, and switching
        # raises the one asked for. Taking the others away instead -
        # grid_forget, as CustomTkinter does - meant every switch
        # unmapped one whole tab and mapped another, and every CTk widget
        # in it redrew on the <Configure> that brings: 120-260ms a switch,
        # measured, against 35-60ms for a raise. Nothing behind is drawn
        # to the screen (an embedded mpv included - checked with a clip
        # playing), and the tabs learn they are hidden from on_hidden in
        # _on_tab_changed rather than from <Unmap>.
        #
        # The raise also settles which tab is seen whatever the stacking
        # order was left as - by the early layout, by CustomTkinter, by
        # anything: the one asked for is on top.
        try:
            frame = self.tabview.tab(name)
            tk.Misc.lift(frame)
        except (ValueError, tk.TclError):
            frame = None
        # Keystrokes follow focus, and a tab underneath is still mapped:
        # a filter box focused on the tab just left would go on taking
        # them. Hand focus to the window unless it is already on this tab.
        if frame is not None:
            try:
                focus = frame.focus_get()
            except (KeyError, tk.TclError):
                focus = None
            if focus is not None and not str(focus).startswith(str(frame)):
                behind = [str(self.tabview.tab(other)) for other in TAB_NAMES
                          if other != name]
                if any(str(focus).startswith(path + ".") for path in behind):
                    frame.winfo_toplevel().focus_set()
        self._on_tab_changed()

    def _style_tabs(self) -> None:
        # The tabs are drawn in the header now.
        self._paint_header()

    def _first_look(self) -> None:
        tab = self._tab_object(self.tabview.get())
        shown = getattr(tab, "on_shown", None)
        if shown is not None:
            shown()

    # ── the tabs, built when they are needed ─────────────────────────────
    #
    # All four used to be built before the window appeared: a second of
    # constructing widgets plus another half second of Tk laying out four
    # full tabs at once, with nothing on screen. Worse, the constructors
    # read the disk - the library index, the premium pool, the convert
    # queue - so on a machine whose disk is busy (which for this app's
    # user means "while Topaz is upscaling", i.e. most of the time) that
    # second became fifteen. Measured, twice.
    #
    # So only the tab being opened is built up front, and the other three
    # follow a moment later, one per callback, while the window is
    # already usable. Switching to one before it is ready builds it on
    # demand, which is what the properties below are for: every
    # `app.library` in the codebase still works, and still means "the
    # Library tab", whether or not it exists yet.

    TAB_CLASSES = {"Convert": ConvertTab, "Library": LibraryTab,
                   "Vault": VaultTab, "Beat This": BeatTab}

    def _tab_object(self, name: str, build: bool = True):
        """The tab object for `name`, building it if it is not there yet.

        `build=False` asks only for one that already exists - for the
        callers that run on every tab change, every resize and every
        settings save, and must not be the reason a tab gets built.
        """
        tab = self._tabs.get(name)
        if tab is not None or not build:
            return tab
        cls = self.TAB_CLASSES.get(name)
        if cls is None:
            return None
        tab = cls(self.tabview.tab(name), self)
        self._tabs[name] = tab
        return tab

    # How long mouse and keyboard must have been still before a hidden tab
    # is built. Each build holds the window for a few hundred milliseconds;
    # done while you are using it, that was the hitching of the first few
    # seconds after launch. Done in a pause, nobody is there to feel it,
    # and the tabs are still usually ready before they are first opened.
    IDLE_BEFORE_BUILD = 1.0

    def _note_input(self, _event=None) -> None:
        self._last_input = time.monotonic()

    def _idle(self) -> bool:
        return time.monotonic() - getattr(self, "_last_input", 0.0) >= self.IDLE_BEFORE_BUILD

    def _build_rest(self) -> None:
        """Build one not-yet-built tab, then come back for the next.

        One per callback rather than all three in a row: each is a few
        hundred milliseconds, and three of them together is the startup
        stall this exists to remove - just moved later. And only in a
        pause in input - see IDLE_BEFORE_BUILD. A tab opened before its
        turn is built on the spot by _tab_object, as it always was.
        """
        if not self._idle():
            self.root.after(250, self._build_rest)
            return
        for name in TAB_NAMES:
            if name not in self._tabs:
                # Announced, so the freeze log does not record a tab
                # being built as a mystery stutter - see watchdog.building.
                watchdog.building(True)
                try:
                    self._tab_object(name)
                finally:
                    watchdog.building(False)
                # Its first layout next, on its own callback - see
                # _lay_out_early.
                self.root.after(120, lambda n=name: self._lay_out_early(n))
                return

    def _lay_out_early(self, name: str) -> None:
        """Do a hidden tab's first layout now, underneath the tab on
        screen, and then carry on building the rest.

        The first switch to a tab cost 100-150ms and a second switch
        under 15. The difference is every CustomTkinter widget in it
        redrawing because it has just been handed its real size for the
        first time - after that the sizes do not change, and CTk skips
        the redraw. So it is done here instead, while the user is looking
        at something else: the tab is gridded into the same cell as the
        one showing, lowered beneath it so it cannot be seen, laid out,
        and left there for _select_tab to raise. Measured on the first switch afterwards:
        Convert 131-148ms to 16, Vault 103-108 to 8, Beat This 123-136
        to 10-13.

        The grid options are copied off the tab that is showing rather
        than written out here, so this lays the tab out exactly where
        CustomTkinter would put it, whatever version decides that.
        """
        tv = self.tabview
        showing = tv.get()
        if name != showing and not self._idle():
            self.root.after(250, lambda: self._lay_out_early(name))
            return
        if name != showing:
            watchdog.building(True)
            try:
                frame = tv.tab(name)
                info = tv.tab(showing).grid_info()
                keep = ("row", "column", "sticky", "padx", "pady",
                        "ipadx", "ipady", "rowspan", "columnspan")
                frame.grid(**{k: info[k] for k in keep if k in info})
                # Beneath the tab that is showing - and only that. A bare
                # lower() put it beneath every child of the tabview,
                # including the canvas the tabview paints its own
                # background on, and it stayed there: switching to it
                # later showed that background instead of the tab. Every
                # tab but the first came up as an empty black panel.
                tk.Misc.lower(frame, tv.tab(showing))
                frame.update_idletasks()
                # And left there, under the one showing: switching to it
                # later is a raise (see _select_tab).
            except (ValueError, KeyError, tk.TclError):
                pass
            finally:
                watchdog.building(False)
        self.root.after(120, self._build_rest)

    @property
    def convert(self):
        return self._tab_object("Convert")

    @property
    def library(self):
        return self._tab_object("Library")

    @property
    def vault(self):
        return self._tab_object("Vault")

    @property
    def beat(self):
        return self._tab_object("Beat This")

    def _on_tab_changed(self) -> None:
        name = self.tabview.get()
        self.cfg.last_tab = name
        self.cfg.save_soon()
        self._style_tabs()
        # The header glows in the open tab's colour (when no picture is set).
        self._draw_header(self._banner_width or self.root.winfo_width() or 1760)
        # Tabs get to do their first-look work when they are first looked
        # at, rather than all of it during startup.
        shown = getattr(self._tab_object(name), "on_shown", None)
        if shown is not None:
            shown()
        # And the tabs now behind it get told so. Two tabs hold a player;
        # a clip still playing on the page you just left is sound with no
        # picture, coming from somewhere the user cannot see to stop it.
        for other in TAB_NAMES:
            if other == name:
                continue
            # build=False: a tab that does not exist has no player running
            # and nothing to put away, and building all three to tell them
            # so would undo the whole point of building them lazily.
            hidden = getattr(self._tab_object(other, build=False),
                             "on_hidden", None)
            if hidden is not None:
                hidden()

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        # Fires while the window is still being sized at startup, so it
        # must not be what builds the Library tab.
        library = self._tab_object("Library", build=False)
        if library is not None:
            library.on_root_resize()

    # ── settings ─────────────────────────────────────────────────────────

    def open_settings(self, initial_tab: str = "Encoding") -> None:
        SettingsWindow(self.root, self, initial_tab=initial_tab)

    def on_settings_saved(self) -> None:
        self._apply_chrome()
        set_probe_cache_limit(self.cfg.probe_cache_limit)
        self.cache.limit = self.cfg.frame_cache_limit
        # Only the tabs that exist: one that has not been built yet will
        # read the new settings when it is.
        for name in TAB_NAMES:
            tab = self._tab_object(name, build=False)
            settled = getattr(tab, "after_settings_saved", None)
            if settled is not None:
                settled()

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
        root.bind("<Control-a>", lambda e: (
            self.library.mark_all_on_page(e) if self._active() == "Library"
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
        root.bind("<F1>", lambda e: self._shortcuts())

        # Convert-only
        root.bind("<Control-Return>", lambda e: self._only("Convert", self.convert.key_start))
        for key in ("h", "H"):
            root.bind(key, lambda e: self._only_evt("Convert", self.convert.key_peek_toggle, e))

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
        # Transport, Library-only. The set an editor expects: comma and
        # full stop step one frame (Resolve, Premiere and every web player
        # agree on those two), Shift with an arrow is a fine one-second
        # nudge, Home and End are the ends of the clip, and a digit jumps
        # that tenth of the way in.
        root.bind(",", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_frame_step(ev, -1), e))
        root.bind(".", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_frame_step(ev, 1), e))
        root.bind("<Shift-Left>", self._shift_left)
        root.bind("<Shift-Right>", self._shift_right)
        root.bind("<Home>", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_edge(ev, False), e))
        root.bind("<End>", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_edge(ev, True), e))
        for key in ("m", "M"):
            root.bind(key, lambda e: self._only_evt("Library", self.library.key_mute, e))
        # Up and Down walk the results; V puts the one you are looking at
        # into the project you are working in (Shift+V to pick another).
        root.bind("<Up>", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_step_clip(ev, -1), e))
        root.bind("<Down>", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_step_clip(ev, 1), e))
        root.bind("v", lambda e: self._only_evt("Library", self.library.key_mark_used, e))
        root.bind("V", lambda e: self._only_evt(
            "Library", lambda ev: self.library.key_mark_used(ev, pick=True), e))
        for digit in range(10):
            root.bind(str(digit), lambda e, d=digit: self._only_evt(
                "Library", lambda ev: self.library.key_jump(ev, d / 10.0), e))
        root.bind("<Control-c>", lambda e: self._only_evt("Library", self.library.key_copy_name, e))
        root.bind("<Control-Shift-C>",
                  lambda e: self._only_evt("Library", self.library.key_copy_path, e))

    def _shortcuts(self) -> None:
        name = self._active()
        tab = self._tab_object(name, build=False)
        on_help = None
        if name == "Library":
            def on_help():
                HelpWindow(self.root)
        elif tab is not None:
            on_help = getattr(tab, "_open_help", None)
        open_shortcuts(self.root, name, on_help)

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

    # Shift means "further" in Convert and "finer" in Library, because the
    # unshifted step already means different things in the two: a frame of
    # scrub there, five seconds of playback here.
    def _shift_left(self, event):
        if self._active() == "Convert":
            self.convert.key_scrub(event, -10)
        elif self._active() == "Library":
            return self.library.key_seek(event, -1)
        return None

    def _shift_right(self, event):
        if self._active() == "Convert":
            self.convert.key_scrub(event, 10)
        elif self._active() == "Library":
            return self.library.key_seek(event, 1)
        return None

    # ── shutdown ─────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        # Only the tabs that exist. Convert keeps its veto (it asks before
        # abandoning an encode), so it is asked first and by name; a tab
        # that was never opened has nothing to save and nothing to ask.
        convert = self._tab_object("Convert", build=False)
        if convert is not None and not convert.on_app_close():
            return
        for name in ("Library", "Vault", "Beat This"):
            tab = self._tab_object(name, build=False)
            closing = getattr(tab, "on_app_close", None)
            if closing is not None:
                closing()
        self.cfg.save()
        # Off the screen first. Tearing a few thousand widgets down takes
        # Tk half a second to most of one (the freeze log's _on_close
        # entries), and a window sitting there unresponsive through it
        # reads as the app hanging on the way out.
        # withdraw() alone: it hides the window at once on Windows. An
        # update_idletasks() after it ran redraws queued for widgets whose
        # players had just been shut down, and crashed Tk (access violation,
        # freeze.log 2026-10-10).
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        self.root.destroy()


# The root, held for the life of the process. Player, prefetch and fetch
# threads all reach it through the widgets they hold; if one of them
# ended up holding the last reference when main() returns, the Tcl
# interpreter would be deleted from that thread, and on Windows Tcl
# aborts the process for that - a crash on close. Held here, it is freed
# on the main thread at exit.
_ROOT_KEEP: list = []


def main() -> None:
    # No full garbage-collection passes while starting up - see heap.
    heap.hold()
    # Windows: the taskbar's name for us, 1ms timers, and a UI thread
    # that is answered before the workers - see winsys. All no-ops
    # anywhere else.
    winsys.set_app_id()
    winsys.sharpen_timers()
    winsys.raise_ui_thread()
    winsys.full_speed()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    # The design's typefaces (Space Grotesk, Bricolage Grotesque, JetBrains
    # Mono) ship in paz_suite/fonts and are loaded for this process only -
    # nothing is installed. Before the root, so Tk sees them when
    # resolve_fonts() asks what exists.
    winsys.load_private_fonts(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"))
    root = ctk.CTk()
    _ROOT_KEEP.append(root)
    # Needs the root to exist (it asks Tk what's installed) but must run
    # before any widget is built, since T.UI/T.MONO are read at construction.
    resolve_fonts()
    root.configure(fg_color=T.BG)

    # Settled off-thread, before anything asks: importing sounddevice is
    # what loads the audio library, and on an unhappy machine that is slow
    # or worse. Nothing waits on the answer.
    audio_out.start_probe()
    libmpv_player.start_load()
    vlc_player.start_load()

    # A hung window is the one failure that leaves no evidence - nothing
    # crashed, so there is no traceback, and killing the process throws
    # away the only copy of where it stopped. This writes its own
    # post-mortem to ~/.video_tool/freeze.log instead.
    dog = Watchdog(root, os.path.join(CONFIG_DIR, "freeze.log"))
    dog.start()
    PazApp(root)
    root.after(heap.HOLD_AT_MOST_MS, heap.release)
    try:
        root.mainloop()
    finally:
        uithread.stop()
        dog.stop()
        winsys.restore_timers()


if __name__ == "__main__":
    main()
