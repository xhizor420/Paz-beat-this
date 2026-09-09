"""Saved searches: a name and the text you would have typed.

The list itself is plain data, so the awkward cases can be pinned without
a window - a name reused, a rename onto a name already taken, whitespace,
a name long enough to wreck the chip row.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import (                      # noqa: E402
    SAVED_NAME_MAX, clean_saved_name, delete_saved, find_saved,
    rename_saved, save_search)


# ── saving ──────────────────────────────────────────────────────────────

def test_saving_keeps_the_name_and_the_query():
    saved, what = save_search([], "Wolves", "wolf duo -solo is:4k")
    assert what == "added"
    assert saved == [{"name": "Wolves", "query": "wolf duo -solo is:4k"}]


def test_saving_over_a_name_replaces_it():
    """Two chips with the same label doing different things is not a
    feature."""
    saved, _ = save_search([], "Wolves", "wolf")
    saved, what = save_search(saved, "Wolves", "wolf duo is:4k")
    assert what == "updated"
    assert saved == [{"name": "Wolves", "query": "wolf duo is:4k"}]


def test_the_name_is_matched_however_it_was_capitalised():
    saved, _ = save_search([], "Wolves", "wolf")
    saved, what = save_search(saved, "wolves", "canine")
    assert what == "updated"
    assert len(saved) == 1
    assert saved[0]["name"] == "Wolves", "the original spelling is kept"


def test_the_same_query_can_be_saved_under_two_names():
    saved, _ = save_search([], "Wolves", "wolf is:4k")
    saved, what = save_search(saved, "Big wolves", "wolf is:4k")
    assert what == "added"
    assert len(saved) == 2


def test_nothing_is_saved_without_both_halves():
    assert save_search([], "", "wolf")[1] == ""
    assert save_search([], "Wolves", "")[1] == ""
    assert save_search([], "   ", "   ")[1] == ""


def test_whitespace_is_tidied_on_the_way_in():
    saved, _ = save_search([], "  Wolves  ", "  wolf   duo  ")
    assert saved[0] == {"name": "Wolves", "query": "wolf duo"}


def test_a_very_long_name_is_cut_to_fit():
    saved, _ = save_search([], "w" * 200, "wolf")
    assert len(saved[0]["name"]) == SAVED_NAME_MAX


def test_saving_does_not_mutate_the_list_it_was_given():
    before = [{"name": "Wolves", "query": "wolf"}]
    save_search(before, "Foxes", "fox")
    assert before == [{"name": "Wolves", "query": "wolf"}]


def test_order_is_the_order_they_were_saved():
    saved = []
    for name in ("One", "Two", "Three"):
        saved, _ = save_search(saved, name, name.lower())
    assert [e["name"] for e in saved] == ["One", "Two", "Three"]


# ── renaming ────────────────────────────────────────────────────────────

def test_renaming_keeps_the_query_and_the_position():
    saved = []
    for name in ("One", "Two", "Three"):
        saved, _ = save_search(saved, name, name.lower())
    saved, ok = rename_saved(saved, "Two", "Second")
    assert ok
    assert [e["name"] for e in saved] == ["One", "Second", "Three"]
    assert find_saved(saved, "Second")["query"] == "two"


def test_renaming_onto_a_taken_name_is_refused():
    """Merging them silently would lose one."""
    saved, _ = save_search([], "Wolves", "wolf")
    saved, _ = save_search(saved, "Foxes", "fox")
    after, ok = rename_saved(saved, "Foxes", "Wolves")
    assert not ok
    assert after == saved


def test_renaming_to_its_own_name_is_allowed():
    saved, _ = save_search([], "Wolves", "wolf")
    after, ok = rename_saved(saved, "Wolves", "Wolves")
    assert ok and len(after) == 1


def test_renaming_something_that_is_not_there():
    saved, _ = save_search([], "Wolves", "wolf")
    after, ok = rename_saved(saved, "Nope", "Something")
    assert not ok and after == saved


def test_renaming_to_nothing_is_refused():
    saved, _ = save_search([], "Wolves", "wolf")
    after, ok = rename_saved(saved, "Wolves", "   ")
    assert not ok and after == saved


# ── forgetting ──────────────────────────────────────────────────────────

def test_forgetting_removes_only_that_one():
    saved = []
    for name in ("One", "Two", "Three"):
        saved, _ = save_search(saved, name, name.lower())
    saved = delete_saved(saved, "Two")
    assert [e["name"] for e in saved] == ["One", "Three"]


def test_forgetting_something_that_is_not_there_changes_nothing():
    saved, _ = save_search([], "Wolves", "wolf")
    assert delete_saved(saved, "Foxes") == saved


def test_forgetting_does_not_mutate_the_list_it_was_given():
    before = [{"name": "Wolves", "query": "wolf"}]
    delete_saved(before, "Wolves")
    assert before == [{"name": "Wolves", "query": "wolf"}]


# ── looking up ──────────────────────────────────────────────────────────

def test_find_ignores_case_and_padding():
    saved, _ = save_search([], "Wolves", "wolf")
    assert find_saved(saved, "  wOlVeS ") is not None
    assert find_saved(saved, "foxes") is None


def test_clean_name_handles_junk():
    assert clean_saved_name(None) == ""
    assert clean_saved_name("  a   b ") == "a b"


# ── round trip through the config file ──────────────────────────────────

def test_saved_searches_survive_a_save_and_reload(tmp_path, monkeypatch):
    """They are worth nothing if they do not come back next session."""
    from paz_suite import config as cfg_mod
    path = tmp_path / "paz_config.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(path))
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(tmp_path))

    cfg = cfg_mod.AppConfig()
    cfg.saved_searches, _ = save_search([], "Wolves", "wolf duo -solo is:4k")
    cfg.save()

    reloaded = cfg_mod.AppConfig.load()
    assert reloaded.saved_searches == [
        {"name": "Wolves", "query": "wolf duo -solo is:4k"}]


def test_a_config_written_before_this_existed_still_loads(tmp_path, monkeypatch):
    import json
    from paz_suite import config as cfg_mod
    path = tmp_path / "paz_config.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(path))
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(tmp_path))
    path.write_text(json.dumps({"sort": "Newest"}), encoding="utf-8")
    assert cfg_mod.AppConfig.load().saved_searches == []


# ── the scrubber's duration format ──────────────────────────────────────

def test_scrubber_durations_are_timecode_not_prose():
    """fmt_len says "1 min 12 sec", which is right in a caption and wrong
    beside a running clock - it is prose next to a timecode, and wide
    enough to push the readout off the end of the panel."""
    from paz_suite.format import fmt_short
    assert fmt_short(0) == "0:00"
    assert fmt_short(None) == "0:00"
    assert fmt_short(-5) == "0:00"
    assert fmt_short(9) == "0:09"
    assert fmt_short(72) == "1:12"
    assert fmt_short(600) == "10:00"
    assert fmt_short(3600) == "1:00:00"
    assert fmt_short(3671) == "1:01:11"


def test_scrubber_duration_is_never_wider_than_a_timecode():
    from paz_suite.format import fmt_short
    for seconds in (0, 1, 59, 60, 599, 600, 3599, 3600, 86399):
        assert len(fmt_short(seconds)) <= 8
