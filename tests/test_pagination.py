"""Paging that stays honest and bounded at depth.

WHAT BREAKS IF THIS IS WRONG
    Every failure here is silent, which is why they all shipped:

      * `total` from an unbounded count_documents — correct, and O(matching
        rows) on every page request of a million-document project.
      * `has_more` inferred as `rows.length < total` — correct only while every
        total is exact, and wrong in BOTH directions the moment one is not: a
        "load more" that returns an empty page, or matches with no way to reach
        them.

    These are the PRIMITIVES. How a given service wires them into a route —
    sort stability, the chunk window content search pages over — is tested in
    that service's own suite, against its own routes.
"""

from __future__ import annotations

import mongomock

from apex_common import pagination as pg


# ===========================================================================
# The primitives
# ===========================================================================


def test_the_probe_fetches_exactly_one_extra_row():
    """One row, not a doubled page: the probe is the cheapest possible answer."""
    assert pg.probe_limit(50) == 51
    assert pg.probe_limit(1) == 2
    assert pg.probe_limit(0) == 2        # a nonsense limit still yields a probe


def test_the_extra_row_means_more_and_never_reaches_the_client():
    page, has_more = pg.split_probe(list(range(51)), 50)
    assert has_more is True
    assert len(page) == 50, "the probe row must be trimmed off"


def test_a_short_page_means_this_is_the_last_one():
    page, has_more = pg.split_probe([1, 2, 3], 50)
    assert has_more is False and page == [1, 2, 3]


def test_a_full_page_with_no_probe_row_is_the_last_one():
    """Exactly `limit` rows back means the probe found nothing beyond them."""
    page, has_more = pg.split_probe(list(range(50)), 50)
    assert has_more is False and len(page) == 50


def test_a_count_below_its_cap_is_exact():
    total, exact = pg.bounded_count(4_312, cap=10_000)
    assert (total, exact) == (4_312, True)


def test_a_count_that_reached_its_cap_is_a_floor_not_a_total():
    """This is the whole reason for the flag: a capped count may have stopped
    early, and rendering it as exact is how a UI claims "10,000 documents" for
    a corpus of four million."""
    total, exact = pg.bounded_count(10_000, cap=10_000)
    assert (total, exact) == (10_000, False)


def test_the_count_query_is_actually_capped():
    """The cap has to reach Mongo. A cap applied in Python after the fact is
    the expensive version of the same number."""
    col = mongomock.MongoClient()["db"]["c"]
    col.insert_many([{"p": "x"} for _ in range(25)])
    total, exact = pg.count_capped_sync(col, {"p": "x"}, cap=10)
    assert total == 10 and exact is False


def test_page_meta_prefers_a_measured_has_more_over_the_count():
    """When both are available the measurement wins — the count can be a floor."""
    meta = pg.page_meta(rows=[1] * 50, limit=50, skip=0, total=50,
                        total_is_exact=False, has_more=True)
    assert meta.has_more is True


def test_page_meta_never_reports_fewer_than_it_has_shown():
    """A total smaller than skip + page length is self-contradictory."""
    meta = pg.page_meta(rows=[1] * 10, limit=10, skip=100, total=5)
    assert meta.total >= 110


def test_distinct_in_order_keeps_best_first():
    results = [("b", 1), ("a", 2), ("b", 3), ("c", 4)]
    assert pg.distinct_in_order(results, key=lambda r: r[0]) == ["b", "a", "c"]


# ===========================================================================
# CROSS-SERVICE CONTRACT
# ===========================================================================
#
# One admin screen renders lists from every service, so these field names have
# to agree. They were duplicated per repo before this package existed; the
# services now keep only a repo guard that they define no paging of their own.


def test_the_paging_field_names_are_the_agreed_ones():
    assert set(pg.PageMeta().model_dump()) == {
        "total", "total_is_exact", "has_more", "limit", "skip",
    }


def test_an_empty_page_claims_nothing():
    """The default has to be the safe one: no rows, no total, no next page."""
    meta = pg.PageMeta()
    assert (meta.total, meta.has_more) == (0, False)
    assert meta.total_is_exact is True
