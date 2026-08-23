"""e621 tag lookup — one cache, shared by Convert and Library.

Grabber downloads carry nothing but the post ID ("6574692.webm"), so a
freshly converted library starts out tagless. e621's public JSON API turns
that ID back into artist, characters, species, rating and score. Rules of
the road: a descriptive User-Agent, roughly one request per second, and a
local cache so no post is ever asked about twice. Only the ID is ever sent -
no filenames, no paths, no thumbnails.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .config import CONFIG_DIR, E621_META_PATH

APP_NAME = "PAZ Suite"
# Still finding its edges - Resolve, the beat models and the
# 4K paths all landed recently. 1.0 when those have been lived
# with for a while.
APP_VERSION = "0.8 beta"

E621_API = "https://e621.net/posts/{pid}.json"
E621_POST = "https://e621.net/posts/{pid}"
E621_UA = f"{APP_NAME}/{APP_VERSION} (personal library tagger)"

# Artist-category tags that aren't actually artists.
_ARTIST_NOISE = {"conditional_dnp", "avoid_posting", "unknown_artist",
                  "sound_warning", "epilepsy_warning", "third-party_edit"}

# ── soft refresh schedule ──────────────────────────────────────────────────
#
# A post's score and tags mostly move while it's new; a post from years ago
# has effectively stopped changing. (post_age_days, recheck_every_days) -
# first matching age band wins. Applied against how old the POST is on
# e621, not how long it's been in the local library, so a freshly-uploaded
# clip you tagged last year still gets checked often if the post itself is
# recent.
_REFRESH_SCHEDULE = (
    (30, 3),      # under a month old: recheck every 3 days
    (180, 14),    # under half a year: every 2 weeks
    (365, 60),    # under a year: every 2 months
    (float("inf"), 180),   # older: every 6 months
)


def _parse_iso(text: str) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _refresh_interval_seconds(post_age_seconds: float) -> float:
    age_days = post_age_seconds / 86400
    for max_days, interval_days in _REFRESH_SCHEDULE:
        if age_days < max_days:
            return interval_days * 86400
    return _REFRESH_SCHEDULE[-1][1] * 86400


class E621Meta:
    """
    Sidecar tag database keyed by post ID.

    Records look like {artist:[], character:[], species:[], copyright:[],
    lore:[], rating:"e", score:int, tags:"flat lowercase string", url:...}.
    A post that 404s (or is hidden from anonymous users) is cached as
    {"missing": True} so it is not retried every run; transient network
    errors are NOT cached.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._dirty = False
        try:
            with open(E621_META_PATH, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, ValueError):
            self._data = {}

    def get(self, pid: str) -> dict | None:
        with self._lock:
            return self._data.get(pid)

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            data = dict(self._data)
            self._dirty = False
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = E621_META_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, E621_META_PATH)
        except OSError:
            pass

    def fetch(self, pid: str, user: str = "", key: str = "") -> dict:
        """One API call. Returns the record; {"error": ...} on transient failure."""
        url = E621_API.format(pid=pid)
        if user and key:
            url += ("?login=" + urllib.parse.quote(user) +
                    "&api_key=" + urllib.parse.quote(key))
        request = urllib.request.Request(url, headers={"User-Agent": E621_UA})
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            record = {"missing": True, "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"error": str(exc)}          # transient: do not cache
        else:
            post = payload.get("post") or {}
            tags = post.get("tags") or {}

            def cat(name):
                return list(tags.get(name) or [])

            flat = []
            for group in ("artist", "character", "species", "copyright",
                          "general", "meta", "lore"):
                flat.extend(tags.get(group) or [])
            record = {
                "artist": [a for a in cat("artist") if a not in _ARTIST_NOISE],
                "character": cat("character"),
                "species": cat("species"),
                "copyright": cat("copyright"),
                "lore": cat("lore"),
                "rating": (post.get("rating") or "")[:1],
                "score": (post.get("score") or {}).get("total", 0),
                "tags": " ".join(flat).lower(),
                "url": E621_POST.format(pid=pid),
                "created_at": _parse_iso(post.get("created_at")),
            }
        record["fetched_at"] = time.time()
        with self._lock:
            self._data[pid] = record
            self._dirty = True
        return record

    # ── soft refresh ────────────────────────────────────────────────────

    def is_stale(self, pid: str, now: float | None = None) -> bool:
        """
        True when a successfully-cached post is "due" for a re-check.

        Records that 404'd or are hidden (``missing``) are excluded - that
        flag exists precisely so those are never retried automatically.
        A record cached before this schedule existed (no ``fetched_at``)
        counts as due exactly once, so it picks up real timestamps the
        next time anything asks.
        """
        record = self._data.get(pid)
        if not record or record.get("missing"):
            return False
        fetched_at = record.get("fetched_at")
        if not fetched_at:
            return True
        now = now if now is not None else time.time()
        post_age = max(now - (record.get("created_at") or fetched_at), 0)
        return (now - fetched_at) >= _refresh_interval_seconds(post_age)

    def due_for_refresh(self, pids, budget: int, exclude=()) -> list:
        """
        Up to `budget` stale pids from `pids`, freshest post first (posts
        still gaining votes/tags matter more to keep current than ones
        that settled down years ago).
        """
        if budget <= 0:
            return []
        exclude = set(exclude)
        now = time.time()
        with self._lock:
            candidates = [pid for pid in dict.fromkeys(pids)
                          if pid not in exclude and self.is_stale(pid, now)]

            def sort_key(pid):
                record = self._data.get(pid) or {}
                fetched_at = record.get("fetched_at") or 0
                return now - (record.get("created_at") or fetched_at)

            candidates.sort(key=sort_key)
        return candidates[:budget]
