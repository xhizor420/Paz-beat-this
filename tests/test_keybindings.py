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
