"""'Like this' - ranking the library against a clip you picked.

The whole value is in which tags count. A tag on most of the library
says nothing about any one clip; a tag on a handful says a great deal;
and sharing an artist and a character is a different kind of alike from
sharing "outdoors". If that weighting is wrong the feature is a shuffle
button with a confusing name.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import similar      # noqa: E402


class Rec:
    def __init__(self, name, tags, named=()):
        self.name = self.path = name
        self.tags = set(tags)
        self.named = frozenset(named)


def library():
    # "anthro" is on everything; "canine" on most; the rest are rare.
    common = ["anthro", "canine"]
    recs = [Rec(f"filler{i}.mp4", common + [f"misc{i}"]) for i in range(20)]
    recs += [
        Rec("target.mp4", common + ["lewdchord", "digi", "neon", "bedroom"],
            named=["lewdchord", "digi"]),
        Rec("same_artist.mp4", common + ["lewdchord", "digi", "kitchen"],
            named=["lewdchord", "digi"]),
        Rec("same_scene.mp4", common + ["neon", "bedroom", "someoneelse"],
            named=["someoneelse"]),
        Rec("only_common.mp4", common + ["misc99"]),
        Rec("nothing.mp4", ["unrelated1", "unrelated2"]),
    ]
    return recs


def ranked_names():
    recs = library()
    weights = similar.tag_weights(recs)
    target = next(r for r in recs if r.name == "target.mp4")
    return [r.name for r in similar.rank(recs, target, weights)], recs, weights, target


def test_the_clip_you_picked_leads_the_results():
    names, *_ = ranked_names()
    assert names[0] == "target.mp4"


def test_the_same_artist_and_character_ranks_above_a_shared_setting():
    names, *_ = ranked_names()
    assert names.index("same_artist.mp4") < names.index("same_scene.mp4")


def test_sharing_only_what_everything_shares_ranks_below_both():
    names, *_ = ranked_names()
    assert names.index("only_common.mp4") > names.index("same_scene.mp4")


def test_a_clip_with_nothing_in_common_scores_zero():
    _names, recs, weights, target = ranked_names()
    nothing = next(r for r in recs if r.name == "nothing.mp4")
    assert similar.score(nothing, target.tags, target.named, weights) == 0.0


def test_a_tag_on_most_of_the_library_is_not_worth_anything():
    """"anthro" in a furry collection describes the collection."""
    _names, _recs, weights, _target = ranked_names()
    assert "anthro" not in weights
    assert "neon" in weights


def test_a_rarer_tag_is_worth_more_than_a_commoner_one():
    recs = [Rec(f"r{i}.mp4", ["everywhere"] + (["rare"] if i < 2 else [])) for i in range(30)]
    weights = similar.tag_weights(recs)
    assert weights["rare"] > weights.get("everywhere", 0)


def test_a_tag_stuffed_clip_cannot_win_on_volume_alone():
    """Fifty tags means fifty chances to overlap; the score is divided by
    the square root of the count so breadth is not depth."""
    target = Rec("t.mp4", ["a", "b", "c"])
    tight = Rec("tight.mp4", ["a", "b", "c"])
    stuffed = Rec("stuffed.mp4", ["a", "b", "c"] + [f"pad{i}" for i in range(50)])
    recs = [target, tight, stuffed] + [Rec(f"f{i}.mp4", [f"x{i}"]) for i in range(20)]
    weights = similar.tag_weights(recs)
    assert (similar.score(tight, target.tags, target.named, weights)
            > similar.score(stuffed, target.tags, target.named, weights))


def test_an_untagged_target_leaves_the_order_alone():
    recs = library()
    weights = similar.tag_weights(recs)
    blank = Rec("blank.mp4", [])
    assert [r.name for r in similar.rank(recs, blank, weights)] == [r.name for r in recs]


def test_no_target_leaves_the_order_alone():
    recs = library()
    assert similar.rank(recs, None, {}) == list(recs)


def test_an_empty_library_has_no_weights():
    assert similar.tag_weights([]) == {}
    assert similar.tag_weights([Rec("a.mp4", [])]) == {}
