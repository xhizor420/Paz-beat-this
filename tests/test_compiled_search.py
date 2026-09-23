"""The search box answers exactly as it did before it got faster.

A search used to decide, for every clip and every term, what kind of term
it was and whether it had a wildcard - ten thousand times per keystroke -
and then look for a word in each of a clip's forty tags one at a time in
Python. It now works each term out once (library_db.term_test) and looks
for a word in one string holding every tag.

Faster is worthless if it finds different clips. So the matcher it
replaced is kept here word for word, and the two are run side by side
over thousands of random clips and queries.
"""

from __future__ import annotations

import fnmatch
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_db import Rec, compile_query, parse_query   # noqa: E402


# ── the matcher as it was, verbatim ─────────────────────────────────────

def old_term_hits(rec, kind, value):
    if kind == "is":
        if value in ("untagged", "notags"):
            return not rec.tags
        if value == "tagged":
            return bool(rec.tags)
        if value in ("noid", "unknown"):
            return not rec.pid
        if value == "silent":
            return rec.duration <= 0
        if value in ("4k", "premium"):
            return rec.premium
        if value in ("no4k", "sd"):
            return not rec.premium
        if value in ("portrait", "phone", "vertical"):
            return rec.orientation == "portrait"
        if value in ("widescreen", "landscape", "horizontal"):
            return rec.orientation == "widescreen"
        if value == "square":
            return rec.orientation == "square"
        if value in ("unused", "fresh", "new"):
            return not rec.used_projects
        if value == "used":
            return bool(rec.used_projects)
        return False
    if kind == "artist":
        return any(value == a or fnmatch.fnmatch(a, value) for a in rec.artists)
    if kind == "character":
        return any(value == c or fnmatch.fnmatch(c, value) for c in rec.characters)
    if kind == "species":
        return any(value == s or fnmatch.fnmatch(s, value) for s in rec.species)
    if kind in ("copyright", "series"):
        return any(value == c or fnmatch.fnmatch(c, value) for c in rec.copyrights)
    if kind == "lore":
        return any(value == item or fnmatch.fnmatch(item, value)
                   for item in rec.lore)
    if kind == "rating":
        return rec.rating == value[:1]
    if kind == "folder":
        return value in rec.folder.lower()
    if kind == "id":
        return rec.pid == value
    if kind == "used":
        if value in ("", "any"):
            return bool(rec.used_projects)
        return any(value == p.lower() for p in rec.used_projects)
    if "*" in value:
        return any(fnmatch.fnmatch(t, value) for t in rec.tags)
    if value in rec.tags:
        return True
    if value in rec.sort_name or value in rec.pid:
        return True
    return any(value in t for t in rec.tags)


def old_matches(rec, includes, excludes):
    for kind, value in includes:
        if not old_term_hits(rec, kind, value):
            return False
    for kind, value in excludes:
        if old_term_hits(rec, kind, value):
            return False
    return True


# ── random clips and queries ────────────────────────────────────────────

WORDS = ["wolf", "fox", "male", "female", "solo", "duo", "kenket", "zonkpunch",
         "canine", "vulpine", "blue_eyes", "sfw", "animated", "sound", "4k",
         "tag[1]", "why?", "a*b", "wolf_o'donnell", "pmv"]


def make_rec(rng, n):
    width, height = rng.choice([(1920, 1080), (1080, 1920), (1000, 1000), (0, 0)])
    rec = Rec(path=f"/lib/{n}.mp4", name=f"{rng.choice(['', 'Wolf ', 'fox_'])}{n}",
              folder=rng.choice(["Wolves", "misc", "PMV parts"]),
              pid=rng.choice(["", str(4000000 + n)]), size=n, mtime=n,
              duration=rng.choice([0.0, 12.5, 300.0]), width=width, height=height,
              fps=30.0)
    if rng.random() < 0.85:
        rec.artists = rng.sample(["kenket", "zonkpunch", "tag[1]", "why?"], rng.randint(0, 2))
        rec.characters = rng.sample(["wolf_o'donnell", "krystal", "fox_mccloud"], rng.randint(0, 2))
        rec.species = rng.sample(["canine", "vulpine", "wolf"], rng.randint(0, 2))
        rec.copyrights = rng.sample(["star_fox", "pokemon"], rng.randint(0, 1))
        rec.lore = rng.sample(["male/male", "canon"], rng.randint(0, 1))
        rec.rating = rng.choice(["s", "q", "e", ""])
        rec.tags = set(rng.sample(WORDS, rng.randint(0, 10))) | set(rec.artists) \
            | set(rec.characters) | set(rec.species)
    rec.premium = rng.random() < 0.3
    rec.used_projects = rng.sample(["Summer PMV", "wolfpack"], rng.randint(0, 2))
    rec.compute_named()
    return rec


TERMS = ["wolf", "fox", "ol", "40000", "wolf_", "tag[1]", "why?", "a*b", "w*f",
         "*fox*", "k?nket", "artist:kenket", "artist:k*", "artist:tag[1]",
         "artist:why?", "character:*mccloud", "species:wolf", "series:star_fox",
         "copyright:poke*", "lore:male/male", "rating:e", "rating:explicit",
         "folder:wolves", "folder:pmv", "id:4000007", "used:any", "used:",
         'used:"summer pmv"', "used:wolfpack", "is:untagged", "is:tagged",
         "is:noid", "is:silent", "is:4k", "is:sd", "is:portrait", "is:phone",
         "is:widescreen", "is:square", "is:unused", "is:used", "is:nonsense",
         "e621", "", "-"]


def queries(rng, count):
    for _ in range(count):
        picked = rng.sample(TERMS, rng.randint(1, 3))
        yield " ".join(("-" + t if t and rng.random() < 0.3 else t) for t in picked)


@pytest.fixture(scope="module")
def library():
    rng = random.Random(621)
    return [make_rec(rng, n) for n in range(600)]


def test_every_term_alone_finds_the_same_clips(library):
    for term in TERMS:
        for text in (term, "-" + term):
            includes, excludes = parse_query(text)
            test = compile_query(includes, excludes)
            new = [r.path for r in library if test(r)]
            old = [r.path for r in library if old_matches(r, includes, excludes)]
            assert new == old, f"{text!r} found different clips"


def test_random_queries_find_the_same_clips(library):
    rng = random.Random(1)
    for text in queries(rng, 1500):
        includes, excludes = parse_query(text)
        test = compile_query(includes, excludes)
        new = [r.path for r in library if test(r)]
        old = [r.path for r in library if old_matches(r, includes, excludes)]
        assert new == old, f"{text!r} found different clips"


def test_the_joined_tags_follow_the_tags():
    """tag_text is derived, so it has to be rebuilt whenever the tags
    are - which is what compute_named is for."""
    rec = Rec(path="/a.mp4", name="a", folder="", pid="1", size=1, mtime=1,
              duration=1.0, width=1, height=1, fps=1.0)
    rec.tags = {"wolf"}
    rec.compute_named()
    includes, excludes = parse_query("wol")
    assert compile_query(includes, excludes)(rec)
    rec.tags = {"fox"}
    rec.compute_named()
    assert not compile_query(includes, excludes)(rec)


def test_a_word_cannot_match_across_two_tags():
    """The tags are joined with newlines; a term never holds one from the
    search box, but one handed in directly must not bridge two tags."""
    from paz_suite.library_db import term_test
    rec = Rec(path="/a.mp4", name="a", folder="", pid="", size=1, mtime=1,
              duration=1.0, width=1, height=1, fps=1.0)
    rec.tags = {"wolf", "fox"}
    rec.compute_named()
    for value in ("wolffox", "foxwolf", "wolf\nfox", "fox\nwolf", "f\nw"):
        assert not term_test("tag", value)(rec), repr(value)
    assert term_test("tag", "olf")(rec)
