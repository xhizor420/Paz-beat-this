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
        self._data: dict = {}
        # Where this cache lives, settled here rather than read from the
        # module every time. The read and the writes happen on another
        # thread, and a thread that looks up a module global does it at
        # whatever moment it gets there - which is not the moment the
        # object was made. That is how a test's cache came to write
        # itself over the real one: the test had moved the globals,
        # the thread wrote after the test put them back. An object's file
        # should not be able to change underneath it.
        self._path = E621_META_PATH
        self._log = E621_META_LOG
        self._dir = CONFIG_DIR
        # Read on a thread, not here. On this library the cache is 7.5MB
        # of JSON and parsing it is 283ms - and this object is built
        # before the window exists, so that was 283ms of nothing on
        # screen at all, growing with the library. Everything that reads
        # the cache waits for it below, and the thing that asks first is
        # the library load, which is already on a worker: so the wait
        # lands there, off the UI thread, overlapping the widget build
        # rather than preceding it.
        #
        # Never worse than doing it here. The old code always stalled the
        # UI thread for the whole parse; this one only stalls a caller
        # that asks before the parse is done, and the one early caller is
        # not the UI thread.
        self._ready = threading.Event()
        threading.Thread(target=self._read_cache, daemon=True).start()

    def _read_cache(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            # Merged, not assigned. fetch() deliberately does not wait
            # for this read - it is a network call, and blocking one on a
            # disk parse would be backwards - so a record could already
            # be in memory by the time the file arrives. What was just
            # fetched is newer than what is on disk, so it wins.
            data.update(self._data)
            self._data = data
        if self._replay_log():
            # A previous run was interrupted mid-fetch. Fold its journal
            # back into the cache and start clean. Done before anyone is
            # let in, so nobody ever sees the cache without the journal
            # already folded into it - and it only happens after a run
            # that was cut short, not on an ordinary launch.
            self._dirty = True
            self._write_cache()
        self._ready.set()

    def wait_until_read(self, timeout: float = 60.0) -> bool:
        """Block until the cache is in memory. True if it got there.

        Public because the Library's loader wants to wait deliberately,
        on its own thread, rather than discover the wait inside a lookup.
        """
        return self._ready.wait(timeout)

    def get(self, pid: str) -> dict | None:
        self._ready.wait(60.0)
        with self._lock:
            return self._data.get(pid)

    def snapshot(self) -> dict:
        """Every cached record, as one dict, taken under the lock once.

        For the callers that ask about the whole library at once - a load
        looks this cache up twice per clip, and on ten thousand clips
        that is twenty thousand lock acquisitions for twenty thousand
        dictionary lookups. Measured at 78ms of a 350ms library load,
        which is more than the load spends reading the database.

        A snapshot is also the more honest thing for those callers to
        work from: a load that asked twenty thousand separate questions
        could be answered from two different versions of the cache if a
        tag fetch happened to land in the middle of it.
        """
        self._ready.wait(60.0)
        with self._lock:
            return dict(self._data)

    def save(self) -> None:
        """Rewrite the whole cache. Use at shutdown and at the end of a
        fetch - see checkpoint() for the one to call during it.

        Waits for the read first: writing what is in memory before the
        file has been read into it would put an empty cache over ten
        thousand posts.
        """
        self._ready.wait(60.0)
        self._write_cache()

    def _write_cache(self) -> None:
        """The write itself, without waiting for the read. Called by
        save(), which waits, and by the reader thread once it has folded
        in a journal - where waiting would be waiting on itself."""
        with self._lock:
            if not self._dirty:
                return
            data = dict(self._data)
            self._dirty = False
            self._pending.clear()
        try:
            os.makedirs(self._dir, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._path)
        except OSError:
            return
        try:
            os.remove(self._log)
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
            os.makedirs(self._dir, exist_ok=True)
            with open(self._log, "a", encoding="utf-8") as fh:
                for pid, record in rows:
                    fh.write(json.dumps({"pid": pid, "rec": record}) + "\n")
        except OSError:
            pass

    def _replay_log(self) -> int:
        """Apply a journal left by an interrupted run. Returns how many."""
        count = 0
        try:
            with open(self._log, "r", encoding="utf-8") as fh:
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

    def due_for_refresh(self, pids, budget: int, exclude=(),
                        prefer=()) -> list:
        """
        Up to `budget` stale pids from `pids`, most worth re-checking
        first.

        Three things decide the order, and they are not the same
        question as "is it due", which is_stale already answered:

          * whether you have actually used the clip. A post on a clip
            that has been in an edit is one you will reach for again,
            and its tags and score are what you would find it by. The
            caller knows which those are - see `prefer`.
          * whether it has never come back with anything. A record with
            no tags at all is either a post that gained them since, or
            one whose tags were lost to a bad fetch; either way there is
            more to gain from asking again than from re-checking a post
            that already has forty.
          * how new the post is. A post still gaining votes and tags
            moves; one that settled down years ago does not.

        In that order, because the first two are about whether an answer
        is worth having and the third is only about how likely it is to
        have changed.
        """
        if budget <= 0:
            return []
        self._ready.wait(60.0)
        exclude = set(exclude)
        wanted = set(prefer)
        now = time.time()
        with self._lock:
            candidates = [pid for pid in dict.fromkeys(pids)
                          if pid not in exclude and self.is_stale(pid, now)]

            def sort_key(pid):
                record = self._data.get(pid) or {}
                fetched_at = record.get("fetched_at") or 0
                age = now - (record.get("created_at") or fetched_at)
                return (0 if pid in wanted else 1,
                        0 if not record.get("tags") else 1,
                        age)

            candidates.sort(key=sort_key)
        return candidates[:budget]
