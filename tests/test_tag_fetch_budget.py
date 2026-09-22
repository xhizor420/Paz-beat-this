"""The quiet tag fetch must not lock the tab for an evening.

Opening the Library (or finishing a sync) kicks off an ambient e621
fetch for anything untagged. e621 allows about two requests a second, so
a library with ten thousand untagged post IDs is hours of fetching - and
the whole time, the tab is busy, which means Sync and Fetch are both
refused. Ambient work takes a batch and leaves the rest for next time.
Pressing Fetch yourself is the unbudgeted way, and stays that way.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_tab import LibraryTab      # noqa: E402


class Rec:
    def __init__(self, pid, used=()):
        self.pid = pid
        # Clips that have been in an edit are the ones whose tags are
        # worth keeping current first - see due_for_refresh's `prefer`.
        self.used_projects = list(used)


class Cfg:
    e621_enabled = True
    # Two budgets: the backlog of never-fetched posts gets the bigger
    # allowance, the soft refresh the smaller one. See AppConfig.
    library_fetch_budget = 40
    library_stale_refresh_budget = 40


class Meta:
    def __init__(self, cached=()):
        self.cached = set(cached)
        self.asked = []

    def get(self, pid):
        return {"tags": []} if pid in self.cached else None

    def due_for_refresh(self, pids, budget, exclude=(), prefer=()):
        self.asked.append({"budget": budget, "exclude": set(exclude),
                           "prefer": set(prefer)})
        return []


class FakeTab:
    _fetch_tags = LibraryTab._fetch_tags
    tagging = False
    cancelled = False

    def cancel_tag_fetch(self):
        self.cancelled = True

    def __init__(self, count=10_000, cached=(), used=()):
        self.busy = False
        self.cfg = Cfg()
        self.emeta = Meta(cached)
        used = set(used)
        self.records = [Rec(str(1000 + i),
                            ["PMV"] if str(1000 + i) in used else [])
                        for i in range(count)]
        self.status = []
        self.fetched = None

    def set_status(self, text, colour=None):
        self.status.append(text)

    def _run_fetch(self, todo, refreshing, note="", ambient=False):
        self.fetched = (list(todo), refreshing, note)
        self.ambient = ambient

    def F(self, key, **fmt):
        return key


def test_the_ambient_fetch_takes_a_batch_not_the_whole_library():
    tab = FakeTab(count=10_000)
    tab._fetch_tags()
    todo, _refreshing, note = tab.fetched
    assert len(todo) == Cfg.library_fetch_budget
    assert "9,960 more next time" in note


# ── the backlog and the refresh are budgeted separately ────────────────
#
# They are different jobs. An untagged clip cannot be searched by
# artist, character or species at all, while a refresh only sharpens a
# score that is already roughly right - so the backlog gets the bigger
# allowance, and one number for both meant the smaller one governed.

def test_the_backlog_is_governed_by_its_own_budget():
    tab = FakeTab(count=10_000)
    tab.cfg.library_fetch_budget = 500
    tab.cfg.library_stale_refresh_budget = 10
    tab._fetch_tags()
    todo, _refreshing, _note = tab.fetched
    assert len(todo) == 500


def test_the_refresh_is_governed_by_its_own_budget():
    tab = FakeTab(count=10_000)
    tab.cfg.library_fetch_budget = 500
    tab.cfg.library_stale_refresh_budget = 10
    tab._fetch_tags()
    assert tab.emeta.asked[-1]["budget"] == 10


def test_a_zero_backlog_budget_means_take_them_all():
    """0 is the "no limit" value, the way it is elsewhere in the config -
    not "fetch nothing", which would silently switch tagging off."""
    tab = FakeTab(count=10_000)
    tab.cfg.library_fetch_budget = 0
    tab._fetch_tags()
    todo, _refreshing, note = tab.fetched
    assert len(todo) == 10_000
    assert note == ""


def test_pressing_fetch_lifts_both_budgets():
    tab = FakeTab(count=10_000)
    tab.cfg.library_fetch_budget = 5
    tab.cfg.library_stale_refresh_budget = 5
    tab._fetch_tags(full=True)
    todo, _refreshing, _note = tab.fetched
    assert len(todo) == 10_000
    assert tab.emeta.asked[-1]["budget"] == 10_000


def test_clips_that_have_been_used_are_named_as_preferred():
    """A post on a clip that has been in an edit is one you will reach
    for again, so its tags and score are the ones worth keeping
    current - see due_for_refresh."""
    tab = FakeTab(count=50, used=["1003", "1007"])
    tab._fetch_tags()
    assert tab.emeta.asked[-1]["prefer"] == {"1003", "1007"}


def test_nothing_used_prefers_nothing():
    tab = FakeTab(count=20)
    tab._fetch_tags()
    assert tab.emeta.asked[-1]["prefer"] == set()


def test_the_backlog_is_not_offered_to_the_refresh_as_well():
    """A post cannot be both never-fetched and due for a re-check, and
    asking for it twice would spend the budget on it twice."""
    tab = FakeTab(count=30)
    tab.cfg.library_fetch_budget = 10
    tab._fetch_tags()
    todo, _refreshing, _note = tab.fetched
    assert tab.emeta.asked[-1]["exclude"] == set(todo)


def test_pressing_fetch_yourself_still_catches_up_everything():
    tab = FakeTab(count=10_000)
    tab._fetch_tags(full=True)
    todo, _refreshing, note = tab.fetched
    assert len(todo) == 10_000
    assert note == ""


def test_a_small_backlog_is_done_in_one_go_with_nothing_left_over():
    tab = FakeTab(count=12)
    tab._fetch_tags()
    todo, _refreshing, note = tab.fetched
    assert len(todo) == 12
    assert note == ""


def test_nothing_untagged_means_no_fetch_at_all():
    tab = FakeTab(count=5, cached=[str(1000 + i) for i in range(5)])
    tab._fetch_tags()
    assert tab.fetched is None
    assert tab.status and "already tagged" in tab.status[-1]


def test_a_busy_tab_is_left_alone():
    tab = FakeTab(count=100)
    tab.busy = True
    tab._fetch_tags()
    assert tab.fetched is None


# ── ambient work must not stand in the user's way ───────────────────────

def test_the_tab_opening_starts_an_ambient_batch():
    tab = FakeTab(count=100)
    tab._fetch_tags()
    assert tab.ambient is True


def test_pressing_fetch_is_not_ambient():
    tab = FakeTab(count=100)
    tab._fetch_tags(full=True)
    assert tab.ambient is False


def test_ambient_work_does_not_start_on_top_of_itself():
    tab = FakeTab(count=100)
    tab.tagging = True
    tab._fetch_tags()
    assert tab.fetched is None


def test_pressing_fetch_cuts_in_on_ambient_work():
    """e621 allows two requests a second, so a batch runs for a while. The
    button must not have to queue behind one the app started itself."""
    tab = FakeTab(count=100)
    tab.tagging = True
    tab._fetch_tags(full=True)
    assert tab.cancelled
    assert tab.fetched is not None


# ── a finished run must not tidy away the one that replaced it ──────────

class Button:
    def __init__(self):
        self.state = "disabled"

    def configure(self, state=None, **kw):
        if state:
            self.state = state


class Runner:
    _fetch_release = LibraryTab._fetch_release

    def __init__(self):
        self.busy = True
        self._fetch_running = True
        self.more_btn = Button()
        self._fetch_stop = object()

    def ui(self, fn, *a, **kw):
        fn(*a, **kw)


def test_a_finished_run_hands_back_what_it_was_holding():
    tab = Runner()
    assert tab._fetch_release(tab._fetch_stop, ambient=False) is True
    assert tab.busy is False
    assert tab._fetch_running is False
    assert tab.more_btn.state == "normal"


def test_a_cancelled_run_does_not_release_its_replacement():
    """It finishes a moment after the thing that cancelled it started, so
    an unconditional release unlocks the new run's buttons and tells the
    app nothing is fetching while a fetch is underway."""
    tab = Runner()
    stale = tab._fetch_stop
    tab._fetch_stop = object()            # the replacement claims the slot
    assert tab._fetch_release(stale, ambient=True) is False
    assert tab.busy is True
    assert tab._fetch_running is True
    assert tab.more_btn.state == "disabled"


def test_an_ambient_run_never_touches_the_buttons():
    tab = Runner()
    tab.busy = False
    tab._fetch_release(tab._fetch_stop, ambient=True)
    assert tab._fetch_running is False
    assert tab.more_btn.state == "disabled"   # left exactly as it found it


# ── what the refresh checks first ──────────────────────────────────────
#
# is_stale answers "is this due". The order is a different question:
# which of the due ones is worth asking about, given only so many
# requests a second. Three factors, in this order - whether you have
# used the clip, whether the record has any tags at all, and how new the
# post is - because the first two are about whether an answer is worth
# having and the third is only about how likely it is to have changed.

def refresher(records, now=1_700_000_000.0):
    """A real E621Meta, with a cache built by hand and no network."""
    import types
    from paz_suite import e621
    meta = e621.E621Meta.__new__(e621.E621Meta)
    import threading
    meta._lock = threading.Lock()
    meta._ready = threading.Event()
    meta._ready.set()
    meta._data = dict(records)
    meta._dirty = False
    meta._pending = set()
    meta._now = now
    # is_stale reads the clock; pin it so the test is not about today.
    meta.is_stale = types.MethodType(
        lambda self, pid, when=None: e621.E621Meta.is_stale(self, pid, now),
        meta)
    return meta


def record(fetched_days_ago, post_days_old, tags="wolf canine",
           now=1_700_000_000.0):
    return {"tags": tags, "score": 5,
            "fetched_at": now - fetched_days_ago * 86400,
            "created_at": now - post_days_old * 86400}


def test_a_used_clip_is_checked_before_an_unused_one():
    meta = refresher({
        "unused": record(400, 900),
        "used": record(400, 900),
    })
    assert meta.due_for_refresh(["unused", "used"], 2,
                                prefer={"used"})[0] == "used"


def test_a_record_with_no_tags_is_checked_before_one_with_some():
    """Either the post gained tags since, or a bad fetch lost them.
    Either way there is more to gain than from re-checking a post that
    already has forty."""
    meta = refresher({
        "tagged": record(400, 900),
        "bare": record(400, 900, tags=""),
    })
    assert meta.due_for_refresh(["tagged", "bare"], 2)[0] == "bare"


def test_among_equals_the_newer_post_is_checked_first():
    meta = refresher({
        "old": record(400, 2000),
        "new": record(400, 40),
    })
    assert meta.due_for_refresh(["old", "new"], 2)[0] == "new"


def test_being_used_outranks_being_bare():
    meta = refresher({
        "bare": record(400, 900, tags=""),
        "used": record(400, 900),
    })
    assert meta.due_for_refresh(["bare", "used"], 2,
                                prefer={"used"})[0] == "used"


def test_the_budget_still_caps_the_result():
    meta = refresher({str(i): record(400, 900) for i in range(50)})
    assert len(meta.due_for_refresh([str(i) for i in range(50)], 7)) == 7


def test_nothing_excluded_comes_back():
    meta = refresher({"a": record(400, 900), "b": record(400, 900)})
    assert meta.due_for_refresh(["a", "b"], 5, exclude=["a"]) == ["b"]


def test_a_post_that_is_not_due_is_not_offered_however_preferred():
    """Preference is about order, not about overriding the schedule."""
    meta = refresher({"fresh": record(0, 10)})
    assert meta.due_for_refresh(["fresh"], 5, prefer={"fresh"}) == []


# ── the bigger pull has to reach an install that already exists ────────

def test_a_config_carrying_the_old_default_is_moved_up(tmp_path, monkeypatch):
    """A default that only applies to fresh installs never reaches
    anyone who already has a config, which is everyone it changed for."""
    import json
    from paz_suite import config as cfgmod
    path = tmp_path / "paz_config.json"
    path.write_text(json.dumps({"library_stale_refresh_budget": 40}),
                    encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    cfg = cfgmod.AppConfig.load()
    assert cfg.library_stale_refresh_budget == \
        cfgmod.AppConfig.library_stale_refresh_budget
    assert cfg.fetch_budget_upgraded is True


def test_a_budget_the_user_actually_chose_is_left_alone(tmp_path, monkeypatch):
    import json
    from paz_suite import config as cfgmod
    path = tmp_path / "paz_config.json"
    path.write_text(json.dumps({"library_stale_refresh_budget": 7}),
                    encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    assert cfgmod.AppConfig.load().library_stale_refresh_budget == 7


def test_the_move_happens_once_and_then_never_again(tmp_path, monkeypatch):
    import json
    from paz_suite import config as cfgmod
    path = tmp_path / "paz_config.json"
    path.write_text(json.dumps({"library_stale_refresh_budget": 40}),
                    encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    cfgmod.AppConfig.load()                      # moves it, and saves
    again = cfgmod.AppConfig.load()
    again.library_stale_refresh_budget = 40      # set back by hand
    again.save()
    assert cfgmod.AppConfig.load().library_stale_refresh_budget == 40


def test_the_new_budget_is_bigger_than_the_re_check_one():
    """They are different jobs and the backlog is the one worth clearing."""
    from paz_suite.config import AppConfig
    assert AppConfig.library_fetch_budget > \
        AppConfig.library_stale_refresh_budget
