"""Handing a result from a worker thread back to the UI.

A freeze log caught a worker wedged inside Tk's createcommand, reached
through root.after(). Tk is not something two threads may touch, so the
rule here is absolute: a post appends and nothing else, and the main
thread comes and collects. These pin that, because the cost of getting it
wrong is a frozen window rather than a wrong answer.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import uithread                            # noqa: E402


class FakeRoot:
    """Records after() calls and who made them."""

    def __init__(self):
        self.scheduled = []
        self.callers = []

    def after(self, _ms, func, *args):
        self.callers.append(threading.current_thread())
        self.scheduled.append((func, args))
        return "job"

    def run_once(self):
        pending, self.scheduled = self.scheduled, []
        for func, args in pending:
            func(*args)


@pytest.fixture
def root():
    uithread.reset()
    fake = FakeRoot()
    uithread.install(fake)
    yield fake
    uithread.reset()


def test_posting_does_not_touch_tk(root):
    """The whole point. root.after() from a worker is a worker inside the
    Tcl interpreter."""
    before = len(root.callers)
    uithread.post(lambda: None)
    assert len(root.callers) == before, "posting reached into Tk"


def test_posting_from_a_worker_thread_does_not_touch_tk(root):
    before = len(root.callers)
    done = threading.Event()

    def worker():
        uithread.post(lambda: None)
        done.set()

    threading.Thread(target=worker).start()
    assert done.wait(2)
    assert len(root.callers) == before, "a worker reached into Tk"


def test_only_the_installing_thread_ever_schedules(root):
    here = threading.current_thread()
    done = threading.Event()
    threading.Thread(target=lambda: (uithread.post(lambda: None),
                                     done.set())).start()
    assert done.wait(2)
    root.run_once()      # the pump, on this thread
    assert all(caller is here for caller in root.callers)


def test_what_was_posted_actually_runs(root):
    seen = []
    uithread.post(seen.append, "a")
    uithread.post(seen.append, "b")
    assert seen == [], "ran before the UI thread asked for it"
    root.run_once()
    assert seen == ["a", "b"]


def test_order_is_kept(root):
    seen = []
    for index in range(50):
        uithread.post(seen.append, index)
    root.run_once()
    assert seen == list(range(50))


def test_the_pump_keeps_going(root):
    seen = []
    uithread.post(seen.append, 1)
    root.run_once()
    uithread.post(seen.append, 2)
    root.run_once()
    assert seen == [1, 2], "the pump stopped rescheduling itself"


def test_work_posted_before_the_loop_starts_is_not_lost(root):
    """Tabs build their first workers inside __init__, before mainloop."""
    uithread.reset()
    seen = []
    uithread.post(seen.append, "early")
    fake = FakeRoot()
    uithread.install(fake)
    fake.run_once()
    assert seen == ["early"]


def test_a_failing_callback_does_not_strand_the_rest(root):
    seen = []

    def boom():
        raise ValueError("callback blew up")

    uithread.post(boom)
    uithread.post(seen.append, "after")
    root.run_once()
    assert seen == ["after"], "one bad callback stopped every other worker"


def test_a_flood_is_taken_in_slices(root):
    """Running ten thousand at once would stall the window, which is the
    thing this module exists to avoid."""
    seen = []
    for index in range(uithread.MAX_PER_PUMP * 3):
        uithread.post(seen.append, index)
    root.run_once()
    assert len(seen) == uithread.MAX_PER_PUMP
    root.run_once()
    assert len(seen) == uithread.MAX_PER_PUMP * 2


def test_stopping_ends_the_pump(root):
    seen = []
    uithread.stop()
    uithread.post(seen.append, "x")
    root.run_once()
    assert seen == []


def test_posting_is_quick_even_under_contention(root):
    """It is called from every worker in the app, including ones in tight
    loops."""
    start = time.monotonic()
    for _ in range(20000):
        uithread.post(lambda: None)
    assert time.monotonic() - start < 1.0
