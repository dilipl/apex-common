"""Sending mail — the transport, not the message.

WHAT THIS IS FOR
    Two services need to send mail and they need to agree on nothing except
    how it leaves the building: ingestor-service emails a note or a transcript
    the user asked for, task-service-agent emails a digest it generated. The
    TRANSPORT is infrastructure and belongs here. The MESSAGE — subject lines,
    layout, what a digest says — is domain and stays in each service.

NO VENDOR, AND NO DEPENDENCY
    Every hosted mail provider speaks SMTP, and smtplib is in the standard
    library. So this module has NO dependency of its own and therefore no
    optional extra: adding a provider's REST client later would be the thing
    that needs one, and the protocol below is what makes that a new class
    rather than a change at the call sites.

IT RAISES. IT DOES NOT SWALLOW.
    `send` raises EmailError on failure, and each caller decides what that
    means — because the right answer differs:

        "email me this entry"   the user is waiting. A failure must reach them
                                as a failure, not as a success they will wait
                                for forever.
        the nightly digest      one bad address must not stop the other two
                                hundred. The loop logs and continues.

    A library that logged-and-returned would make the first case impossible,
    and this codebase has already paid for a degrade-silently default: output
    bodies were written only to object storage, the failure path set the key to
    "" while the row still said `completed`, and two reports were lost without
    a trace. Degrading is a decision for the caller who knows what is waiting.

UNCONFIGURED IS `None`, NOT A MAILER THAT FAILS
    `get_mailer` returns None when no provider is set, so a service MOUNTS its
    "email this to me" route only when it can actually send. That is the same
    principle the agent catalogue applies — a capability a deployment does not
    have is absent rather than present and broken — and it means the frontend
    discovers what exists instead of offering a button that 500s.

FAILS AT STARTUP, NOT AT SEND
    A misconfigured SMTP block raises from the constructor, where an operator
    is watching the logs, rather than at 3am on the first digest. Same reason
    apex_common.auth checks its cookie policy before anything is registered.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "EmailConfig",
    "EmailError",
    "Mailer",
    "ConsoleMailer",
    "SmtpMailer",
    "get_mailer",
    "PROVIDERS",
]

# The providers this module knows. "" means mail is off and `get_mailer`
# returns None.
PROVIDERS = ("smtp", "console")

# Header values are single-line by definition. A newline in one lets a caller
# append headers of their own — an extra Bcc, a different Reply-To — so a
# subject built from user text is an injection vector. Rejected rather than
# stripped: silently rewriting what a caller asked to send is its own surprise.
_FORBIDDEN_IN_HEADER = ("\r", "\n")


class EmailError(RuntimeError):
    """A message could not be sent. The caller decides whether that is fatal."""


@dataclass(frozen=True)
class EmailConfig:
    """What a deployment needs to send mail. Read from that service's settings.

    `provider` blank means mail is OFF, which is the default everywhere — the
    base engine sends no mail and nothing about it changes by this module
    existing.
    """

    provider: str = ""
    from_address: str = ""
    from_name: str = ""
    # SMTP only.
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)
    # STARTTLS on the standard submission port. Set false ONLY for a local
    # capture server (MailHog and friends), which speaks no TLS at all.
    starttls: bool = True
    timeout_s: float = 10.0


@runtime_checkable
class Mailer(Protocol):
    """Send one message. Raises EmailError if it could not be handed off."""

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> None: ...


def _check_recipients(to: Sequence[str]) -> list[str]:
    """Reject what cannot be sent, and nothing more.

    DELIBERATELY NOT VALIDATING ADDRESS SYNTAX. A regex that decides what an
    email address may look like is wrong at the edges (it rejects valid
    addresses, which is worse than a bounce) and the SMTP server is the real
    authority. What IS checked is the shape this module can be sure about: at
    least one recipient, no blanks, and no header injection.
    """
    cleaned = [addr.strip() for addr in to if addr and addr.strip()]
    if not cleaned:
        raise EmailError("No recipient")
    for addr in cleaned:
        if any(ch in addr for ch in _FORBIDDEN_IN_HEADER):
            raise EmailError("Recipient contains a line break")
    return cleaned


def _check_subject(subject: str) -> str:
    if any(ch in subject for ch in _FORBIDDEN_IN_HEADER):
        raise EmailError("Subject contains a line break")
    return subject


def _build(
    config: EmailConfig,
    *,
    to: Sequence[str],
    subject: str,
    text: str,
    html: Optional[str],
    reply_to: Optional[str],
) -> EmailMessage:
    """The message itself, identical whatever carries it.

    text is ALWAYS set, even when html is given: a mail client that cannot or
    will not render html must still show the content, and a digest nobody can
    read is a digest nobody keeps receiving.
    """
    recipients = _check_recipients(to)
    msg = EmailMessage()
    msg["From"] = (
        formataddr((config.from_name, config.from_address))
        if config.from_name
        else config.from_address
    )
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = _check_subject(subject)
    if reply_to:
        msg["Reply-To"] = _check_subject(reply_to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class ConsoleMailer:
    """Logs the message instead of sending it.

    For local development, where nobody has SMTP credentials and nobody should
    need them to work on a digest template. It is NOT the disabled state —
    disabled is `get_mailer` returning None — because a developer choosing this
    wants to SEE the mail, and a route that is mounted and logs is exactly that.
    """

    def __init__(self, config: EmailConfig) -> None:
        if not config.from_address:
            raise ValueError("MAIL_FROM is required")
        self._config = config

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> None:
        msg = _build(
            self._config, to=to, subject=subject, text=text, html=html,
            reply_to=reply_to,
        )
        logger.info(
            "mail (console) to=%s subject=%s bytes=%d",
            msg["To"], msg["Subject"], len(text),
        )


class SmtpMailer:
    """SMTP submission, one connection per message.

    NOT POOLED. A digest run sends tens or hundreds of messages, not
    thousands, and a long-lived SMTP connection in an async web process is a
    thing that has to be kept alive, re-established and shared across workers
    — complexity to buy throughput nobody here needs yet. Revisit when a run
    takes long enough to notice.
    """

    def __init__(self, config: EmailConfig) -> None:
        # Checked HERE so a bad deployment fails at startup, in front of an
        # operator, rather than on the first send.
        if not config.host:
            raise ValueError("MAIL_HOST is required when MAIL_PROVIDER=smtp")
        if not config.from_address:
            raise ValueError("MAIL_FROM is required when MAIL_PROVIDER=smtp")
        if config.username and not config.password:
            raise ValueError("MAIL_USERNAME is set but MAIL_PASSWORD is empty")
        self._config = config

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> None:
        cfg = self._config
        msg = _build(
            cfg, to=to, subject=subject, text=text, html=html, reply_to=reply_to
        )
        try:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s) as smtp:
                if cfg.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if cfg.username:
                    smtp.login(cfg.username, cfg.password)
                smtp.send_message(msg)
        except EmailError:
            raise
        except Exception as e:
            # The provider's own message is the useful part; the type alone is
            # not ("SMTPAuthenticationError" vs "the password is wrong").
            raise EmailError(f"Could not send mail via {cfg.host}: {e}") from e


def get_mailer(config: EmailConfig) -> Optional[Mailer]:
    """The mailer for this deployment, or None when mail is off.

    None is a real answer, not an error: most deployments send no mail, and the
    caller is expected to branch on it — mount the route, or do not.
    """
    provider = (config.provider or "").strip().lower()
    if not provider:
        return None
    if provider == "smtp":
        return SmtpMailer(config)
    if provider == "console":
        return ConsoleMailer(config)
    # A typo must not silently disable mail. Blank is how you turn it off, and
    # it is a different thing from "misspelled" — the latter would otherwise
    # deploy a service that quietly never emails anybody.
    raise ValueError(
        f"Unknown MAIL_PROVIDER {config.provider!r}. "
        f"Use one of {', '.join(PROVIDERS)}, or leave it blank to disable mail."
    )
