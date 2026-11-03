"""The document-readiness contract, pinned.

WHY A TEST AND NOT JUST A COMMENT
    This rule decides whether the Library tells you a document can be queried
    yet. It now lives in TWO services, because the document LISTING moved to
    ingestor-service (which owns the records) while semantic search over
    document CONTENT stayed in task-service-agent (which owns Milvus and the
    embedding service). Both return the same row, with the same `state` field.

    If the two mappings drift, nothing errors. The badge on a row just changes
    depending on whether you had typed in the search box — and in the direction
    that matters, a document with no vectors could be reported `ready` by one
    service and not the other. "Ready" is the one answer that lets a tool run
    against a document retrieval cannot see, which is the worst failure shape
    in this system: a fluent, correctly-cited answer to a different question.

    The fixture table below is IDENTICAL in both repos. Editing one service's
    mapping fails that service's suite, with this file naming the other one.
    The repos share no library — the same constraint as observability.py,
    pagination.py and title_norm.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apex_common.doc_status import (
    DOC_STATUS_CONTRACT,
    FAILED,
    NO_STATUS_GRACE_MINUTES,
    READY,
    UNKNOWN,
    WORKING,
    classify,
    is_pollable,
)

OTHER_SERVICE = "ingestor-service/app/services/doc_status.py"

# ---------------------------------------------------------------------------
# THE CONTRACT TABLE — identical in both repos. Change it in both, or not at all.
#
# The left column is heavy-processing-engine's vocabulary (ProcessingStatus in
# status_tracker.py). hpe owns those names; both readers must agree on what
# they mean for "can I use this".
# ---------------------------------------------------------------------------
CASES: list[tuple[str | None, str]] = [
    # The only status that means there are vectors.
    ("completed", READY),
    # On its way.
    ("queued", WORKING),
    ("processing", WORKING),
    ("extraction_started", WORKING),
    # Text is out, vectors are not in yet — still not queryable.
    ("extraction_completed", WORKING),
    # Never, without intervention.
    ("failed", FAILED),
    # FAILED here even though the admin taxonomy treats only {completed, failed}
    # as terminal: no vectors means not queryable, whatever stage it died at.
    ("failed_embedding", FAILED),
    # A status hpe adds that neither reader has learned. NOT guessed at, and
    # emphatically not `ready` — an unrecognised status reported as ready is
    # how a tool runs against a document that has no vectors.
    ("some_new_hpe_status", UNKNOWN),
    # An absent or blank status is NOT in this table: with no status row the
    # answer depends on the document's age, which is the grace-period rule
    # tested separately below.
    # Case and padding are folded, because these values cross a service
    # boundary as free text.
    ("COMPLETED", READY),
    ("  completed  ", READY),
    ("Failed_Embedding", FAILED),
]


@pytest.mark.parametrize("status,expected", CASES)
def test_the_readiness_mapping(status, expected):
    """A document with a status row, classified. No created_at involved."""
    assert classify(status, datetime.now(timezone.utc)) == expected


@pytest.mark.parametrize("status,expected", CASES)
def test_the_mapping_ignores_age_when_a_status_exists(status, expected):
    """Age is only a fallback. A status row always wins, however old the row."""
    ancient = datetime.now(timezone.utc) - timedelta(days=365)
    assert classify(status, ancient) == expected


def test_the_contract_identifier_is_pinned():
    """Bumping this is the deliberate act of changing the rule.

    If this fails, the mapping changed. Update the table in BOTH repos and bump
    the constant in both, then re-run both suites.
    """
    assert DOC_STATUS_CONTRACT == "2026-10-21", (
        "The readiness mapping changed. Mirror it in "
        f"{OTHER_SERVICE} and bump DOC_STATUS_CONTRACT in both."
    )


# ── No status row at all: the grace period ────────────────────────────────────

def test_a_just_uploaded_document_with_no_status_row_is_working():
    """The worker may not have written its first row yet. Reporting `unknown`
    here would put a 'cannot tell' badge on every fresh upload."""
    assert classify(None, datetime.now(timezone.utc)) == WORKING


def test_an_old_document_with_no_status_row_is_unknown_not_failed():
    """It was never picked up, or predates status tracking. Both are "cannot
    tell" — rendering them as broken would have shown a whole legacy corpus as
    broken the day this shipped."""
    old = datetime.now(timezone.utc) - timedelta(minutes=NO_STATUS_GRACE_MINUTES + 1)
    assert classify(None, old) == UNKNOWN


def test_the_grace_boundary_is_inclusive():
    at_boundary = datetime.now(timezone.utc) - timedelta(
        minutes=NO_STATUS_GRACE_MINUTES - 0.1
    )
    assert classify(None, at_boundary) == WORKING


def test_a_naive_datetime_is_read_as_utc():
    """Rows written by different services are not consistently tz-aware; a
    naive value must not raise when subtracted from an aware now()."""
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert classify(None, naive) == WORKING


def test_an_iso_string_created_at_is_accepted():
    """A few legacy rows store created_at as a string."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    assert classify(None, now) == WORKING


def test_an_unreadable_created_at_is_treated_as_old():
    """The conservative direction: claiming `working` forever would make the
    client poll forever."""
    assert classify(None, "not a date") == UNKNOWN
    assert classify(None, None) == UNKNOWN
    assert classify(None, 12345) == UNKNOWN


# ── What the client polls on ──────────────────────────────────────────────────

def test_only_working_is_pollable():
    """`working` is the only state that can change on its own, which is what
    stops the Library polling a finished project forever."""
    assert is_pollable(WORKING) is True
    for state in (READY, FAILED, UNKNOWN):
        assert is_pollable(state) is False


def test_the_four_state_names_are_the_wire_values():
    """The client branches on these strings; they are a contract, not labels."""
    assert (READY, WORKING, FAILED, UNKNOWN) == (
        "ready", "working", "failed", "unknown",
    )
