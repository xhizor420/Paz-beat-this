"""One safe way for a background thread to touch the UI.

Every tab does its slow work (probing, thumbnailing, encoding, beat
analysis) on a worker thread and hands the result back with
`widget.after(0, ...)`. That is fine *while the main loop is running*,
but Tk's `createcommand` blocks for a second waiting for the main loop
whenever it's called from a non-main thread and then raises

    RuntimeError: main thread is not in main loop

There are two windows where that bites. At startup the tabs are built -
and their first worker threads started - inside `PazApp.__init__`, which
runs before `root.mainloop()`; a fast worker finishing in that gap kills
its own thread with the traceback above and silently loses the result
(no census, no thumbnails). At shutdown the same call raises `TclError`
once the interpreter is gone.

So worker threads post here instead. Until the main loop is confirmed
running, callbacks are parked in a queue; the moment it starts they are
flushed in order and everything afterwards goes straight through to
`after(0, ...)` exactly as before, so there's no added latency in the
normal case. Callbacks that arrive while the window is being torn down
are dropped rather than raising.
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections import deque

_lock = threading.Lock()
_pending: deque = deque()
_root = None
_running = False


def install(root) -> None:
    """Point the dispatcher at the app's root window. Call once, from the
    main thread, before any worker thread is started."""
    global _root
    with _lock:
        _root = root
    # Runs as soon as the main loop starts spinning - which is exactly the
    # moment it becomes safe for other threads to schedule callbacks.
    try:
        root.after(0, _activate)
    except (RuntimeError, tk.TclError):
        pass


def _activate() -> None:
    global _running
    with _lock:
        _running = True
        parked = list(_pending)
        _pending.clear()
    for fn, args, kwargs in parked:
        _invoke(fn, args, kwargs)


def _invoke(fn, args, kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except tk.TclError:
        pass  # window went away between posting and running


def post(fn, *args, **kwargs) -> None:
    """Run `fn(*args, **kwargs)` on the UI thread. Safe from any thread,
    at any point in the app's life."""
    with _lock:
        root, running = _root, _running
        if not running:
            _pending.append((fn, args, kwargs))
            return
    try:
        root.after(0, lambda: _invoke(fn, args, kwargs))
    except (RuntimeError, tk.TclError):
        pass  # shutting down


def reset() -> None:
    """Forget the installed root - only needed by tests."""
    global _root, _running
    with _lock:
        _root = None
        _running = False
        _pending.clear()
