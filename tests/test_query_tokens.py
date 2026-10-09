"""The chips under the search box show the terms the search actually used.

They were cut with a plain split(), so a project search -
used:"ENEMY AND REVENGE 2" - came out as four chips, and the ✕ on "AND"
deleted a word from the middle of the project's name.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite.library_db import parse_query, query_tokens   # noqa: E402


def test_a_quoted_project_is_one_term():
    assert query_tokens('used:"ENEMY AND REVENGE 2"') == ['used:"ENEMY AND REVENGE 2"']


def test_quoted_and_plain_terms_side_by_side():
    assert query_tokens('fox used:"summer pmv" -solo') == \
        ["fox", 'used:"summer pmv"', "-solo"]


def test_an_excluded_quoted_term_keeps_its_minus():
    assert query_tokens('-used:"a b"') == ['-used:"a b"']


def test_extra_spaces_make_no_empty_terms():
    assert query_tokens("  fox   wolf ") == ["fox", "wolf"]


def test_an_unclosed_quote_runs_to_the_end():
    assert query_tokens('fox used:"half typed') == ["fox", 'used:"half typed']


@pytest.mark.parametrize("query", [
    'fox', 'fox wolf -solo', 'used:"ENEMY AND REVENGE 2"',
    'artist:x used:"summer pmv" -rating:s', '-used:"a b" c',
])
def test_it_agrees_with_the_search_about_where_terms_end(query):
    includes, excludes = parse_query(query)
    assert len(query_tokens(query)) == len(includes) + len(excludes)
