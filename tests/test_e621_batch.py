"""Tagging a hundred posts per request instead of one.

At e621's one request a second, a ten-thousand-clip library took about
three hours to tag one post at a time. A search for `id:a,b,c...` answers
a hundred posts in the same one request.

It is a search, not a lookup, so it is trusted only as far as it can be
checked: only posts that were asked for are kept, anything else in the
answer means the search did not do what it was meant to and batching is
switched off, and a post the search left out is asked about on its own -
because only a lookup can say a post is gone, and "missing" is forever.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import e621, library_tab                       # noqa: E402
from paz_suite.library_tab import LibraryTab                  # noqa: E402


def post(pid, artist="kenket"):
    return {"id": int(pid), "tags": {"artist": [artist], "general": ["wolf"]},
            "rating": "e", "score": {"total": 7},
            "created_at": "2024-01-01T00:00:00Z"}


@pytest.fixture
def meta(tmp_path, monkeypatch):
    monkeypatch.setattr(e621, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(e621, "E621_META_PATH", str(tmp_path / "meta.json"))
    monkeypatch.setattr(e621, "E621_META_LOG", str(tmp_path / "meta.log"))
    made = e621.E621Meta()
    made.wait_until_read()
    return made


def serve(monkeypatch, answer):
    """Every request gets `answer(url)`: a dict to send back as JSON, or
    an HTTP status to fail with. Returns the list of URLs asked for."""
    asked = []

    def urlopen(request, timeout=0):
        url = request.full_url
        asked.append(url)
        result = answer(url)
        if isinstance(result, int):
            raise urllib.error.HTTPError(url, result, "no", {}, None)
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(e621.urllib.request, "urlopen", urlopen)
    return asked


def ids_in(url):
    tags = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["tags"][0]
    first = tags.split()[0]
    return first[len("id:"):].split(",")


# ── fetch_many ─────────────────────────────────────────────────────────

def test_one_request_answers_many_posts(meta, monkeypatch):
    asked = serve(monkeypatch, lambda url: {"posts": [post(p) for p in ids_in(url)]})
    answer = meta.fetch_many(["101", "102", "103"])
    assert len(asked) == 1
    assert ids_in(asked[0]) == ["101", "102", "103"]
    assert set(answer["records"]) == {"101", "102", "103"}
    assert meta.get("102")["artist"] == ["kenket"]
    assert meta.get("102")["url"] == e621.E621_POST.format(pid="102")


def test_the_record_is_the_same_shape_as_a_lookup(meta, monkeypatch):
    serve(monkeypatch, lambda url: {"posts": [post("101")]})
    batch = dict(meta.fetch_many(["101"])["records"]["101"])
    serve(monkeypatch, lambda url: {"post": post("101")})
    single = dict(meta.fetch("101"))
    for record in (batch, single):
        record.pop("fetched_at", None)
    assert batch == single


def test_a_post_left_out_is_not_marked_missing(meta, monkeypatch):
    serve(monkeypatch, lambda url: {"posts": [post("101")]})
    answer = meta.fetch_many(["101", "102"])
    assert set(answer["records"]) == {"101"}
    assert meta.get("102") is None


def test_a_post_that_was_not_asked_for_switches_batching_off(meta, monkeypatch):
    """What a search that ignored the id list would look like: the
    newest posts on the site. None of them may be kept."""
    serve(monkeypatch, lambda url: {"posts": [post("101"), post("999999")]})
    answer = meta.fetch_many(["101", "102"])
    assert answer.get("unsupported") is True
    assert meta.get("101") is None
    assert meta.get("999999") is None


@pytest.mark.parametrize("status,refused", [(400, True), (422, True),
                                            (429, False), (503, False),
                                            (500, False)])
def test_which_failures_mean_the_search_is_refused(meta, monkeypatch, status, refused):
    serve(monkeypatch, lambda url: status)
    answer = meta.fetch_many(["101"])
    assert answer.get("error")
    assert bool(answer.get("unsupported")) is refused
    assert meta.get("101") is None


def test_a_strange_answer_is_refused(meta, monkeypatch):
    serve(monkeypatch, lambda url: {"success": False})
    assert meta.fetch_many(["101"]).get("unsupported") is True


def test_never_more_than_a_batch_and_only_numbers(meta, monkeypatch):
    asked = serve(monkeypatch, lambda url: {"posts": []})
    meta.fetch_many([str(n) for n in range(1, 500)] + ["../etc"])
    assert len(ids_in(asked[0])) == e621.BATCH_SIZE
    assert all(i.isdigit() for i in ids_in(asked[0]))


def test_the_login_goes_with_the_request_only_when_both_are_set(meta, monkeypatch):
    asked = serve(monkeypatch, lambda url: {"posts": []})
    meta.fetch_many(["101"], "someone", "")
    meta.fetch_many(["101"], "someone", "k3y")
    assert "login" not in asked[0]
    assert "login=someone" in asked[1] and "api_key=k3y" in asked[1]


# ── the fetch loop ─────────────────────────────────────────────────────

class FakeMeta:
    """Answers like E621Meta, from a script."""

    def __init__(self, known=(), gone=(), batch=None, batching=True):
        self.known = set(known)
        self.gone = set(gone)
        self.batch = batch          # None: answer from `known`
        self.batching = batching
        self.batch_calls = []
        self.single_calls = []

    def fetch_many(self, pids, user="", key=""):
        self.batch_calls.append(list(pids))
        if self.batch is not None:
            return self.batch(pids)
        return {"records": {p: {"tags": "x"} for p in pids if p in self.known}}

    def fetch(self, pid, user="", key=""):
        self.single_calls.append(pid)
        if pid in self.gone:
            return {"missing": True, "error": "HTTP 404"}
        if pid in self.known:
            return {"tags": "x"}
        return {"error": "HTTP 503"}

    def checkpoint(self):
        pass

    def save(self):
        pass


class Cfg:
    e621_fetch_delay = 1.0
    e621_user = ""
    e621_key = ""


class Nothing:
    def set(self, *_a):
        pass

    def configure(self, **_kw):
        pass


class FakeTab:
    _run_fetch = LibraryTab._run_fetch
    _fetch_release = LibraryTab._fetch_release

    def __init__(self, emeta):
        self.emeta = emeta
        self.cfg = Cfg()
        self.progress = self.more_btn = Nothing()
        self.busy = False
        self.done = None

    def cancel_tag_fetch(self):
        pass

    def F(self, key, **_kw):
        return key

    def set_status(self, *_a):
        pass

    def ui(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def _fetch_done(self, hits, missing, failed, last_error, refreshed,
                    pids, cancelled, ambient):
        self.done = {"hits": hits, "missing": missing, "failed": failed,
                     "error": last_error, "pids": pids}


@pytest.fixture
def instant(monkeypatch):
    """The worker runs on the spot and the pauses take no time."""
    class Now:
        def __init__(self, target, daemon=True):
            self.target = target

        def start(self):
            self.target()

    class Event:
        def __init__(self):
            self.flag = False
            self.waits = 0

        def set(self):
            self.flag = True

        def is_set(self):
            return self.flag

        def wait(self, _seconds):
            self.waits += 1
            return self.flag

    monkeypatch.setattr(library_tab.threading, "Thread", Now)
    monkeypatch.setattr(library_tab.threading, "Event", Event)


def pids(n, start=1):
    return [str(i) for i in range(start, start + n)]


def test_a_backlog_goes_a_hundred_at_a_time(instant):
    meta = FakeMeta(known=pids(250))
    tab = FakeTab(meta)
    tab._run_fetch(pids(250), 0)
    assert [len(c) for c in meta.batch_calls] == [100, 100, 50]
    assert meta.single_calls == []
    assert tab.done["hits"] == 250
    assert tab.done["pids"] == pids(250)
    assert tab._fetch_stop.waits == 2, "one pause between requests, not per post"


def test_what_the_search_leaves_out_is_looked_up_on_its_own(instant):
    meta = FakeMeta(known=pids(8), gone={"9"})
    tab = FakeTab(meta)
    tab._run_fetch(pids(10), 0)
    assert meta.single_calls == ["9", "10"]
    assert tab.done["hits"] == 8
    assert tab.done["missing"] == 1
    assert tab.done["failed"] == 1


def test_a_refused_search_falls_back_to_one_at_a_time(instant):
    meta = FakeMeta(known=pids(5), batch=lambda p: {"error": "HTTP 422",
                                                     "unsupported": True})
    tab = FakeTab(meta)
    tab._run_fetch(pids(5), 0)
    assert len(meta.batch_calls) == 1
    assert meta.batching is False
    assert meta.single_calls == pids(5)
    assert tab.done["hits"] == 5


def test_a_passing_failure_retries_the_same_hundred(instant):
    answers = iter([{"error": "HTTP 503"}, None])

    def flaky(p):
        answer = next(answers)
        return answer or {"records": {x: {} for x in p}}
    meta = FakeMeta(batch=flaky)
    tab = FakeTab(meta)
    tab._run_fetch(pids(3), 0)
    assert meta.batch_calls == [pids(3), pids(3)]
    assert tab.done["hits"] == 3
    assert tab.done["failed"] == 0


def test_a_run_of_failures_gives_up(instant):
    meta = FakeMeta(batch=lambda p: {"error": "HTTP 503"})
    tab = FakeTab(meta)
    tab._run_fetch(pids(300), 0)
    assert len(meta.batch_calls) == e621.GIVE_UP_AFTER
    assert tab.done["hits"] == 0
    assert tab.done["error"] == "HTTP 503"
    assert tab.done["pids"] == []


def test_without_batching_it_is_the_old_loop(instant):
    meta = FakeMeta(known=pids(3), batching=False)
    tab = FakeTab(meta)
    tab._run_fetch(pids(3), 0)
    assert meta.batch_calls == []
    assert meta.single_calls == pids(3)
    assert tab.done["hits"] == 3


def test_a_cancel_stops_between_requests(instant):
    meta = FakeMeta(known=pids(300))
    tab = FakeTab(meta)
    original = meta.fetch_many

    def then_cancel(p, *a):
        tab._fetch_stop.set()
        return original(p, *a)
    meta.fetch_many = then_cancel
    tab._run_fetch(pids(300), 0)
    assert tab.done["hits"] == 100
    assert tab.done["pids"] == pids(100)
