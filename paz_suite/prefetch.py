"""Read the clips you are about to play before you play them.

The library and the 4K pool live on one large SMR hard drive. Opening a
clip that is not in memory means seeks on a drive that is often busy with
Topaz, and that is most of the second between pressing Play and seeing a
picture. A clip that was read recently opens from Windows' file cache in
RAM instead - and this machine has tens of gigabytes of RAM sitting idle.

So whenever the selection moves, or the pointer settles on a card, the
clip it points at and its neighbours are read through once, here, in the
background. Nothing is kept by this module: the reading is the whole
point, and Windows holds the pages. Walking the results with Up and Down
then lands on clips that are already warm.

Three rules keep it from getting in the way:

* One reader, and the newest request wins. Arrowing past a clip abandons
  its read part-way rather than finishing a file nobody will watch.
* The reader runs in Windows background mode - low disk priority as well
  as low CPU - so it queues behind the clip that is actually playing.
* A file read in the last few minutes is not read again.
"""

from __future__ import annotations

import collections
import os
import threading
import time

from . import winsys

CHUNK = 2 << 20
# How long a read counts as still warm. Windows keeps the pages while
# nothing needs the memory, which on this machine is a long time; this
# only stops the same handful of neighbours being re-read on every step.
WARM_FOR = 600.0
# Remembered reads, newest last. Bookkeeping only - no data is held.
REMEMBER = 512

_lock = threading.Lock()
_wanted: list = []
_generation = 0
_warm: "collections.OrderedDict" = collections.OrderedDict()
_wake = threading.Event()
_thread = None
_stats = {"files": 0, "bytes": 0, "abandoned": 0}


def want(paths) -> None:
    """Read these, in this order, instead of whatever was asked before.
    Returns at once; safe from any thread."""
    global _generation, _thread
    paths = [p for p in dict.fromkeys(paths) if p]
    with _lock:
        _wanted[:] = paths
        _generation += 1
        if _thread is None:
            _thread = threading.Thread(target=_run, daemon=True, name="prefetch")
            _thread.start()
    _wake.set()


def stats() -> dict:
    with _lock:
        return dict(_stats)


def _is_warm(key) -> bool:
    seen = _warm.get(key)
    if seen is None:
        return False
    if time.monotonic() - seen > WARM_FOR:
        _warm.pop(key, None)
        return False
    return True


def _run() -> None:
    winsys.background_thread()
    while True:
        _wake.wait()
        with _lock:
            if not _wanted:
                _wake.clear()
                continue
            path = _wanted.pop(0)
            generation = _generation
        _read(path, generation)


def _read(path: str, generation: int) -> None:
    try:
        st = os.stat(path)
    except OSError:
        return
    key = (path, st.st_size, st.st_mtime_ns)
    with _lock:
        if _is_warm(key):
            _warm.move_to_end(key)
            return
    done = 0
    try:
        with open(path, "rb", buffering=0) as fh:
            while True:
                with _lock:
                    # Moved on: drop this one unless it is still asked for.
                    superseded = generation != _generation and path not in _wanted
                if superseded:
                    with _lock:
                        _stats["abandoned"] += 1
                    return
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                done += len(chunk)
    except OSError:
        return
    with _lock:
        _warm[key] = time.monotonic()
        _warm.move_to_end(key)
        while len(_warm) > REMEMBER:
            _warm.popitem(last=False)
        _stats["files"] += 1
        _stats["bytes"] += done
