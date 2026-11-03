"""Structured logs, and the correlation ids that survive a service boundary.

WHY THIS EXISTS
    A document's life crosses three services and a queue:

        browser -> ingestor-service      HTTP upload, writes GCS, publishes
                -> heavy-processing-engine   consumes, extracts, embeds
                -> task-service-agent        retrieves it in an answer

    None of that was traceable. Logs were plain text in all three, so there was
    nothing to query; the OOM incident ran 2.5 hours before anyone noticed a
    document was missing, and finding out what happened to it meant reading
    three terminals by eye.

    task-service-agent already had a RequestIDMiddleware. It minted a uuid,
    stashed it on request.state for the error handler, and NEVER PUT IT IN A
    LOG LINE — so it identified a failure response and nothing that led to it.

TWO IDS, BECAUSE THEY ANSWER DIFFERENT QUESTIONS
    request_id   one HTTP request. "What happened inside this API call."
    document_id  one document, for its whole life. "What ever happened to this
                 file." This is the one that matters for the journey, because
                 extraction happens minutes later in another process.

    The request_id is also written INTO THE QUEUE MESSAGE, so the worker's logs
    can be joined back to the upload that caused them. An HTTP header cannot
    cross a queue; that is the gap this closes.

HOW A FIELD REACHES EVERY LOG LINE
    A contextvar holds the bound fields and a logging.Filter copies them onto
    each record. Binding happens ONCE per request (middleware) or per message
    (the worker loop), so no call site has to pass anything down.

    `bind` always sets a NEW dict rather than mutating the current one. That is
    the opposite of the deliberate mutation-sharing in
    deep_research/search.py — there, parallel researchers had to append into one
    shared sink; here, two concurrent requests must never see each other's ids.
    asyncio copies the context per Task, so a fresh dict per bind keeps them
    isolated.

KEEP THE FIELD NAMES IDENTICAL ACROSS EVERY SERVICE
    A query is only useful if `request_id` means `request_id` everywhere — one
    trace spans ingestor-service, heavy-processing-engine and
    task-service-agent, joined on these names.

THIS IS THE SINGLE HOME
    Until 2026-11-03 this file was copied into each service and held in place by
    a hand-written identical fixture table per repo, because the services shared
    no library. They now share this one. Each service keeps a REPO GUARD
    asserting it defines nothing here itself — re-adding a local copy is the
    failure this package exists to prevent.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from typing import Any, Optional

# ---------------------------------------------------------------------------
# The cross-service field contract. Renaming one of these in a single service
# silently breaks every query that spans services, so each repo tests them.
# ---------------------------------------------------------------------------
FIELD_REQUEST_ID = "request_id"
FIELD_DOCUMENT_ID = "document_id"
FIELD_PROJECT_ID = "project_id"
FIELD_USER_ID = "user_id"
FIELD_SERVICE = "service"

CONTRACT_FIELDS = (
    FIELD_REQUEST_ID,
    FIELD_DOCUMENT_ID,
    FIELD_PROJECT_ID,
    FIELD_USER_ID,
    FIELD_SERVICE,
)

# The header a caller can set to continue an existing trace instead of starting
# a new one. Also the header we echo back.
REQUEST_ID_HEADER = "X-Request-ID"

_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "log_context", default=None
)

# Set once by configure_logging so every record carries its origin.
_service_name = os.getenv("SERVICE_NAME", "unknown")


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def current() -> dict:
    """The fields currently bound. Always a copy — callers cannot mutate state."""
    return dict(_context.get() or {})


def bind(**fields: Any):
    """Add fields to the logging context for this task. Returns a reset token.

    Values that are None are dropped rather than logged as null, so a line
    carries only the ids that are actually known at that point.
    """
    merged = current()
    merged.update({k: v for k, v in fields.items() if v is not None})
    return _context.set(merged)


def reset(token) -> None:
    """Restore the context to what it was before the matching `bind`."""
    try:
        _context.reset(token)
    except (ValueError, LookupError):
        # The token belongs to a different context — can happen if a caller
        # binds in one task and resets in another. Clearing is the safe answer;
        # leaking one request's ids into another's logs is not.
        _context.set(None)


def new_request_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Logging plumbing
# ---------------------------------------------------------------------------


class ContextFilter(logging.Filter):
    """Copy the bound context fields onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            setattr(record, key, value)
        return True


# Attributes the stdlib puts on every record. Anything NOT in here is something
# a caller attached (our context fields, or logger.info(..., extra={...})), and
# is worth emitting.
_STANDARD = frozenset(
    "args asctime created exc_info exc_text filename funcName levelname levelno "
    "lineno module msecs message msg name pathname process processName "
    "relativeCreated stack_info thread threadName taskName".split()
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    `severity` and `message` are named for Cloud Logging, which promotes them
    out of the payload automatically — so a Cloud Run deployment gets correct
    log levels and a readable summary line with no further configuration.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            FIELD_SERVICE: _service_name,
            "logger": record.name,
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_") or key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            # Without this a JSON formatter silently drops every traceback,
            # which would make the logs worse than the plain-text ones.
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        try:
            return json.dumps(payload, default=str)
        except Exception:
            # A log line must never raise. Fall back to the plain message.
            return json.dumps(
                {"severity": record.levelname, "message": record.getMessage(),
                 FIELD_SERVICE: _service_name}
            )


class TextFormatter(logging.Formatter):
    """Human-readable, with the bound ids appended. For a terminal."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ids = " ".join(
            f"{k}={getattr(record, k)}"
            for k in CONTRACT_FIELDS
            if k != FIELD_SERVICE and hasattr(record, k)
        )
        return f"{base}  [{ids}]" if ids else base


def _use_json() -> bool:
    """JSON unless we are clearly attached to a terminal.

    LOG_FORMAT=json|text overrides. The default leans to JSON because the
    failure modes are asymmetric: plain text in production means the logs are
    unqueryable and this whole module does nothing, while JSON in a terminal is
    merely ugly.
    """
    explicit = os.getenv("LOG_FORMAT", "").strip().lower()
    if explicit in ("json", "text"):
        return explicit == "json"
    try:
        return not sys.stderr.isatty()
    except Exception:
        return True


def configure_logging(service: str) -> None:
    """Install structured logging. Call once, at startup, before anything logs.

    Replaces the plain `logging.basicConfig` the three services each had.
    """
    global _service_name
    _service_name = service

    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if _use_json() else TextFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    # Replace rather than append: uvicorn/basicConfig may already have installed
    # a handler, and leaving it attached prints every line twice.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These flood at INFO and drown everything useful.
    for noisy in ("pymongo", "urllib3", "google.api_core", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
