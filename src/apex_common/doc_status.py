"""Can this document be queried yet?

THE PROBLEM THIS ANSWERS
    An upload appeared in the Library IMMEDIATELY and looked identical to a
    document that finished processing weeks ago. Extraction takes minutes.
    `/documents` returned `{_id, file_name, file_type, created_at}` — no status
    field at all — so the card had nothing to render and nothing re-checked.

    The failure that follows is the worst shape in this codebase: ask a tool
    about a document that has no vectors yet, and retrieval finds nothing for
    it, but the tools are grounded across the WHOLE PROJECT. So the model
    answers fluently from the OTHER documents, correctly cited, with nothing
    saying the document you asked about was not ready. Not an error — a
    confident answer to a different question.

A LIBRARY NEEDS THREE STATES, NOT SEVEN
    heavy-processing-engine owns the vocabulary (ProcessingStatus in
    status_tracker.py): queued, processing, extraction_started,
    extraction_completed, failed_embedding, completed, failed. That detail is
    right for an operator and noise for a reader, whose only question is
    "can I use this yet?":

        ready    completed              -> has vectors, retrievable now
        working  queued/processing/
                 extraction_*           -> not yet, but on its way
        failed   failed/failed_embedding-> never, without intervention
        unknown  no status row at all   -> we genuinely do not know

    `failed_embedding` counts as FAILED here even though ingestor's admin
    taxonomy treats only {completed, failed} as terminal: a document whose
    embedding failed has no vectors, so for "can I query it" the answer is no.
    The admin screen's richer view is the place to see the difference.

WHY `unknown` IS NOT `failed`
    A document with no status row was never picked up, OR predates status
    tracking. Both are "I cannot tell", and a Library that renders them as
    broken would have shown all nineteen documents in this corpus as broken
    the day this shipped. A YOUNG document with no row is reported `working`
    instead — it was probably uploaded seconds ago and the worker has not
    written its first row yet.

    DELIBERATELY NOT DIAGNOSING. "This has been pending for 40 minutes and is
    probably stuck" is the admin screen's job (ingestor's admin_service derives
    stuck/no_status with the same age threshold). Duplicating that judgement
    here would give two screens two opinions about one document; the Library
    says only what it can see.

THIS IS THE SINGLE HOME
    Until 2026-11-03 this file was copied into each service and held in place by
    a hand-written identical fixture table per repo, because the services shared
    no library. They now share this one. Each service keeps a REPO GUARD
    asserting it defines nothing here itself — re-adding a local copy is the
    failure this package exists to prevent.

    BOTH SERVICES NEED IT: ingestor-service serves the document LISTING and
    task-service-agent serves CONTENT SEARCH, and both return the same row
    shape. If the mapping differed, a row's readiness badge would change
    depending on whether you had typed in the search box.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

READY = "ready"
WORKING = "working"
FAILED = "failed"
UNKNOWN = "unknown"

# hpe's vocabulary, mapped to the three answers a reader needs. Listed
# explicitly rather than matched by prefix so a NEW status hpe introduces lands
# in `unknown` — visible and harmless — instead of being silently guessed at.
_READY_STATUSES = frozenset({"completed"})
_FAILED_STATUSES = frozenset({"failed", "failed_embedding"})
_WORKING_STATUSES = frozenset({
    "queued", "processing", "extraction_started", "extraction_completed",
})

# A document with no status row yet is reported `working` for this long, after
# which it becomes `unknown`. Mirrors ingestor's STUCK_AFTER_MINUTES=20, which
# is where "it has been too long" is actually diagnosed; this value only decides
# how long we give the worker to write its first row.
NO_STATUS_GRACE_MINUTES = 20

# Bump ONLY when the MAPPING changes — not when a comment does. The paired
# contract test in the other repo asserts this same value, so a one-sided edit
# to the rule fails both suites instead of silently giving one service a
# different opinion about whether a document is usable.
DOC_STATUS_CONTRACT = "2026-10-21"


def _age_minutes(created_at: Any) -> Optional[float]:
    """Minutes since `created_at`, or None when it cannot be read.

    Documents carry a datetime; a few legacy rows carry a string. An unreadable
    date returns None, which the caller treats as "old" — the conservative
    direction, since claiming `working` forever would make the UI poll forever.
    """
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0


def classify(status: Optional[str], created_at: Any = None) -> str:
    """Map a raw processing status to one of READY/WORKING/FAILED/UNKNOWN."""
    if status:
        normalised = str(status).strip().lower()
        if normalised in _READY_STATUSES:
            return READY
        if normalised in _FAILED_STATUSES:
            return FAILED
        if normalised in _WORKING_STATUSES:
            return WORKING
        # A status hpe added that this service has not learned. Not ready, not
        # known to have failed — say so rather than guess.
        return UNKNOWN

    age = _age_minutes(created_at)
    if age is not None and age <= NO_STATUS_GRACE_MINUTES:
        return WORKING
    return UNKNOWN


def is_pollable(state: str) -> bool:
    """Whether this state can still change on its own.

    The client polls only while something is pollable, which is what stops the
    Library polling a finished project forever.
    """
    return state == WORKING


def statuses_for(collection, document_ids: list[str]) -> dict[str, dict]:
    """Raw status rows for a page of documents, keyed by document id.

    ONE query for the page, on the indexed `document_id` — not one per row.
    `processing_status` is written by heavy-processing-engine and read here;
    joining in the application rather than with $lookup keeps the "no row at
    all" case visible, which an aggregation would render as an empty array and
    bury. (The same reason ingestor's admin_service joins in Python.)
    """
    if not document_ids:
        return {}
    rows = collection.find(
        {"document_id": {"$in": list(document_ids)}},
        {"document_id": 1, "status": 1, "error": 1, "message": 1},
    )
    return {str(r.get("document_id")): r for r in rows}
