"""Paging that stays honest — and stays cheap — at depth.

WHY THIS EXISTS
    Every list endpoint here was written the same way:

        total = coll.count_documents(query)
        rows  = coll.find(query).skip(skip).limit(limit)

    Both halves break on a corpus of millions, and they break QUIETLY:

    1. `count_documents` with no bound is O(matching rows). On an unfiltered
       project listing that is the whole project, on EVERY page request, to
       render a number nobody acts on. The index makes the page fast and does
       nothing for the count.

    2. `skip(n)` makes the server walk and discard n rows. Page 1 is free,
       page 400 reads 20,000 rows to return 50.

    3. The client decided whether a "load more" button existed by comparing
       `rows.length < total`. That is only correct when `total` is exact — and
       the moment one endpoint's total became an estimate (content search),
       the button lied in both directions: offering a page that came back
       empty, or hiding matches that were there.

WHAT THIS MODULE SETTLES
    `has_more` is MEASURED, never inferred from a count. Ask for one row more
    than the page; if it arrives, there is a next page. That answer is exact
    even when the total is not, and it costs one row.

    `total` becomes a *bounded* count: counted up to a cap, with a flag saying
    whether the cap was hit. Past the cap the honest rendering is "10,000+",
    which is what a user can actually act on — and the count stops being
    unbounded work.

    Three fields travel together on every list response, and `PageMeta` is the
    one definition of them:

        total           how many match (exact, or the cap)
        total_is_exact  False means "at least `total`" — render "N+"
        has_more        is there a page after this one

THE FIELD NAMES MUST AGREE ACROSS EVERY SERVICE
    One admin screen renders lists from all three, so `total_is_exact` has to
    mean the same thing in each.

THIS IS THE SINGLE HOME
    Until 2026-11-03 this file was copied into each service and held in place by
    a hand-written identical fixture table per repo, because the services shared
    no library. They now share this one. Each service keeps a REPO GUARD
    asserting it defines nothing here itself — re-adding a local copy is the
    failure this package exists to prevent.

THE RULE FOR CALLERS
    Never compute `has_more` on the client. Never print `total` without
    checking `total_is_exact`. Both mistakes were already shipped here once.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

# Counting is capped rather than unbounded. 10,000 is chosen because it is far
# past the point where a human reads the number and far short of the point where
# counting costs real time: the aggregation stops as soon as it has that many.
DEFAULT_COUNT_CAP = 10_000


class PageMeta(BaseModel):
    """The paging contract, identical on every list response in the service."""

    total: int = 0
    # False means `total` is a floor, not a count: "at least this many".
    # Set when a bounded count hit its cap, or when the total is inherently an
    # estimate (ranked search over chunks collapsed to distinct documents).
    total_is_exact: bool = True
    # Measured by over-fetching one row, never derived from `total`.
    has_more: bool = False
    limit: int = 0
    skip: int = 0


def probe_limit(limit: int) -> int:
    """How many rows to actually fetch for a page of `limit`.

    One more than asked. The extra row is the whole mechanism: its presence is
    the answer to "is there a next page", and it never reaches the client.
    """
    return max(1, limit) + 1


def split_probe(rows: Sequence[T], limit: int) -> tuple[list[T], bool]:
    """Trim an over-fetched page back to size and report whether more exist."""
    limit = max(1, limit)
    return list(rows[:limit]), len(rows) > limit


def bounded_count(count: int, cap: int = DEFAULT_COUNT_CAP) -> tuple[int, bool]:
    """Interpret the result of a capped count.

    A count that came back EQUAL to its cap may have stopped early, so it is a
    floor rather than a total. Treating it as exact is how a UI ends up
    confidently displaying "10,000 documents" for a corpus of four million.
    """
    return count, count < cap


def count_capped_sync(coll, query: dict, cap: int = DEFAULT_COUNT_CAP) -> tuple[int, bool]:
    """Bounded `count_documents` against a synchronous pymongo collection."""
    return bounded_count(coll.count_documents(query, limit=cap), cap)


async def count_capped(coll, query: dict, cap: int = DEFAULT_COUNT_CAP) -> tuple[int, bool]:
    """Bounded `count_documents` against an async collection."""
    return bounded_count(await coll.count_documents(query, limit=cap), cap)


def page_meta(
    *,
    rows: Sequence[Any],
    limit: int,
    skip: int,
    total: int | None = None,
    total_is_exact: bool = True,
    has_more: bool | None = None,
) -> PageMeta:
    """Assemble the contract from an already-trimmed page.

    `total` defaults to the honest minimum — what the caller has actually been
    shown — so an endpoint that cannot count at all still reports something
    true rather than zero.
    """
    seen = max(skip, 0) + len(rows)
    if total is None:
        total = seen
        # Nothing was counted, so the total is a floor by construction.
        total_is_exact = False if has_more else total_is_exact
    return PageMeta(
        total=max(total, seen if total_is_exact else 0),
        total_is_exact=total_is_exact,
        has_more=bool(has_more) if has_more is not None else seen < total,
        limit=limit,
        skip=skip,
    )


def distinct_in_order(items: Iterable[Any], key) -> list[Any]:
    """Collapse ranked results to distinct keys, keeping best-first order."""
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out
