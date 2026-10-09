"""Shared test helpers."""

from __future__ import annotations

import time

import pytest


def make_root(factory):
    """A Tk root for a test, or a skip that says why there isn't one.

    These used to skip with "no display" on any TclError. On Windows the
    common cause is not a missing display at all: real-time antivirus
    briefly holding one of Tk's own .tcl files while the interpreter
    reads it. That skip hid real failures - a run where it happened
    reported a broken test as skipped. So: try again a couple of times,
    which gets past the lock, and if it still will not come up, skip
    with the actual error, where it can be seen.
    """
    import tkinter as tk
    error = None
    for attempt in range(3):
        try:
            return factory()
        except tk.TclError as exc:
            error = exc
            time.sleep(0.3 * (attempt + 1))
    first_line = str(error).strip().splitlines()[0] if str(error).strip() else repr(error)
    pytest.skip(f"Tk would not start: {first_line[:160]}")
