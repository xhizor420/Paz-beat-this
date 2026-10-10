"""Right-click menus are rounded cards, not native Win32 menus."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.widgets import PopupMenu, menu_rule, popup_menu  # noqa: E402


@pytest.fixture(scope="module")
def root():
    # One root for the module, made the way conftest says: a new Tk per
    # test was what came up "no display" partway through a run.
    import tkinter as tk
    from conftest import make_root
    win = make_root(tk.Tk)
    win.withdraw()
    yield win
    try:
        win.destroy()
    except tk.TclError:
        pass


def test_popup_menu_is_not_a_tk_menu(root):
    menu = popup_menu(root)
    assert isinstance(menu, PopupMenu)
    assert not hasattr(menu, "tk") or not str(type(menu)).endswith("Menu'>")


def test_empty_menu_index_matches_tk(root):
    assert popup_menu(root).index("end") is None


def test_delete_clears(root):
    menu = popup_menu(root)
    menu.add_command(label="A", command=lambda: None)
    menu.add_command(label="B", command=lambda: None)
    menu_rule(menu)
    assert menu.index("end") == 2
    menu.delete(0, "end")
    assert menu.index("end") is None
    assert menu.items == []


def test_cascade_parent_unwraps(root):
    outer = popup_menu(root)
    inner = popup_menu(outer)
    assert inner.parent is root
    outer.add_cascade(label="More", menu=inner)
    inner.add_command(label="Inside", command=lambda: None)
    kinds = [i["kind"] for i in outer.items]
    assert kinds == ["cascade"]


def test_menu_opens_and_closes(root):
    hit = []
    menu = popup_menu(root)
    menu.add_command(label="Do it", command=lambda: hit.append("ok"))
    menu_rule(menu)
    menu.add_command(label="Quiet", state="disabled")
    root.update_idletasks()
    menu.tk_popup(40, 40)
    root.update_idletasks()
    assert menu._win is not None
    assert menu._win.winfo_exists()
    # Click the first row.
    menu._on_click(type("E", (), {"y": menu.items[0]["_y0"] + 4})())
    assert hit == ["ok"]
    assert menu._win is None
