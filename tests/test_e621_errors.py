"""A failed request must never be remembered as a missing post.

`missing` is a permanent verdict. is_stale() refuses to re-check a record
carrying it, and _fetch_tags only queues posts with no record at all - so
a post marked missing is never asked about again for the life of the
cache file.

Every HTTP error was being cached that way. e621's own rate limit is one
request a second and the fetcher runs at exactly that, so a bulk tagging
run over ten thousand clips is precisely the situation that earns a 429 -
and every post that got one was silently condemned to stay untagged
forever. The same for a 503 during maintenance, or a 401 from a key that
had expired. Only "this post does not exist" is worth remembering.
"""

from __future__ import annotations

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import e621                                    # noqa: E402


@pytest.fixture
def meta(tmp_path, monkeypatch):
    monkeypatch.setattr(e621, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(e621, "E621_META_PATH", str(tmp_path / "meta.json"))
    monkeypatch.setattr(e621, "E621_META_LOG", str(tmp_path / "meta.log"))
    return e621.E621Meta()


def answers(monkeypatch, status):
    """Make the next request come back as this HTTP status."""
    def raise_it(*_a, **_kw):
        raise urllib.error.HTTPError("u", status, "no", {}, None)

    monkeypatch.setattr(e621.urllib.request, "urlopen", raise_it)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 401, 403, 408])
def test_a_request_level_failure_is_not_cached(meta, monkeypatch, status):
    answers(monkeypatch, status)
    record = meta.fetch("123")
    assert record.get("error")
    assert not record.get("missing"), f"HTTP {status} was cached as missing"
    assert meta.get("123") is None, \
        f"HTTP {status} left a record behind, so the post is never retried"


@pytest.mark.parametrize("status", [404, 410])
def test_a_post_that_is_really_gone_is_remembered(meta, monkeypatch, status):
    answers(monkeypatch, status)
    record = meta.fetch("123")
    assert record.get("missing") is True
    assert (meta.get("123") or {}).get("missing") is True


def test_a_network_error_is_still_not_cached(meta, monkeypatch):
    def down(*_a, **_kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(e621.urllib.request, "urlopen", down)
    assert meta.fetch("123").get("error")
    assert meta.get("123") is None


def test_a_rate_limited_post_can_be_fetched_on_a_later_run(meta, monkeypatch):
    """The whole point: the first run fails, the second one works."""
    answers(monkeypatch, 429)
    meta.fetch("123")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def ok(*_a, **_kw):
        return Response()

    monkeypatch.setattr(e621.urllib.request, "urlopen", ok)
    monkeypatch.setattr(e621.json, "load", lambda _fh: {
        "post": {"tags": {"artist": ["kenket"], "general": ["wolf"]},
                 "rating": "e", "score": {"total": 12},
                 "created_at": "2024-01-01T00:00:00Z"}})
    record = meta.fetch("123")
    assert record["artist"] == ["kenket"]
    assert "wolf" in record["tags"]
    assert (meta.get("123") or {}).get("artist") == ["kenket"]


def test_a_missing_post_is_never_due_for_a_recheck(meta, monkeypatch):
    """Confirming the cost of getting the above wrong."""
    answers(monkeypatch, 404)
    meta.fetch("123")
    assert meta.is_stale("123") is False
    assert meta.due_for_refresh(["123"], budget=10) == []


def test_the_give_up_threshold_is_a_run_not_a_single_post():
    """A guard that fired on one failure would stop a normal run; one
    that never fires leaves a dead service to be discovered three hours
    later, one post per second."""
    assert 2 < e621.GIVE_UP_AFTER < 50
