"""e621 requests are paced in one place, for every fetcher at once."""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import e621  # noqa: E402


def _reset(gap: float) -> None:
    e621._pace_gap = gap
    e621._pace_next = 0.0


def test_two_fetchers_together_still_keep_the_gap():
    _reset(0.05)
    starts = []
    lock = threading.Lock()

    def fetcher():
        for _ in range(3):
            e621._pace()
            with lock:
                starts.append(time.monotonic())

    threads = [threading.Thread(target=fetcher) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 6
    assert min(gaps) >= 0.04          # one shared queue, not one per fetcher
    _reset(e621.PACE)


def test_a_rate_limit_answer_slows_down_and_good_answers_recover():
    _reset(e621.PACE)
    e621._answered(429)
    assert e621._pace_gap == e621.PACE * 2
    for _ in range(20):
        e621._answered(503)
    assert e621._pace_gap == e621.PACE_MAX
    for _ in range(40):
        e621._answered(200)
    assert e621._pace_gap == e621.PACE
    e621._answered(404)               # about the post, not the pace
    assert e621._pace_gap == e621.PACE
    _reset(e621.PACE)
