"""Ranking clips by how much they have in common with one you picked.

A library of ten thousand clips is not browsed, it is searched - and the
best search term is usually a clip you already like. "Like this" answers
"what else have I got that is this sort of thing", which is the question
a media bin exists for and the one the search box is worst at: you would
have to know which of a clip's forty tags are the ones that matter.

Which tags matter is exactly what this works out. A tag on eight
thousand clips says nothing about any of them; a tag on twelve says a
great deal. So every tag is weighted by how rare it is in YOUR library
(the usual inverse document frequency), and the ones that name something
- artist, character, species, copyright - count for more, because two
clips by the same artist of the same character are alike in a way that
two clips sharing "outdoors" are not.

Scores are divided by the square root of how many tags a clip carries,
so a heavily tagged post cannot out-rank a well-matched one simply by
having more chances to overlap.
"""

from __future__ import annotations

import math
from collections import Counter
from itertools import chain

# A tag on more than this share of the library is descriptive of the
# library, not of a clip: "anthro" in a furry collection, "sound" in one
# that has been filtered for it. They cost time and say nothing.
COMMON_SHARE = 0.4

# What a tag that names someone or something is worth against a plain
# descriptive one. Same artist and same character is the strongest signal
# a tag list carries.
NAMED_WEIGHT = 3.0


def tag_weights(records) -> dict:
    """tag -> how much sharing it is worth, for this library.

    Built once per library load: it depends on the whole collection, not
    on any one clip, and computing it per search would be the expensive
    part of a feature that has to feel instant.
    """
    # Counter over one chain of every clip's tags: the counting loop is
    # C, where a dict.get per tag in Python was most of a hundred
    # milliseconds on a ten-thousand-clip library.
    tagged = [tags for tags in (getattr(rec, "tags", None) for rec in records) if tags]
    total = len(tagged)
    if not total:
        return {}
    counts = Counter(chain.from_iterable(tagged))
    ceiling = total * COMMON_SHARE
    weights = {}
    for tag, count in counts.items():
        if count > ceiling:
            continue
        # +1 so a tag on a single clip has a finite, not infinite, weight.
        weights[tag] = math.log(total / (count + 1)) + 1.0
    return weights


def score(rec, target_tags: set, target_named: frozenset, weights: dict) -> float:
    """How much this clip has in common with the one picked."""
    tags = getattr(rec, "tags", None)
    if not tags or not target_tags:
        return 0.0
    shared = tags & target_tags
    if not shared:
        return 0.0
    named = getattr(rec, "named", frozenset())
    total = 0.0
    for tag in shared:
        weight = weights.get(tag)
        if weight is None:
            continue
        if tag in target_named and tag in named:
            weight *= NAMED_WEIGHT
        total += weight
    return total / math.sqrt(len(tags))


def rank(records, target, weights: dict) -> list:
    """`records`, most like `target` first.

    The target itself sorts to the front - it is the reference you are
    reading the rest against, and a row of results that does not include
    the thing you asked about is disorienting.
    """
    if target is None:
        return list(records)
    target_tags = getattr(target, "tags", None) or set()
    target_named = getattr(target, "named", frozenset())
    if not target_tags:
        return list(records)

    # Rec caches its own lowercased name, but everything else in this
    # module reads its records through getattr so it can be handed any
    # record-shaped object. Keep that here too.
    def tie(rec) -> str:
        return getattr(rec, "sort_name", "") or rec.name.lower()

    # Only a clip that shares a tag with the target can score at all, and
    # on a real library that is a small part of it. Scoring and sorting
    # the whole library on a (-score, name) key was a hundred and forty
    # milliseconds at worst; the clips that score nothing are simply put
    # after the ones that do, in name order - which is exactly where the
    # full sort put them, since every one of them scores 0.
    front: list = []
    scored: list = []
    rest: list = []
    for rec in records:
        if rec.path == target.path:
            front.append(rec)
            continue
        tags = getattr(rec, "tags", None)
        value = (score(rec, target_tags, target_named, weights)
                 if tags and not tags.isdisjoint(target_tags) else 0.0)
        if value > 0.0:
            scored.append((-value, tie(rec), rec))
        else:
            rest.append(rec)
    front.sort(key=tie)
    # Sorted on the score and the name only, never the record itself -
    # two clips with the same score and name keep the order they came in,
    # as the one-key sort did.
    scored.sort(key=lambda row: (row[0], row[1]))
    rest.sort(key=tie)
    return front + [row[2] for row in scored] + rest
