"""The speed check's report: what it says has to match what it measured."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.speedcheck import SpeedCheck                 # noqa: E402


class Lib:
    records = [object()] * 10
    cfg = type("Cfg", (), {"page_size": 48})()


def check(results):
    sc = SpeedCheck.__new__(SpeedCheck)
    sc.lib = Lib()
    sc.results = results
    return sc


def test_rows_group_by_action_in_the_order_run():
    sc = check([("turn a page", 10.0, ""), ("click a clip", 5.0, ""),
                ("turn a page", 30.0, ""), ("turn a page", 20.0, "")])
    rows = sc.rows()
    assert [r[0] for r in rows] == ["turn a page", "click a clip"]
    label, runs, median, worst, errors = rows[0]
    assert (runs, median, worst, errors) == (3, 20.0, 30.0, [])


def test_a_failure_is_reported_not_hidden():
    sc = check([("sort", None, "TclError: boom"), ("sort", 12.0, "")])
    text = sc.report_text()
    assert "failed: TclError: boom" in text
    assert sc.rows()[0][1] == 1


def test_slow_actions_are_flagged_and_the_summary_names_the_slowest():
    sc = check([("theater on/off", 140.0, ""), ("a search", 20.0, "")])
    text = sc.report_text()
    assert "over 100 ms" in text
    assert "theater on/off at 140 ms" in sc.summary()
