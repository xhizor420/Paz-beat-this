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

So worker threads post here instead - and, since a freeze log showed a
worker wedged inside `createcommand`, they no longer call Tk at all. A
post only appends to a queue. The main thread drains that queue on a
pump it schedules for itself, which is the only arrangement where Tk is
touched by exactly one thread. Callbacks that arrive before the main loop
starts simply wait for the first pump; ones that arrive while the window
is being torn down are dropped rather than raising.
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections import deque

# How often the main thread looks for work handed over by a worker. Small
# enough that a result appearing is indistinguishable from immediate, and
# cheap: an empty pump is a deque check.
PUMP_MS = 10
# Anything more than this waiting means a worker is posting faster than
# the UI can draw; running them all in one pump would stall the window
# instead, so a pump takes a bounded slice and comes straight back.
MAX_PER_PUMP = 200

_lock = threading.Lock()
_pending: deque = deque()
_root = None
_pumping = False


def install(root) -> None:
    """Point the dispatcher at the app's root window. Call once, from the
    main thread, before any worker thread is started."""
    global _root, _pumping
    with _lock:
        _root = root
        _pumping = True
    # Scheduled from the main thread, which is the only thread allowed to
    # do this. Every later pump reschedules itself from inside the loop,
    # so no other thread ever calls into Tk.
    try:
        root.after(PUMP_MS, _pump)
    except (RuntimeError, tk.TclError):
        with _lock:
            _pumping = False


def _pump() -> None:
    """Run what the workers left, on the UI thread."""
    global _pumping
    with _lock:
        root, pumping = _root, _pumping
    if not pumping:
        return
    for _ in range(MAX_PER_PUMP):
        with _lock:
            if not _pending:
                break
            fn, args, kwargs = _pending.popleft()
        _invoke(fn, args, kwargs)
    try:
        root.after(PUMP_MS, _pump)
    except (RuntimeError, tk.TclError):
        with _lock:
            _pumping = False


def _invoke(fn, args, kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except tk.TclError:
        pass  # window went away between posting and running
    except Exception:
        # A failing callback must not stop the pump - that would strand
        # every later hand-off from every worker in the app.
        import traceback
        traceback.print_exc()


def post(fn, *args, **kwargs) -> None:
    """Run `fn(*args, **kwargs)` on the UI thread. Safe from any thread,
    at any point in the app's life.

    Does not touch Tk. A worker calling root.after() is a worker calling
    into the Tcl interpreter, which is not something two threads may do;
    a freeze log caught one wedged inside createcommand. Appending is all
    that happens here - the main thread comes and collects.
    """
    with _lock:
        _pending.append((fn, args, kwargs))


def stop() -> None:
    """Stop pumping, on the way down."""
    global _pumping
    with _lock:
        _pumping = False


def reset() -> None:
    """Forget the installed root - only needed by tests."""
    global _root, _pumping
    with _lock:
        _root = None
        _pumping = False
        _pending.clear()
