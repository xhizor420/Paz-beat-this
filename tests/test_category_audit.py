"""Which category a clip belongs in, by its e621 tags.

Grabber files a post under whichever search found it, so the same post
can sit in two category folders, or in the wrong one. Convert uses these
rules instead: MLP Futa, MLP, Furry Futa, Furry, in that order.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import category_audit as ca                     # noqa: E402


def rec(tags="", copyright=()):
    return {"tags": tags, "copyright": list(copyright)}


# ── the rules ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("tags, copyright, folder", [
    ("gynomorph female", (), "Furry Futa"),
    ("herm male", (), "Furry Futa"),
    ("intersex", (), "Furry Futa"),
    ("femboy male", (), "Furry"),              # femboys are male
    ("crossgender female", (), "Furry"),
    ("human", (), "Furry"),                     # human only: a missed tag
    ("gynomorph", ("my_little_pony",), "MLP Futa"),
    ("female", ("my_little_pony",), "MLP"),
    ("female equine", (), "Furry"),             # a horse is not a pony
])
def test_the_rules(tags, copyright, folder):
    assert ca.category(rec(tags, copyright)) == folder


def test_no_record_or_a_gone_post_has_no_category():
    assert ca.category(None) == ""
    assert ca.category({"missing": True}) == ""


def test_route_follows_the_tags_and_only_into_your_folders():
    futa = rec("gynomorph")
    assert ca.route("Furry", "1", futa, ca.CATEGORIES) == "Furry Futa"
    assert ca.route("Furry", "1", futa, ["Furry"]) == "Furry"        # not one of yours
    assert ca.route("Furry", "1", None, ca.CATEGORIES) == "Furry"    # untagged stays
    assert ca.route("Other", "1", futa, ca.CATEGORIES) == "Other"    # not a category
    assert ca.route("Furry", "1", futa, ca.CATEGORIES, {"1": "Furry"}) == "Furry"
