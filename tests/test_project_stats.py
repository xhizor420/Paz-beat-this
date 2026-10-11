"""A Vault project's overview: totals, quality, and the tags its clips share."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import project_stats  # noqa: E402


def clip(tags, folder="Furry", w=1920, h=1080, fps=30.0, dur=10.0, size=100, artists=()):
    return SimpleNamespace(tags=set(tags), folder=folder, width=w, height=h, fps=fps,
                           duration=dur, size=size, artists=list(artists))


def test_tags_the_clips_share_and_the_ones_that_go_together():
    library = [clip({"animated"}) for _ in range(10)]
    project = [
        clip({"animated", "cum", "cum_on_breasts", "big_breasts"}),
        clip({"animated", "cum", "cum_on_breasts"}),
        clip({"animated", "cum", "cum_on_breasts"}, folder="MLP Futa"),
        clip({"animated", "solo"}, w=3840, h=2160, fps=60.0),
    ]
    share = project_stats.library_share(library + project)
    o = project_stats.overview(project, share, ["Furry", "MLP Futa"])
    assert o.clips == 4 and o.seconds == 40.0
    assert (o.real_60, o.real_4k, o.real_4k60) == (1, 1, 1)
    assert o.categories == [("Furry", 3), ("MLP Futa", 1)]
    tags = dict(o.tags)
    assert "animated" not in tags                 # most of the library has it
    assert tags["cum"] == 3 and tags["cum_on_breasts"] == 3
    assert "solo" not in tags                     # on one clip only
    assert (("cum", "cum_on_breasts"), 3) in o.pairs
    assert project_stats.tag_label("cum_on_breasts") == "cum on breasts"


def test_a_tag_hidden_on_the_overview_leaves_both_lists():
    project = [clip({"anthro_on_human", "cum", "cum_on_breasts"}) for _ in range(3)]
    share = project_stats.library_share(project + [clip({"x"}) for _ in range(20)])
    o = project_stats.overview(project, share, hidden=["anthro_on_human"])
    assert "anthro_on_human" not in dict(o.tags)
    assert all("anthro_on_human" not in pair for pair, _n in o.pairs)
    assert (("cum", "cum_on_breasts"), 3) in o.pairs
