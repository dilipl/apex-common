"""Structured logging and the correlation ids that cross service boundaries.

WHAT BREAKS IF THIS IS WRONG
    Two failure modes, and the second is worse than having no tracing at all:

      * Nothing is logged with an id  -> the feature silently does nothing and
        a document's journey stays untraceable.
      * IDS LEAK BETWEEN CONCURRENT REQUESTS -> the logs actively lie, and
        someone debugging an incident follows a trail into the wrong request.

    The isolation tests below exist for the second one. The worker handles ten
    messages at once and every FastAPI app serves concurrent requests, so this
    is the normal case, not an edge case.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from apex_common import observability as obs


@pytest.fixture(autouse=True)
def clean_context():
    """Every test starts with an empty context and leaves one behind."""
    obs._context.set(None)
    yield
    obs._context.set(None)


@pytest.fixture
def capture():
    """Capture emitted records as parsed JSON, through the real formatter."""
    import io

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(obs.JsonFormatter())
    handler.addFilter(obs.ContextFilter())
    log = logging.getLogger("test.obs")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    def lines():
        return [json.loads(l) for l in buf.getvalue().strip().splitlines() if l.strip()]

    return log, lines


# ===========================================================================
# The JSON shape
# ===========================================================================


def test_a_log_line_is_one_json_object(capture):
    log, lines = capture
    log.info("hello")
    assert len(lines()) == 1
    assert lines()[0]["message"] == "hello"


def test_severity_and_message_are_named_for_cloud_logging(capture):
    """Cloud Run promotes `severity` and `message` out of the payload. Naming
    them anything else means every line shows up as INFO with no summary."""
    log, lines = capture
    log.warning("careful")
    entry = lines()[0]
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "careful"


def test_printf_style_arguments_are_interpolated(capture):
    log, lines = capture
    log.info("extracted %d pages from %s", 417, "erp.pdf")
    assert lines()[0]["message"] == "extracted 417 pages from erp.pdf"


def test_tracebacks_survive(capture):
    """A JSON formatter that drops exc_info makes the logs WORSE than text."""
    log, lines = capture
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("it failed")
    assert "ValueError: boom" in lines()[0]["exception"]


def test_the_service_name_is_on_every_line(capture):
    log, lines = capture
    log.info("x")
    assert lines()[0][obs.FIELD_SERVICE]


def test_extra_fields_are_emitted(capture):
    log, lines = capture
    log.info("indexed", extra={obs.FIELD_DOCUMENT_ID: "doc-9", "chunks": 1059})
    entry = lines()[0]
    assert entry[obs.FIELD_DOCUMENT_ID] == "doc-9"
    assert entry["chunks"] == 1059


def test_an_unserialisable_value_does_not_kill_the_log_line(capture):
    """Logging must never raise. A line that cannot be rendered still goes out."""
    log, lines = capture

    class Weird:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    log.info("odd", extra={"thing": Weird()})
    assert lines()[0]["message"] == "odd"


# ===========================================================================
# Binding
# ===========================================================================


def test_bound_fields_reach_every_subsequent_line(capture):
    log, lines = capture
    obs.bind(**{obs.FIELD_REQUEST_ID: "req-1"})
    log.info("one")
    log.info("two")
    assert all(e[obs.FIELD_REQUEST_ID] == "req-1" for e in lines())


def test_binding_twice_accumulates(capture):
    log, lines = capture
    obs.bind(**{obs.FIELD_REQUEST_ID: "req-1"})
    obs.bind(**{obs.FIELD_DOCUMENT_ID: "doc-1"})
    log.info("x")
    entry = lines()[0]
    assert entry[obs.FIELD_REQUEST_ID] == "req-1"
    assert entry[obs.FIELD_DOCUMENT_ID] == "doc-1"


def test_none_values_are_dropped_not_logged_as_null(capture):
    """A line should carry the ids that are KNOWN, not a column of nulls."""
    log, lines = capture
    obs.bind(**{obs.FIELD_REQUEST_ID: "req-1", obs.FIELD_DOCUMENT_ID: None})
    log.info("x")
    assert obs.FIELD_DOCUMENT_ID not in lines()[0]


def test_reset_restores_the_previous_context(capture):
    log, lines = capture
    token = obs.bind(**{obs.FIELD_REQUEST_ID: "outer"})
    inner = obs.bind(**{obs.FIELD_DOCUMENT_ID: "doc-1"})
    obs.reset(inner)
    log.info("after inner reset")
    assert lines()[0][obs.FIELD_REQUEST_ID] == "outer"
    assert obs.FIELD_DOCUMENT_ID not in lines()[0]
    obs.reset(token)


def test_current_returns_a_copy(capture):
    """Handing out the live dict would let a caller corrupt another request."""
    obs.bind(**{obs.FIELD_REQUEST_ID: "req-1"})
    snapshot = obs.current()
    snapshot["request_id"] = "tampered"
    assert obs.current()[obs.FIELD_REQUEST_ID] == "req-1"


def test_resetting_a_foreign_token_clears_rather_than_leaking(capture):
    """A token from another context cannot be reset. Clearing is the safe
    answer; leaving one request's ids bound is not."""
    log, lines = capture

    async def make_token():
        return obs.bind(**{obs.FIELD_REQUEST_ID: "from-another-task"})

    token = asyncio.run(make_token())
    obs.bind(**{obs.FIELD_REQUEST_ID: "mine"})
    obs.reset(token)
    log.info("x")
    assert obs.FIELD_REQUEST_ID not in lines()[0]


# ===========================================================================
# ISOLATION — the property that makes the logs trustworthy
# ===========================================================================


async def test_concurrent_tasks_do_not_see_each_others_ids(capture):
    """Ten messages process at once in the worker. If their contexts mixed, the
    logs would confidently attribute one document's failure to another."""
    log, lines = capture

    async def handle(n):
        obs.bind(**{obs.FIELD_REQUEST_ID: f"req-{n}",
                    obs.FIELD_DOCUMENT_ID: f"doc-{n}"})
        await asyncio.sleep(0.01)          # force interleaving
        log.info("working")
        await asyncio.sleep(0.01)
        log.info("done")

    await asyncio.gather(*(handle(i) for i in range(10)))

    entries = lines()
    assert len(entries) == 20
    for e in entries:
        n = e[obs.FIELD_REQUEST_ID].split("-")[1]
        assert e[obs.FIELD_DOCUMENT_ID] == f"doc-{n}", "contexts crossed"


async def test_a_child_task_does_not_leak_back_into_its_parent(capture):
    log, lines = capture
    obs.bind(**{obs.FIELD_REQUEST_ID: "parent"})

    async def child():
        obs.bind(**{obs.FIELD_REQUEST_ID: "child"})

    await asyncio.create_task(child())
    log.info("after child")
    assert lines()[0][obs.FIELD_REQUEST_ID] == "parent"


# ===========================================================================
# CROSS-SERVICE CONTRACT
# ===========================================================================
#
# One trace spans ingestor-service, heavy-processing-engine and
# task-service-agent, joined on these field names. Renaming one would break
# every cross-service query silently; this makes it break a test instead.
# These names were duplicated per repo before this package existed.


def test_the_field_names_are_the_agreed_ones():
    assert obs.FIELD_REQUEST_ID == "request_id"
    assert obs.FIELD_DOCUMENT_ID == "document_id"
    assert obs.FIELD_PROJECT_ID == "project_id"
    assert obs.FIELD_USER_ID == "user_id"
    assert obs.FIELD_SERVICE == "service"
    assert obs.REQUEST_ID_HEADER == "X-Request-ID"


def test_every_contract_field_is_listed():
    """CONTRACT_FIELDS drives the text formatter; a field missing from it is a
    field that silently stops appearing in local logs."""
    assert set(obs.CONTRACT_FIELDS) == {
        "request_id", "document_id", "project_id", "user_id", "service",
    }


@pytest.mark.parametrize("fmt,expect_json", [("json", True), ("text", False)])
def test_the_format_can_be_forced(monkeypatch, fmt, expect_json):
    monkeypatch.setenv("LOG_FORMAT", fmt)
    assert obs._use_json() is expect_json


def test_json_is_the_default_away_from_a_terminal(monkeypatch):
    """Production is the case that matters: plain text there means the logs are
    unqueryable and this whole module does nothing."""
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.setattr(obs.sys, "stderr", type("S", (), {"isatty": lambda s: False})())
    assert obs._use_json() is True
