"""The mail transport.

WHAT BREAKS IF THIS IS WRONG
    Mail is the one thing in these apps that leaves the building and cannot be
    recalled, and three of the failures below are silent by nature:

      * a MISSPELLED provider that disables mail instead of refusing to start —
        a deployment that quietly never emails anybody, and nothing says so
        until a user asks where their digest is.
      * a FAILURE that returns like a success, so the caller reports "sent" for
        a message that was not.
      * a SUBJECT built from user text, carrying a newline, appending headers
        of the sender's choosing.
"""

from __future__ import annotations

import logging

import pytest

from apex_common.email import (
    PROVIDERS,
    ConsoleMailer,
    EmailConfig,
    EmailError,
    Mailer,
    SmtpMailer,
    get_mailer,
)


def _cfg(**over) -> EmailConfig:
    base = dict(
        provider="smtp", from_address="notes@example.com", from_name="Hearthbook",
        host="smtp.example.com", port=587, username="u", password="p",
    )
    base.update(over)
    return EmailConfig(**base)


# ── A fake SMTP server ────────────────────────────────────────────────────────

class FakeSMTP:
    """Records what the mailer did to it. One instance per connection."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.logged_in: tuple | None = None
        self.sent: list = []
        self.closed = False
        self.raise_on_send: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr("apex_common.email.smtplib.SMTP", FakeSMTP)
    return FakeSMTP


def _last(smtp):
    assert smtp.instances, "no connection was opened"
    conn = smtp.instances[-1]
    assert conn.sent, "nothing was sent"
    return conn.sent[-1]


# ── get_mailer: off, on, and misspelled ───────────────────────────────────────

class TestSelection:
    def test_no_provider_is_none_not_an_error(self):
        """Most deployments send no mail. None is the answer the caller branches
        on to decide whether the route exists at all."""
        assert get_mailer(EmailConfig()) is None

    def test_whitespace_only_provider_is_also_off(self):
        assert get_mailer(EmailConfig(provider="   ")) is None

    def test_a_misspelled_provider_refuses_to_start(self, smtp):
        """THE SILENT ONE. Falling back to disabled would deploy a service that
        never emails anybody, and nothing would say why."""
        with pytest.raises(ValueError) as e:
            get_mailer(_cfg(provider="sendgird"))
        assert "sendgird" in str(e.value)
        for p in PROVIDERS:
            assert p in str(e.value), "the message must name the valid options"

    def test_the_provider_name_is_case_insensitive(self, smtp):
        assert isinstance(get_mailer(_cfg(provider="SMTP")), SmtpMailer)

    def test_console_is_selectable(self):
        assert isinstance(
            get_mailer(EmailConfig(provider="console", from_address="a@b.c")),
            ConsoleMailer,
        )

    def test_both_mailers_satisfy_the_protocol(self, smtp):
        """The services depend on Mailer, not on either class."""
        assert isinstance(get_mailer(_cfg()), Mailer)
        assert isinstance(
            get_mailer(EmailConfig(provider="console", from_address="a@b.c")), Mailer
        )


# ── Misconfiguration fails at startup, not at 3am ─────────────────────────────

class TestConstructionGuards:
    def test_smtp_without_a_host_refuses(self):
        with pytest.raises(ValueError, match="MAIL_HOST"):
            get_mailer(_cfg(host=""))

    def test_smtp_without_a_from_address_refuses(self):
        with pytest.raises(ValueError, match="MAIL_FROM"):
            get_mailer(_cfg(from_address=""))

    def test_a_username_with_no_password_refuses(self):
        """Half-entered credentials. SMTP would answer 535 on the first send,
        hours later; this answers at startup."""
        with pytest.raises(ValueError, match="MAIL_PASSWORD"):
            get_mailer(_cfg(password=""))

    def test_no_credentials_at_all_is_fine(self, smtp):
        """An open relay inside a VPC, or a local capture server."""
        mailer = get_mailer(_cfg(username="", password=""))
        mailer.send(to=["a@b.c"], subject="s", text="t")
        assert smtp.instances[-1].logged_in is None

    def test_console_without_a_from_address_refuses(self):
        with pytest.raises(ValueError, match="MAIL_FROM"):
            get_mailer(EmailConfig(provider="console"))

    def test_the_password_is_not_in_the_repr(self):
        """Settings objects get logged and dumped into error reports."""
        assert "hunter2" not in repr(_cfg(password="hunter2"))


# ── The message ───────────────────────────────────────────────────────────────

class TestMessage:
    def test_the_from_header_carries_the_display_name(self, smtp):
        get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text="t")
        assert _last(smtp)["From"] == "Hearthbook <notes@example.com>"

    def test_without_a_display_name_it_is_the_bare_address(self, smtp):
        get_mailer(_cfg(from_name="")).send(to=["a@b.c"], subject="s", text="t")
        assert _last(smtp)["From"] == "notes@example.com"

    def test_several_recipients_are_joined(self, smtp):
        get_mailer(_cfg()).send(to=["a@b.c", "d@e.f"], subject="s", text="t")
        assert _last(smtp)["To"] == "a@b.c, d@e.f"

    def test_blank_recipients_are_dropped_and_the_rest_sent(self, smtp):
        get_mailer(_cfg()).send(to=["a@b.c", "", "  "], subject="s", text="t")
        assert _last(smtp)["To"] == "a@b.c"

    def test_reply_to_is_set_when_given_and_absent_otherwise(self, smtp):
        m = get_mailer(_cfg())
        m.send(to=["a@b.c"], subject="s", text="t", reply_to="me@example.com")
        assert _last(smtp)["Reply-To"] == "me@example.com"
        m.send(to=["a@b.c"], subject="s", text="t")
        assert _last(smtp)["Reply-To"] is None

    def test_html_never_replaces_the_text_part(self, smtp):
        """A client that will not render html must still show the content. A
        digest nobody can read is a digest nobody keeps receiving."""
        get_mailer(_cfg()).send(
            to=["a@b.c"], subject="s", text="the plain words",
            html="<p>the rich words</p>",
        )
        msg = _last(smtp)
        assert msg.is_multipart()
        bodies = {
            part.get_content_type(): part.get_content()
            for part in msg.walk()
            if part.get_content_maintype() == "text"
        }
        assert "the plain words" in bodies["text/plain"]
        assert "the rich words" in bodies["text/html"]

    def test_text_only_is_not_multipart(self, smtp):
        get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text="t")
        assert _last(smtp).is_multipart() is False


# ── Header injection ──────────────────────────────────────────────────────────

class TestHeaderInjection:
    """A subject is often built from user text — an entry title, a document
    name. A newline in a header value lets the caller append headers of their
    own choosing."""

    @pytest.mark.parametrize("evil", [
        "Your week\nBcc: someone@else.com",
        "Your week\r\nBcc: someone@else.com",
        "Your week\rX-Thing: 1",
    ])
    def test_a_newline_in_the_subject_is_refused(self, smtp, evil):
        with pytest.raises(EmailError, match="line break"):
            get_mailer(_cfg()).send(to=["a@b.c"], subject=evil, text="t")
        assert not smtp.instances, "nothing may be sent"

    def test_a_newline_in_a_recipient_is_refused(self, smtp):
        with pytest.raises(EmailError, match="line break"):
            get_mailer(_cfg()).send(
                to=["a@b.c\nBcc: someone@else.com"], subject="s", text="t"
            )
        assert not smtp.instances

    def test_a_newline_in_reply_to_is_refused(self, smtp):
        with pytest.raises(EmailError, match="line break"):
            get_mailer(_cfg()).send(
                to=["a@b.c"], subject="s", text="t", reply_to="x@y.z\nBcc: q@r.s"
            )
        assert not smtp.instances

    def test_the_body_may_contain_anything(self, smtp):
        """The restriction is on HEADERS. A diary entry is full of newlines and
        may well contain the word Bcc."""
        body = "line one\nBcc: not a header\n\nline four"
        get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text=body)
        assert _last(smtp).get_content().strip() == body

    def test_no_recipient_at_all_is_refused(self, smtp):
        for empty in ([], [""], ["   "]):
            with pytest.raises(EmailError, match="No recipient"):
                get_mailer(_cfg()).send(to=empty, subject="s", text="t")
        assert not smtp.instances


# ── Transport behaviour ───────────────────────────────────────────────────────

class TestTransport:
    def test_starttls_and_login_happen_by_default(self, smtp):
        get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text="t")
        conn = smtp.instances[-1]
        assert conn.started_tls is True
        assert conn.logged_in == ("u", "p")
        assert (conn.host, conn.port) == ("smtp.example.com", 587)

    def test_starttls_can_be_turned_off_for_a_local_capture_server(self, smtp):
        """MailHog and friends speak no TLS at all."""
        get_mailer(_cfg(starttls=False)).send(to=["a@b.c"], subject="s", text="t")
        assert smtp.instances[-1].started_tls is False

    def test_the_timeout_is_passed_through(self, smtp):
        """Without it a hung SMTP server holds the request open indefinitely."""
        get_mailer(_cfg(timeout_s=3.5)).send(to=["a@b.c"], subject="s", text="t")
        assert smtp.instances[-1].timeout == 3.5

    def test_the_connection_is_closed_even_when_the_send_fails(self, smtp):
        mailer = get_mailer(_cfg())
        with pytest.raises(EmailError):
            FakeSMTP.instances = []
            orig = FakeSMTP.__init__

            def boom_init(self, *a, **k):
                orig(self, *a, **k)
                self.raise_on_send = RuntimeError("mailbox full")

            FakeSMTP.__init__ = boom_init
            try:
                mailer.send(to=["a@b.c"], subject="s", text="t")
            finally:
                FakeSMTP.__init__ = orig
        assert smtp.instances[-1].closed is True

    def test_one_connection_per_message(self, smtp):
        """Deliberately not pooled — see the class docstring. Pinned so that
        changing it is a decision rather than a side effect."""
        mailer = get_mailer(_cfg())
        for _ in range(3):
            mailer.send(to=["a@b.c"], subject="s", text="t")
        assert len(smtp.instances) == 3


# ── Failure is raised, never swallowed ────────────────────────────────────────

class TestFailure:
    def _failing(self, smtp, exc):
        orig = FakeSMTP.__init__

        def init(self, *a, **k):
            orig(self, *a, **k)
            self.raise_on_send = exc

        FakeSMTP.__init__ = init
        return orig

    def test_a_transport_failure_raises_email_error(self, smtp):
        """THE IMPORTANT ONE. A library that logged and returned would make
        "the user is waiting for this" impossible to serve correctly."""
        orig = self._failing(smtp, RuntimeError("535 auth failed"))
        try:
            with pytest.raises(EmailError) as e:
                get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text="t")
        finally:
            FakeSMTP.__init__ = orig
        # The provider's own words are the useful part: "535 auth failed" tells
        # an operator the password is wrong; the exception type alone does not.
        assert "535 auth failed" in str(e.value)
        assert "smtp.example.com" in str(e.value)

    def test_the_original_exception_is_chained(self, smtp):
        orig = self._failing(smtp, RuntimeError("boom"))
        try:
            with pytest.raises(EmailError) as e:
                get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text="t")
        finally:
            FakeSMTP.__init__ = orig
        assert isinstance(e.value.__cause__, RuntimeError)

    def test_an_email_error_is_not_double_wrapped(self, smtp):
        orig = self._failing(smtp, EmailError("already ours"))
        try:
            with pytest.raises(EmailError) as e:
                get_mailer(_cfg()).send(to=["a@b.c"], subject="s", text="t")
        finally:
            FakeSMTP.__init__ = orig
        assert str(e.value) == "already ours"

    def test_email_error_is_catchable_as_runtime_error(self):
        """Consumers that already catch broadly keep working."""
        assert issubclass(EmailError, RuntimeError)


# ── The console mailer ────────────────────────────────────────────────────────

class TestConsoleMailer:
    def test_it_logs_instead_of_connecting(self, smtp, caplog):
        mailer = get_mailer(EmailConfig(
            provider="console", from_address="a@b.c", from_name="Dev",
        ))
        with caplog.at_level(logging.INFO, logger="apex_common.email"):
            mailer.send(to=["x@y.z"], subject="Your week", text="body")
        assert not smtp.instances, "console must not open a connection"
        assert "Your week" in caplog.text and "x@y.z" in caplog.text

    def test_it_applies_the_same_header_rules(self):
        """Choosing console for local development must not let an injection
        through that production would refuse — or the check is only tested in
        the environment where it is hardest to notice."""
        mailer = get_mailer(EmailConfig(provider="console", from_address="a@b.c"))
        with pytest.raises(EmailError, match="line break"):
            mailer.send(to=["x@y.z"], subject="s\nBcc: q@r.s", text="t")
        with pytest.raises(EmailError, match="No recipient"):
            mailer.send(to=[], subject="s", text="t")
