"""A hung window has to say where it hung.

It is the one failure that leaves no evidence by itself: nothing crashed,
so there is no traceback, and killing the process throws away the only
copy of where it stopped. On a machine you cannot reach, that reduces
diagnosis to guessing at platform differences - so the app writes its own
post-mortem instead.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import watchdog                             # noqa: E402
from paz_suite.watchdog import Watchdog                    # noqa: E402


class FakeRoot:
    """Stands in for the Tk root: records what was scheduled, and can be
    told to stop servicing the queue, which is what a freeze is."""

    def __init__(self):
        self.jobs = []
        self.frozen = False
        self.report_callback_exception = None

    def after(self, _ms, func, *args):
        if not self.frozen:
            self.jobs.append((func, args))
        return "job"

    def beat(self):
        """Run whatever is pending, the way a healthy event loop would."""
        pending, self.jobs = self.jobs, []
        for func, args in pending:
            func(*args)


@pytest.fixture
def dog(tmp_path):
    root = FakeRoot()
    watcher = Watchdog(root, str(tmp_path / "freeze.log"))
    watcher.STUCK_AFTER = 0.3
    yield watcher, root
    watcher.stop()


def read(dog) -> str:
    path = dog.log_path
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def test_a_stopped_heartbeat_is_reported(dog, monkeypatch):
    watcher, root = dog
    monkeypatch.setattr("paz_suite.watchdog.STUCK_AFTER", 0.3)
    watcher.start()
    root.frozen = True                  # the event loop stops answering
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "FROZEN" not in read(watcher):
        time.sleep(0.05)
    assert "FROZEN" in read(watcher), "a hung window went unreported"


def test_the_report_names_the_stuck_thread(dog, monkeypatch):
    watcher, root = dog
    monkeypatch.setattr("paz_suite.watchdog.STUCK_AFTER", 0.3)
    watcher.start()
    root.frozen = True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "FROZEN" not in read(watcher):
        time.sleep(0.05)
    text = read(watcher)
    assert "MAIN/UI" in text, "no way to tell which thread was stuck"
    assert "--- thread" in text
    # and it should say how long, so a two-second stall is not confused
    # with a real hang
    assert "has not responded for" in text


def test_a_healthy_window_is_not_reported(dog, monkeypatch):
    watcher, root = dog
    monkeypatch.setattr("paz_suite.watchdog.STUCK_AFTER", 0.6)
    watcher.start()
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        root.beat()                     # keep answering
        time.sleep(0.05)
    assert "FROZEN" not in read(watcher), "cried wolf on a working window"


def test_one_report_per_hang_not_one_per_tick(dog, monkeypatch):
    watcher, root = dog
    monkeypatch.setattr("paz_suite.watchdog.STUCK_AFTER", 0.3)
    monkeypatch.setattr("paz_suite.watchdog.REARM_AFTER", 60.0)
    watcher.start()
    root.frozen = True
    time.sleep(3.0)
    assert read(watcher).count("FROZEN") == 1


def test_a_ui_callback_error_is_recorded(dog):
    watcher, root = dog
    watcher.start()
    assert callable(root.report_callback_exception)
    try:
        raise ValueError("something in a button handler")
    except ValueError:
        root.report_callback_exception(*sys.exc_info())
    text = read(watcher)
    assert "error in a UI callback" in text
    assert "something in a button handler" in text


def test_starting_is_recorded_so_an_empty_log_means_it_never_ran(dog):
    watcher, _root = dog
    watcher.start()
    assert "started" in read(watcher)


def test_an_unwritable_log_does_not_take_the_app_down(tmp_path):
    root = FakeRoot()
    watcher = Watchdog(root, str(tmp_path / "nope" / "\0bad" / "freeze.log"))
    watcher.start()          # must not raise
    watcher.stop()


def test_the_log_is_rolled_when_it_gets_big(tmp_path):
    path = tmp_path / "freeze.log"
    path.write_bytes(b"x" * (3 * 1024 * 1024))
    root = FakeRoot()
    watcher = Watchdog(root, str(path))
    watcher.start()
    watcher.stop()
    assert os.path.exists(str(path) + ".old"), "old log was not kept"
    assert path.stat().st_size < 3 * 1024 * 1024


def test_the_watcher_thread_does_not_outlive_stop(dog, monkeypatch):
    watcher, root = dog
    monkeypatch.setattr("paz_suite.watchdog.STUCK_AFTER", 0.3)
    watcher.start()
    watcher.stop()
    time.sleep(1.5)
    names = [t.name for t in threading.enumerate() if "_watch" in t.name]
    assert not names, f"watcher thread still running: {names}"


# ── a pause worth noticing, short of a hang ─────────────────────────────

def test_a_pause_is_recorded_without_waiting_for_a_freeze(tmp_path):
    """An app does not have to freeze to feel broken. Until now only death
    was recorded, so a session that stuttered left nothing to read."""
    log = tmp_path / "freeze.log"
    dog = watchdog.Watchdog(root=None, log_path=str(log))
    dog._beat = time.monotonic() - 2.0          # two seconds of stillness
    dog._lag(2.0)
    text = log.read_text()
    assert "LAGGY" in text
    assert "paused for 2.0s" in text


def test_pauses_are_counted_so_a_bad_session_is_obvious(tmp_path):
    log = tmp_path / "freeze.log"
    dog = watchdog.Watchdog(root=None, log_path=str(log))
    for _ in range(3):
        dog._lag(1.8)
    assert "pause #3 this session" in log.read_text()


def test_a_pause_says_where_the_thread_was(tmp_path):
    log = tmp_path / "freeze.log"
    dog = watchdog.Watchdog(root=None, log_path=str(log))

    def somewhere_specific():
        dog._lag(1.6)

    somewhere_specific()
    assert "somewhere_specific" in log.read_text()


def test_a_freeze_is_still_a_freeze_not_just_a_pause():
    assert watchdog.LAGGY_AFTER < watchdog.STUCK_AFTER
    # And a stutter must not drown out the full report for a real hang.
    assert watchdog.LAG_REARM_AFTER < watchdog.REARM_AFTER
