"""The collector's full pass happens once per library load, at a time the
app picks - not in the middle of a click. See paz_suite/heap.py."""

from __future__ import annotations

import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import heap                                  # noqa: E402


class Widget:
    def __init__(self):
        self.jobs = {}
        self.cancelled = []
        self._n = 0

    def after(self, _ms, fn):
        self._n += 1
        self.jobs[self._n] = fn
        return self._n

    def after_cancel(self, job):
        self.cancelled.append(job)
        self.jobs.pop(job, None)


def test_settling_freezes_what_is_alive():
    try:
        kept = [object() for _ in range(1000)]
        heap.settle()
        assert gc.get_freeze_count() >= len(kept)
    finally:
        gc.unfreeze()


def test_settling_again_still_finds_old_cycles():
    """Frozen objects are never scanned, so a cycle frozen once would be
    kept for ever - unless each settle unfreezes before it collects."""
    import weakref

    class Node:
        pass

    try:
        a, b = Node(), Node()
        a.other, b.other = b, a
        watch = weakref.ref(a)
        heap.settle()
        del a, b
        heap.settle()
        assert watch() is None, "a frozen cycle outlived the next settle"
    finally:
        gc.unfreeze()


def test_a_burst_of_loads_settles_once():
    widget = Widget()
    for _ in range(3):
        heap.settle_soon(widget)
    assert len(widget.jobs) == 1
    assert len(widget.cancelled) == 2
    try:
        next(iter(widget.jobs.values()))()
    finally:
        gc.unfreeze()


def test_startup_holds_the_collector_off_and_the_first_settle_does_not_scan(monkeypatch):
    collected = []
    monkeypatch.setattr(heap.gc, "collect", lambda *a: collected.append(1) or 0)
    try:
        heap.hold()
        assert not gc.isenabled()
        heap.settle()
        assert gc.isenabled(), "the collector never came back on"
        assert collected == [], "the first settle paid for a full pass"
        heap.settle()                    # a later load does collect
        assert collected == [1]
    finally:
        gc.enable()
        gc.unfreeze()


def test_the_safety_net_turns_the_collector_back_on():
    try:
        heap.hold()
        heap.release()
        assert gc.isenabled()
        heap.release()                   # and is harmless when not held
        assert gc.isenabled()
    finally:
        gc.enable()
        gc.unfreeze()
