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

from .config import CONFIG_DIR, E621_META_LOG, E621_META_PATH

APP_NAME = "PAZ Suite"
# Still finding its edges - Resolve, the beat models and the
# 4K paths all landed recently. 1.0 when those have been lived
# with for a while.
APP_VERSION = "0.8 beta"

E621_API = "https://e621.net/posts/{pid}.json"
E621_POST = "https://e621.net/posts/{pid}"
E621_UA = f"{APP_NAME}/{APP_VERSION} (personal library tagger)"

# The only answers that mean "this post is not coming back": it does not
# exist, or it has been deleted. Those are worth remembering, so the post
# is never asked about again. Every other HTTP status is about the
# request rather than the post - see fetch().
GONE_FOR_GOOD = frozenset({404, 410})

# How many failures in an unbroken run mean a fetch run should stop. One
# post can fail on its own; this many cannot, and at a second apiece a
# ten-thousand-post library would spend three hours finding that out.
# Nothing is cached from a failed fetch, so the posts not reached are
# simply first in the queue next time.
GIVE_UP_AFTER = 8

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
        self._pending: set = set()
        try:
            with open(E621_META_PATH, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, ValueError):
            self._data = {}
        if self._replay_log():
            # A previous run was interrupted mid-fetch. Fold its journal
            # back into the cache and start clean - once, at startup,
            # where a hundred milliseconds does not matter.
            self._dirty = True
            self.save()

    def get(self, pid: str) -> dict | None:
        with self._lock:
            return self._data.get(pid)

    def save(self) -> None:
        """Rewrite the whole cache. Use at shutdown and at the end of a
        fetch - see checkpoint() for the one to call during it."""
        with self._lock:
            if not self._dirty:
                return
            data = dict(self._data)
            self._dirty = False
            self._pending.clear()
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = E621_META_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, E621_META_PATH)
        except OSError:
            return
        try:
            os.remove(E621_META_LOG)
        except OSError:
            pass

    def checkpoint(self) -> None:
        """Put the posts fetched since the last checkpoint somewhere safe.

        Tagging a whole library is thousands of posts over hours, and it
        has to survive being interrupted - but a full rewrite to bank ten
        new posts means writing the other ten thousand as well. On this
        library that is seven and a half megabytes and an eighth of a
        second, over a thousand times in one run: eight gigabytes of
        writes and two minutes of CPU to save work that would fit in a
        few kilobytes. So the checkpoint appends the new records to a
        journal instead, and the next full save folds them in.
        """
        with self._lock:
            rows = [(pid, self._data[pid]) for pid in sorted(self._pending)
                    if pid in self._data]
            self._pending.clear()
        if not rows:
            return
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(E621_META_LOG, "a", encoding="utf-8") as fh:
                for pid, record in rows:
                    fh.write(json.dumps({"pid": pid, "rec": record}) + "\n")
        except OSError:
            pass

    def _replay_log(self) -> int:
        """Apply a journal left by an interrupted run. Returns how many."""
        count = 0
        try:
            with open(E621_META_LOG, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue        # a half-written last line
                    pid = row.get("pid")
                    if isinstance(pid, str) and isinstance(row.get("rec"), dict):
                        self._data[pid] = row["rec"]
                        count += 1
        except OSError:
            return 0
        return count

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
            if exc.code not in GONE_FOR_GOOD:
                # Everything else is about the request, not the post. A
                # 429 is the rate limit, a 503 is e621 or its front end
                # having a moment, a 401 is a key that has expired, a 500
                # is their problem. Caching any of those as `missing` was
                # permanent: `missing` is the one flag is_stale() will
                # never re-check and _fetch_tags only queues posts with
                # no record at all, so a post marked that way is never
                # asked about again. One rate-limited run through a
                # ten-thousand-clip library would have silently condemned
                # every post it touched to stay untagged for good.
                return {"error": f"HTTP {exc.code}"}
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
            self._pending.add(pid)
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
