"""The Library's tag rail, drawn on one canvas.

It used to be a CTkScrollableFrame holding a CTkButton per chip, a
CTkButton per group heading and a CTkFrame per wrapped line. A CTkButton
is three windows - a frame, the canvas it paints its rounded shape on,
and a label for its text - so a rail of a hundred and thirty chips was
close to five hundred windows: three quarters of every window in the
Library. Each one is mapped, laid out and painted on its own every time
the Library comes back on screen, and each costs a few milliseconds to
build. Pooling them (see git history) took the build cost away; nothing
could take the per-window cost away except not having the windows.

So the rail is now one tk.Canvas and a scrollbar. Chips, headings and
the "hidden tags" line are canvas items: drawing all of them is a few
milliseconds, and coming back to the Library pays for two windows, not
five hundred. Hover, click, right-click and the wheel are handled by
hit-testing the laid-out boxes.

The inspector's tag list under the player had the same shape of problem
- a CTkButton per tag, up to a hundred and sixty per group on a busy
clip, placed a few at a time over several frames because placing them
all at once froze the window - and gets the same answer: TagList, the
same canvas with rows instead of wrapped chips.

The geometry is worked out by lay_out() and lay_out_list(), which need
no window, so the wrap and the rows can be tested on their own - the
wrap is the part that has gone wrong before (see tests/test_tag_rail.py).
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field

import customtkinter as ctk

from .theme import T, px, text_width

# Design pixels, as the CTk version drew them. Everything goes through
# px() on the way to the screen.
CHIP_H = 24
CHIP_R = 6
CHIP_PAD = 18          # text-to-border padding plus the border, both sides
CHIP_GAP = 4           # between chips on a line
LINE_GAP = 2           # between wrapped lines
SWATCH = 10            # extra width a project chip's colour dot takes
LEFT = 5               # chips start this far in
RIGHT = 3              # and stop this far short of the edge
HEAD_H = 22
HEAD_R = 5
HEAD_X = 6             # heading hover plate inset
HEAD_TEXT_X = 6        # heading text inside its plate
HEAD_ABOVE_FIRST = 4
HEAD_ABOVE = 12
HEAD_BELOW = 3
HIDDEN_H = 24
HIDDEN_ABOVE = 10
HIDDEN_BELOW = 6
HIDDEN_X = 8
FALLBACK_ROOM = 236    # before the canvas has a width of its own
MIN_ROOM = 110
CHIP_FONT = 11
HEAD_FONT = 9
HIDDEN_FONT = 9


@dataclass
class Chip:
    name: str
    count: int | None       # None in the inspector, which lists one clip
    token: str
    colour: str
    swatch: str | None = None
    menu: bool = True       # tags offer a right-click menu; projects do not

    @property
    def label(self) -> str:
        return self.name if self.count is None else f"{self.name}  {self.count}"


@dataclass
class Section:
    title: str
    key: str
    open: bool
    chips: list = field(default_factory=list)
    count: int = 0
    columns: int = 1        # the inspector lists a long general group two-up

    @property
    def heading(self) -> str:
        return ("▾  " if self.open else "▸  ") + f"{self.title}   {self.count}"


@dataclass
class Box:
    """One clickable thing on the rail, in canvas pixels."""
    kind: str               # "chip", "row", "head" or "hidden"
    x0: int
    y0: int
    x1: int
    y1: int
    chip: Chip | None = None
    section: Section | None = None

    def holds(self, x: float, y: float) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1


def room_for(canvas_width: int) -> int:
    """How much of the canvas a line of chips may use, in real pixels."""
    if canvas_width < px(80):
        canvas_width = px(FALLBACK_ROOM)
    return max(canvas_width - px(LEFT) - px(RIGHT), px(MIN_ROOM))


def chip_width(chip: Chip, measure) -> int:
    width = measure(chip.label) + px(CHIP_PAD)
    if chip.swatch:
        width += px(SWATCH)
    return width


def lay_out(sections: list, canvas_width: int, measure,
            hidden: int = 0) -> tuple:
    """Where everything goes: (boxes, total height).

    `measure(text)` is the chip font's width of `text` in real pixels.
    Chips wrap to a new line when the next one will not fit; a closed
    section is its heading alone.
    """
    room = room_for(canvas_width)
    right = px(LEFT) + room
    boxes: list = []
    y = 0
    first = True
    for section in sections:
        y += px(HEAD_ABOVE_FIRST if first else HEAD_ABOVE)
        first = False
        boxes.append(Box("head", px(HEAD_X), y, max(canvas_width, right) - px(HEAD_X),
                         y + px(HEAD_H), section=section))
        y += px(HEAD_H) + px(HEAD_BELOW)
        if not section.open or not section.chips:
            continue
        x = px(LEFT)
        line_top = y + px(1)
        for chip in section.chips:
            width = chip_width(chip, measure)
            if x > px(LEFT) and x + width > right:
                x = px(LEFT)
                line_top += px(CHIP_H) + px(LINE_GAP)
            boxes.append(Box("chip", x, line_top, x + width, line_top + px(CHIP_H),
                             chip=chip, section=section))
            x += width + px(CHIP_GAP)
        y = line_top + px(CHIP_H) + px(1)
    if hidden:
        y += px(HIDDEN_ABOVE)
        boxes.append(Box("hidden", px(HIDDEN_X), y,
                         max(canvas_width, right) - px(HIDDEN_X), y + px(HIDDEN_H)))
        y += px(HIDDEN_H) + px(HIDDEN_BELOW)
    return boxes, y + px(4)


def hit(boxes: list, x: float, y: float) -> int:
    """Index of the box under (x, y), or -1."""
    for index, box in enumerate(boxes):
        if box.holds(x, y):
            return index
    return -1


# The inspector's list, in design pixels, as the CTk version drew it.
LIST_X = 6             # rows and headings sit this far in from each side
LIST_HEAD_H = 29
LIST_HEAD_R = 7
LIST_HEAD_ABOVE_FIRST = 4
LIST_HEAD_ABOVE = 8
LIST_HEAD_BELOW = 2
LIST_HEAD_TEXT_X = 8
LIST_ROW_H = 28
LIST_ROW_R = 6
LIST_ROW_GAP = 4       # between rows, and between the two columns' rows
LIST_ROW_TEXT_X = 7
LIST_MAX = 160         # rows per group; past that it is not a list anyone reads
LIST_HEAD_FONT = 11
LIST_ROW_FONT = 13
LIST_EMPTY_FONT = 11


def lay_out_list(sections: list, canvas_width: int) -> tuple:
    """The inspector's rows: (boxes, total height).

    A heading per group and a full-width row per tag under it - or two
    rows side by side, for a group that asks for two columns. Rows do
    not depend on the text, so nothing here measures anything; a name
    too long for its row is cut to fit when it is drawn.
    """
    width = max(int(canvas_width), px(120))
    left, right = px(LIST_X), width - px(LIST_X)
    boxes: list = []
    y = 0
    first = True
    for section in sections:
        y += px(LIST_HEAD_ABOVE_FIRST if first else LIST_HEAD_ABOVE)
        first = False
        boxes.append(Box("head", left, y, right, y + px(LIST_HEAD_H), section=section))
        y += px(LIST_HEAD_H) + px(LIST_HEAD_BELOW)
        if not section.open or not section.chips:
            continue
        shown = section.chips[:LIST_MAX]
        columns = 2 if section.columns == 2 else 1
        gap = px(LIST_ROW_GAP)
        pitch = px(LIST_ROW_H) + gap
        column_w = (right - left - gap * (columns - 1)) / columns
        top = y + gap // 2
        for index, chip in enumerate(shown):
            line, column = divmod(index, columns)
            x0 = int(round(left + column * (column_w + gap)))
            x1 = int(round(left + column * (column_w + gap) + column_w))
            y0 = top + line * pitch
            boxes.append(Box("row", x0, y0, x1, y0 + px(LIST_ROW_H),
                             chip=chip, section=section))
        lines = -(-len(shown) // columns)
        y = top + lines * pitch - gap // 2
    return boxes, y + px(6)


def fit_text(text: str, room: int, measure) -> str:
    """`text`, cut with an ellipsis if it is wider than `room`."""
    if room <= 0:
        return ""
    if measure(text) <= room:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid] + "…") <= room:
            low = mid
        else:
            high = mid - 1
    return text[:low] + "…" if low else "…"


def rounded(x0, y0, x1, y1, r) -> list:
    """Points for a smoothed polygon that reads as a rounded rectangle."""
    r = max(min(r, (x1 - x0) / 2, (y1 - y0) / 2), 0)
    return [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0,
            x1, y0, x1, y0 + r, x1, y0 + r, x1, y1 - r, x1, y1 - r,
            x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]


class TagCanvas(ctk.CTkFrame):
    """A rounded panel holding one canvas and its scrollbar, drawing
    boxes laid out by a subclass and handing the clicks on.

    on_chip(token)              a chip or row was clicked
    on_menu(event, token, name) a tag was right-clicked
    on_toggle(key)              a heading was clicked
    on_manage()                 the "hidden tags" line was clicked
    """

    BORDER = T.ACCENT2_DEEP

    def __init__(self, master, on_chip, on_menu, on_toggle, on_manage=None):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=12,
                         border_width=1, border_color=self.BORDER)
        self.on_chip, self.on_menu = on_chip, on_menu
        self.on_toggle, self.on_manage = on_toggle, on_manage
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        # As far in from the rounded border as CTkScrollableFrame put its
        # contents, so everything sits exactly where it used to.
        inset = px(12 + 1)
        self.canvas = tk.Canvas(self, bg=T.SURFACE, highlightthickness=0, bd=0,
                                yscrollincrement=1, takefocus=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(inset, 0), pady=inset)
        self.bar = ctk.CTkScrollbar(self, command=self.canvas.yview,
                                    fg_color="transparent",
                                    button_color=T.LINE, button_hover_color=T.FAINT)
        self.bar.grid(row=0, column=1, sticky="ns", padx=(0, 2), pady=inset)
        self.canvas.configure(yscrollcommand=self.bar.set)

        self.sections: list = []
        self.hidden = 0
        self.boxes: list = []
        self._plates: list = []      # canvas item painted on hover, per box
        self._height = 0
        self._width = 0
        self._hover = -1
        self._pressed = -1
        self._relayout_job = None

        c = self.canvas
        c.bind("<Configure>", self._on_configure)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", self._on_leave)
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Button-3>", self._on_right)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Button-4>", self._on_wheel)
        c.bind("<Button-5>", self._on_wheel)

    # What a subclass provides.

    def _lay_out(self, width: int) -> tuple:
        raise NotImplementedError

    def _draw_box(self, box: Box) -> int:
        """Draw one box; return the item that changes colour on hover."""
        raise NotImplementedError

    def _draw_extra(self) -> None:
        """Anything drawn that is not a box - an empty-list note."""

    def _plate_fill(self, box: Box, hovered: bool) -> str:
        return T.BTN_HOV if hovered else ""

    # ── contents ─────────────────────────────────────────────────────────

    def show(self, sections: list, hidden: int = 0, keep_scroll: bool = True) -> None:
        """Replace what is shown. The scroll position is kept by default,
        so folding a group or hiding a tag does not throw you to the top;
        a different clip's tags start from the top."""
        self.sections = list(sections)
        self.hidden = int(hidden)
        self._redraw(keep_scroll)

    def _redraw(self, keep_scroll: bool = True) -> None:
        c = self.canvas
        try:
            width = int(c.winfo_width())
        except tk.TclError:
            return
        self._width = width
        top = c.canvasy(0) if keep_scroll else 0
        self.boxes, self._height = self._lay_out(width)
        c.delete("all")
        self._plates = [self._draw_box(box) for box in self.boxes]
        self._hover = self._pressed = -1
        self._draw_extra()
        self._set_region(top)

    def _set_region(self, top: float = 0) -> None:
        c = self.canvas
        try:
            view = int(c.winfo_height())
        except tk.TclError:
            return
        height = max(self._height, view, 1)
        c.configure(scrollregion=(0, 0, max(self._width, 1), height))
        # Keep the same pixel at the top, as far as the new height allows.
        c.yview_moveto(max(0.0, min(top, height - view)) / height)

    def _on_configure(self, event) -> None:
        if event.width == self._width:
            self._set_region(self.canvas.canvasy(0))
            return
        # A new width re-wraps. Once per burst of resizes, not once per
        # pixel of a dragged window edge or handle.
        if self._relayout_job is None:
            self._relayout_job = self.after_idle(self._relayout)

    def _relayout(self) -> None:
        self._relayout_job = None
        self._redraw()

    # ── the pointer ──────────────────────────────────────────────────────

    def _at(self, event) -> int:
        c = self.canvas
        return hit(self.boxes, c.canvasx(event.x), c.canvasy(event.y))

    def _paint(self, index: int, hovered: bool) -> None:
        if not 0 <= index < len(self._plates):
            return
        try:
            self.canvas.itemconfigure(self._plates[index],
                                      fill=self._plate_fill(self.boxes[index], hovered))
        except tk.TclError:
            pass

    def _on_motion(self, event) -> None:
        index = self._at(event)
        if index == self._hover:
            return
        self._paint(self._hover, False)
        self._paint(index, True)
        self._hover = index
        try:
            self.canvas.configure(cursor="hand2" if index >= 0 else "")
        except tk.TclError:
            pass

    def _on_leave(self, _event) -> None:
        self._paint(self._hover, False)
        self._hover = -1
        try:
            self.canvas.configure(cursor="")
        except tk.TclError:
            pass

    def _on_press(self, event) -> None:
        self._pressed = self._at(event)

    def _on_release(self, event) -> None:
        """A click is a press and a release on the same thing, the way a
        button behaves: pressing a chip and sliding off it cancels."""
        pressed, self._pressed = self._pressed, -1
        index = self._at(event)
        if pressed < 0 or index != pressed:
            return
        box = self.boxes[index]
        if box.kind in ("chip", "row"):
            self.on_chip(box.chip.token)
        elif box.kind == "head":
            self.on_toggle(box.section.key)
        elif self.on_manage is not None:
            self.on_manage()

    def _on_right(self, event) -> None:
        index = self._at(event)
        if index < 0:
            return
        box = self.boxes[index]
        if box.kind in ("chip", "row") and box.chip.menu:
            self.on_menu(event, box.chip.token, box.chip.name)

    WHEEL_STEP = CHIP_H + LINE_GAP

    def _on_wheel(self, event) -> str | None:
        c = self.canvas
        try:
            if c.winfo_height() >= self._height:
                return None
        except tk.TclError:
            return None
        # A notch moves about one line, whatever the display scale.
        step = px(self.WHEEL_STEP)
        if getattr(event, "num", None) == 4:
            notches = -1
        elif getattr(event, "num", None) == 5:
            notches = 1
        else:
            delta = getattr(event, "delta", 0) or 0
            notches = -delta if sys.platform == "darwin" else -delta / 120
        if notches:
            c.yview_scroll(int(round(notches * step)) or (1 if notches > 0 else -1),
                           "units")
            # The pointer is over something else now.
            self._on_motion(event)
        return "break"


class TagRail(TagCanvas):
    """The sidebar: every tag in the results, as wrapped chips."""

    def __init__(self, master, on_chip, on_menu, on_toggle, on_manage):
        super().__init__(master, on_chip, on_menu, on_toggle, on_manage)
        # Sized in pixels, like CTk's own fonts: -N is N pixels, where a
        # positive size would be points and come out a different size on
        # every DPI. Measured with the same objects it is drawn with, so
        # a chip is always exactly as wide as its text needs.
        self.chip_font = tkfont.Font(family=T.UI, size=-px(CHIP_FONT))
        self.head_font = tkfont.Font(family=T.UI, size=-px(HEAD_FONT), weight="bold")
        self.hidden_font = tkfont.Font(family=T.UI, size=-px(HIDDEN_FONT))

    def measure(self, text: str) -> int:
        return text_width(self.chip_font, text)

    def _lay_out(self, width: int) -> tuple:
        return lay_out(self.sections, width, self.measure, self.hidden)

    def _plate_fill(self, box: Box, hovered: bool) -> str:
        if box.kind == "chip":
            return T.BTN_HOV if hovered else T.SURFACE
        return T.BTN_HOV if hovered else ""

    def _draw_box(self, box: Box) -> int:
        if box.kind == "chip":
            return self._draw_chip(box)
        if box.kind == "head":
            return self._draw_heading(box)
        return self._draw_hidden(box)

    def _draw_chip(self, box: Box) -> int:
        c, chip = self.canvas, box.chip
        plate = c.create_polygon(
            rounded(box.x0, box.y0, box.x1, box.y1, px(CHIP_R)), smooth=True,
            fill=T.SURFACE, outline=T.LINE, width=max(1, px(1)))
        mid = (box.y0 + box.y1) / 2
        text_x = (box.x0 + box.x1) / 2
        if chip.swatch:
            dot = px(3)
            cx = box.x0 + px(CHIP_PAD) / 2 + dot
            c.create_oval(cx - dot, mid - dot, cx + dot, mid + dot,
                          fill=chip.swatch, outline="")
            text_x += px(SWATCH) / 2
        c.create_text(text_x, mid, text=chip.label, fill=chip.colour,
                      font=self.chip_font, anchor="center")
        return plate

    def _draw_heading(self, box: Box) -> int:
        c = self.canvas
        plate = c.create_polygon(
            rounded(box.x0, box.y0, box.x1, box.y1, px(HEAD_R)), smooth=True,
            fill="", outline="")
        c.create_text(box.x0 + px(HEAD_TEXT_X), (box.y0 + box.y1) / 2,
                      text=box.section.heading, fill=T.FAINT,
                      font=self.head_font, anchor="w")
        return plate

    def _draw_hidden(self, box: Box) -> int:
        c = self.canvas
        plate = c.create_polygon(
            rounded(box.x0, box.y0, box.x1, box.y1, px(HEAD_R)), smooth=True,
            fill="", outline="")
        noun = "tag" if self.hidden == 1 else "tags"
        c.create_text((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2,
                      text=f"{self.hidden} hidden {noun} · manage", fill=T.FAINT,
                      font=self.hidden_font, anchor="center")
        return plate


class TagList(TagCanvas):
    """The inspector: one clip's tags, a row each, under group headings."""

    BORDER = T.LINE
    WHEEL_STEP = LIST_ROW_H + LIST_ROW_GAP
    EMPTY = "No tags for this clip yet - press “Fix missing” up top."

    def __init__(self, master, on_chip, on_menu, on_toggle):
        super().__init__(master, on_chip, on_menu, on_toggle)
        self.head_font = tkfont.Font(family=T.UI, size=-px(LIST_HEAD_FONT),
                                     weight="bold")
        self.row_font = tkfont.Font(family=T.UI, size=-px(LIST_ROW_FONT))
        self.empty_font = tkfont.Font(family=T.UI, size=-px(LIST_EMPTY_FONT))
        # Whether to say there are no tags, as against showing nothing
        # because no clip is selected.
        self.empty = False
        self._fitted: dict = {}

    def show(self, sections: list, hidden: int = 0, keep_scroll: bool = True,
             empty: bool = False) -> None:
        self.empty = empty and not sections
        super().show(sections, hidden, keep_scroll)

    def _lay_out(self, width: int) -> tuple:
        return lay_out_list(self.sections, width)

    def _plate_fill(self, box: Box, hovered: bool) -> str:
        if box.kind == "head":
            return T.BTN_HOV if hovered else T.ELEVATED
        return T.BTN_HOV if hovered else ""

    def _fit(self, text: str, room: int) -> str:
        key = (text, room)
        got = self._fitted.get(key)
        if got is None:
            got = fit_text(text, room, lambda t: text_width(self.row_font, t))
            if len(self._fitted) > 4000:
                self._fitted.clear()
            self._fitted[key] = got
        return got

    def _draw_box(self, box: Box) -> int:
        c = self.canvas
        mid = (box.y0 + box.y1) / 2
        if box.kind == "head":
            plate = c.create_polygon(
                rounded(box.x0, box.y0, box.x1, box.y1, px(LIST_HEAD_R)),
                smooth=True, fill=T.ELEVATED, outline="")
            c.create_text(box.x0 + px(LIST_HEAD_TEXT_X), mid,
                          text=box.section.heading,
                          fill=T.FAINT, font=self.head_font, anchor="w")
            return plate
        plate = c.create_polygon(
            rounded(box.x0, box.y0, box.x1, box.y1, px(LIST_ROW_R)),
            smooth=True, fill="", outline="")
        room = box.x1 - box.x0 - 2 * px(LIST_ROW_TEXT_X)
        c.create_text(box.x0 + px(LIST_ROW_TEXT_X), mid,
                      text=self._fit(box.chip.label, room), fill=box.chip.colour,
                      font=self.row_font, anchor="w")
        return plate

    def _draw_extra(self) -> None:
        if not self.empty:
            return
        width = max(self._width - 2 * px(10), px(80))
        self.canvas.create_text(px(10), px(10), text=self.EMPTY, fill=T.FAINT,
                                font=self.empty_font, anchor="nw", width=width)
        self._height = max(self._height, px(60))
