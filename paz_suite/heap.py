"""Keeping Python's garbage collector out of the way of the window.

Reference counting frees almost everything this app lets go of. The
cyclic collector exists for the rest, and every so often it runs a full
pass over every object the program holds - which here is ten thousand
clip records, every tag on each of them, and the e621 cache behind them.
Measured: 86ms, on the UI thread, at a moment nobody chose. That is a
page turn or a click that randomly takes four times as long as the one
before it, and it was the largest of the stalls left.

Nearly all of those objects are made once, by the library load, and
kept. gc.freeze() moves everything alive at that moment into a
generation the collector never scans, so later passes only look at what
is new. It is redone after every load: unfreeze first, so a full pass
can still find any cycle the previous library left behind, then freeze
what is alive now.

That one pass is itself a pause, so it waits until the load has settled
and the window has had time to draw - and it happens once per load, not
whenever the collector happens to feel like it.

At launch it does not happen at all. The collector is held off from the
start (hold) - measured, it ran two full passes during startup, 42ms
and 100ms - and the first settle freezes without collecting, the way
the gc documentation suggests for exactly this. What that costs is any
cycle of garbage made while starting up, kept for the life of the
program: a one-off, and small. Every later load collects properly.
"""

from __future__ import annotations

import gc

SETTLE_AFTER_MS = 2500

# How long startup may keep the collector held off, if the library load
# never gets far enough to settle it - an unreadable database, say.
HOLD_AT_MOST_MS = 20000

_pending: dict = {}
_held = {"on": False}


def hold() -> None:
    """No automatic collection until the first settle(). For startup."""
    gc.disable()
    _held["on"] = True


def settle() -> None:
    """Stop scanning what is alive now - collecting first, except for the
    first settle after hold()."""
    _pending.pop("job", None)
    if _held["on"]:
        _held["on"] = False
        gc.freeze()
        gc.enable()
        return
    gc.unfreeze()
    gc.collect()
    gc.freeze()


def release() -> None:
    """The safety net for hold(): settle if nothing else has."""
    if _held["on"]:
        settle()


def settle_soon(widget, delay_ms: int = SETTLE_AFTER_MS) -> None:
    """settle(), once things have gone quiet. A second call before the
    first has run replaces it."""
    job = _pending.pop("job", None)
    if job is not None:
        try:
            widget.after_cancel(job)
        except Exception:
            pass
    try:
        _pending["job"] = widget.after(delay_ms, settle)
    except Exception:
        pass
