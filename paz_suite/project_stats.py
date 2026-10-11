"""What a Vault project is made of: totals, quality, categories, and the
tags its clips share - which ones, and which ones come together.

Pure counting over the library's records, no UI, so it can be checked on
its own. A tag most of the library carries (animated, hi_res, 3d) says
nothing about a project, so tags past COMMON_SHARE of the whole library
are left out of the lists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations

# A tag on more than this share of the whole library is too common to
# say anything about one project.
COMMON_SHARE = 0.35
TOP_TAGS = 24
TOP_PAIRS = 14
# Pairs are looked for among this many of the project's top tags - enough
# to find the real combinations, few enough that a clip with eighty tags
# is not three thousand pairs.
PAIR_POOL = 40


@dataclass
class Overview:
    clips: int = 0
    seconds: float = 0.0
    size: int = 0
    real_60: int = 0
    real_4k: int = 0
    real_4k60: int = 0
    categories: list = field(default_factory=list)   # [(folder, count)]
    tags: list = field(default_factory=list)         # [(tag, count)]
    pairs: list = field(default_factory=list)        # [((a, b), count)]
    artists: list = field(default_factory=list)      # [(name, count)]


def library_share(records) -> dict:
    """Tag -> share of the whole library that carries it (0..1)."""
    counts: Counter = Counter()
    total = 0
    for rec in records:
        tags = getattr(rec, "tags", None)
        if not tags:
            continue
        total += 1
        counts.update(tags)
    if not total:
        return {}
    return {tag: n / total for tag, n in counts.items()}


def overview(clips, share: dict, folders=(), min_height: int = 2160,
             min_fps: float = 60.0, hidden=()) -> Overview:
    """`hidden` are tags left out of the tag and pair lists by hand."""
    out = Overview(clips=len(clips))
    hidden = set(hidden)
    by_folder: Counter = Counter()
    tag_counts: Counter = Counter()
    artists: Counter = Counter()
    for rec in clips:
        out.seconds += float(rec.duration or 0)
        out.size += int(rec.size or 0)
        width, height = int(rec.width or 0), int(rec.height or 0)
        short = min(width, height) if width and height else height
        fast = bool(rec.fps) and rec.fps >= min_fps
        big = short >= min_height
        out.real_60 += fast
        out.real_4k += big
        out.real_4k60 += fast and big
        by_folder[rec.folder or ""] += 1
        artists.update(getattr(rec, "artists", None) or ())
        # Years ("2025") are when it was posted, not what is in it.
        tag_counts.update(tag for tag in (getattr(rec, "tags", None) or ())
                          if share.get(tag, 0.0) <= COMMON_SHARE
                          and not tag.isdigit() and tag not in hidden)
    order = {name: i for i, name in enumerate(folders)}
    out.categories = sorted(by_folder.items(),
                            key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
    shared = [(tag, n) for tag, n in tag_counts.most_common() if n >= 2]
    out.tags = shared[:TOP_TAGS]
    out.artists = [(name, n) for name, n in artists.most_common(8) if n >= 2]

    pool = {tag for tag, _n in shared[:PAIR_POOL]}
    pair_counts: Counter = Counter()
    for rec in clips:
        mine = sorted(pool.intersection(getattr(rec, "tags", None) or ()))
        pair_counts.update(combinations(mine, 2))
    out.pairs = [(pair, n) for pair, n in pair_counts.most_common(TOP_PAIRS) if n >= 2]
    return out


def tag_label(tag: str) -> str:
    """cum_on_breasts -> cum on breasts."""
    return tag.replace("_", " ")
