"""A tab that is switched to must be the thing on screen.

The hidden tabs are laid out ahead of time underneath the one showing
(PazApp._lay_out_early). That used a bare lower(), which put them beneath
every child of the tabview - including the canvas it paints its own
background on - and they stayed there: switching to one showed that
background, so every tab but the first came up as a black panel. The
tab was "mapped" all along, which is why checking that proved nothing;
what matters is the stacking order, so that is what this checks.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk                                        # noqa: E402

import customtkinter as ctk                                 # noqa: E402

from paz_suite import app as appmod                         # noqa: E402

_ROOTS: list = []       # freed on the main thread at exit - see app._ROOT_KEEP


@pytest.fixture(scope="module")
def root():
    try:
        window = ctk.CTk()
    except tk.TclError:
        pytest.skip("no display")
    _ROOTS.append(window)
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


class Shell:
    """Just the parts of PazApp these two methods use."""
    _select_tab = appmod.PazApp._select_tab
    _show_tab = appmod.PazApp._show_tab
    _lay_out_early = appmod.PazApp._lay_out_early

    def __init__(self, root):
        self.root = type("R", (), {"after": lambda *_a: None})()
        self.tabview = ctk.CTkTabview(root)
        self.tabview.pack(fill="both", expand=True)
        for name in appmod.TAB_NAMES:
            frame = self.tabview.add(name)
            tk.Label(frame, text=name).pack()
        self.changed = 0

    def _on_tab_changed(self):
        self.changed += 1

    def _build_rest(self):
        pass


def stacking(tabview) -> list:
    """Children of the tabview, bottom first."""
    return [str(w) for w in tabview.tk.splitlist(
        tabview.tk.call("winfo", "children", str(tabview)))]


def above_the_background(shell, name) -> bool:
    order = stacking(shell.tabview)
    return order.index(str(shell.tabview.tab(name))) > order.index(str(shell.tabview._canvas))


@pytest.mark.parametrize("first", appmod.TAB_NAMES)
def test_every_tab_is_seen_when_switched_to(root, first):
    shell = Shell(root)
    try:
        shell._select_tab(first)
        root.update()
        for name in appmod.TAB_NAMES:
            shell._lay_out_early(name)        # what _build_rest does per tab
        root.update()
        for name in list(appmod.TAB_NAMES) + [first]:
            shell._select_tab(name)
            root.update()
            assert shell.tabview.get() == name
            assert shell.tabview.tab(name).winfo_ismapped()
            assert above_the_background(shell, name), \
                f"{name} is beneath the tabview's background (started on {first})"
    finally:
        shell.tabview.destroy()


def test_laying_out_early_leaves_the_showing_tab_on_top(root):
    shell = Shell(root)
    try:
        shell._select_tab("Library")
        root.update()
        shell._lay_out_early("Vault")
        root.update()
        order = stacking(shell.tabview)
        assert shell.tabview.get() == "Library"
        assert not shell.tabview.tab("Vault").winfo_ismapped()
        assert order.index(str(shell.tabview.tab("Library"))) > \
            order.index(str(shell.tabview._canvas))
    finally:
        shell.tabview.destroy()


def test_a_quick_second_switch_is_not_undone_by_the_first(root):
    """CTkTabview.set() leaves a 100ms timer that hides every tab but the
    one it was given. Switching again inside that window used to end with
    the tab you switched to hidden."""
    import time
    shell = Shell(root)
    try:
        shell._select_tab("Convert")
        root.update()
        shell._select_tab("Library")
        shell._select_tab("Vault")            # well inside 100ms
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            root.update()
            time.sleep(0.01)
        assert shell.tabview.get() == "Vault"
        assert shell.tabview.tab("Vault").winfo_ismapped(), "Vault was hidden"
        assert above_the_background(shell, "Vault")
        for other in ("Convert", "Library", "Beat This"):
            assert not shell.tabview.tab(other).winfo_ismapped()
    finally:
        shell.tabview.destroy()
