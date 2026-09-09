"""Keyboard bindings, checked as a set rather than one at a time.

Tk's bind() replaces whatever was on that sequence before, without a
word. So a tab claiming a key another tab already had does not conflict -
it silently wins, and the older feature simply stops working. That is
invisible in review and invisible at runtime until someone reaches for
the old key, so it is checked here instead: one handler per sequence,
dispatching on the active tab.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "paz_suite", "app.py")


def bound_sequences() -> list:
    """Every key sequence handed to root.bind, including the ones bound in
    a loop over several spellings of the same key."""
    tree = ast.parse(open(APP, encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "bind"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "root"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            found.append(node.args[0].value)
        elif node.args and isinstance(node.args[0], ast.Name):
            found.append(f"<loop:{node.args[0].id}>")
    return found


def looped_keys() -> list:
    src = open(APP, encoding="utf-8").read()
    keys = []
    for group in re.findall(r'for key in \(([^)]*)\):', src):
        keys += re.findall(r'"([^"]+)"', group)
    for group in re.findall(r'for digit in range\((\d+)\)', src):
        keys += [str(d) for d in range(int(group))]
    return keys


def test_no_sequence_is_bound_twice():
    literal = [s for s in bound_sequences() if not s.startswith("<loop:")]
    clashes = {k: n for k, n in Counter(literal).items() if n > 1}
    assert not clashes, (
        f"bound more than once, so only the last one runs: {clashes}")


def test_no_looped_key_collides_with_a_literal_binding():
    literal = {s for s in bound_sequences() if not s.startswith("<loop:")}
    clashes = sorted(k for k in looped_keys() if k in literal)
    assert not clashes, f"bound both directly and in a loop: {clashes}"


def test_no_key_is_claimed_by_two_loops():
    keys = looped_keys()
    clashes = {k: n for k, n in Counter(keys).items() if n > 1}
    assert not clashes, f"claimed by two loops: {clashes}"


def test_the_transport_keys_are_all_there():
    """The set a cutting tool is expected to have. If one goes missing in
    a refactor this says which."""
    want = {",", ".", "<Home>", "<End>", "<Left>", "<Right>",
            "<Shift-Left>", "<Shift-Right>", "<space>", "<Return>"}
    have = set(bound_sequences()) | set(looped_keys())
    assert not (want - have), f"transport keys missing: {sorted(want - have)}"


def test_every_digit_jumps():
    have = set(looped_keys())
    assert not ({str(d) for d in range(10)} - have), "digit jump keys missing"


# ── the help window has to describe the keys that exist ─────────────────

# How the help spells a key, and the Tk sequence it must be bound as.
# Only keys the help actually names need an entry.
HELP_TO_TK = {
    "Enter": "<Return>", "Space": "<space>", "Esc": "<Escape>",
    "Home": "<Home>", "End": "<End>", "PgUp": "<Prior>", "PgDn": "<Next>",
    "F5": "<F5>", "/": "/", ",": ",", ".": ".",
    "R": "r", "G": "g", "M": "m",
    "Ctrl+A": "<Control-a>", "Ctrl+L": "<Control-l>", "Ctrl+C": "<Control-c>",
    "Ctrl+Shift+C": "<Control-Shift-C>", "Ctrl+F": "<Control-f>",
    "Ctrl+O": "<Control-o>", "Ctrl+T": "<Control-t>",
    "←→": ("<Left>", "<Right>"),
    "Shift+←→": ("<Shift-Left>", "<Shift-Right>"),
    "0-9": tuple(str(d) for d in range(10)),
}


def help_key_sections() -> str:
    from paz_suite.library_windows import HelpWindow
    return " ".join(text for title, text in HelpWindow.SECTIONS
                    if title.startswith("Keys"))


def test_every_key_the_help_names_is_actually_bound():
    """The help and the bindings are edited in different files, so they
    drift. A key that was renamed or dropped leaves the help telling
    people to press something that does nothing."""
    text = help_key_sections()
    assert text, "no Keys section in the help at all"
    have = set(bound_sequences()) | set(looped_keys())
    missing = []
    for spelling, sequences in HELP_TO_TK.items():
        if spelling not in text:
            continue
        for seq in ((sequences,) if isinstance(sequences, str) else sequences):
            if seq not in have:
                missing.append(f"{spelling} -> {seq}")
    assert not missing, f"help names keys that are not bound: {missing}"


def test_the_help_mentions_frame_stepping():
    """The one that matters for cutting to a beat, and the one nobody
    guesses is there."""
    text = help_key_sections()
    assert "," in text and "." in text and "frame" in text.lower()


# ── startup cost ────────────────────────────────────────────────────────

def test_importing_the_app_does_not_drag_in_torch():
    """torch is half a gigabyte resident and several seconds of loading.
    Nothing in the browsing path may reach for it - the Beat This tab
    probes its dependencies when it is first opened, not at startup."""
    import subprocess
    import sys as _sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        [_sys.executable, "-c",
         "import sys; sys.path.insert(0, %r);"
         "import paz_suite.app, paz_suite.beat_tab, paz_suite.library_tab;"
         "print('torch' in sys.modules)" % root],
        capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "False", (
        "importing the app pulled in torch - that is ~470 MB and several "
        "seconds paid by every session, including ones that never leave "
        "the Library")
