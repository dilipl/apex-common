"""Revoking a token before it expires.

WHY LOGOUT DID NOT LOG YOU OUT
    A JWT is valid because it is signed, not because a server remembers it. So
    `GET /auth/logout` cleared the browser's copy and nothing else: the token
    itself stayed valid until it expired. For the ACCESS token that is a
    30-60 minute window and arguably fine. For the REFRESH token it is THIRTY
    DAYS (REFRESH_TOKEN_EXPIRES_IN=43200), and a refresh token is a credential
    that mints access tokens on demand.

    Before this, a refresh token that leaked — a shared laptop, a copied
    localStorage value, a browser extension — could not be invalidated at all
    except by rotating JWT_SECRET_KEY, which logs out every user of the
    service. "Log out everyone to revoke one person" is not a usable control,
    which in practice meant nothing was ever revoked.

HOW
    Every token fastapi-jwt-auth mints carries a `jti`. Logout records that jti
    here with the token's own expiry, and `token_in_denylist_loader` consults
    this on every request. One indexed lookup by `_id`.

    SYNCHRONOUS ON PURPOSE. fastapi-jwt-auth calls the denylist loader from
    inside its synchronous request verification, so an async check cannot be
    awaited there. The injected collection must therefore be a SYNC pymongo
    collection, and the lookup must stay a single indexed find_one — it runs on
    the event loop thread.

WHY A TTL INDEX AND NOT A SWEEP
    The row is only needed until the token would have expired anyway — after
    that the signature check rejects it regardless. Mongo's TTL monitor deletes
    it on an ABSOLUTE `expires_at`, so the collection stays the size of the
    currently-revoked set rather than growing forever.

FAILS OPEN, DELIBERATELY, AND THIS IS A REAL TRADE
    If Mongo is unreachable the check returns False — "not revoked" — so a
    database blip does not log out every user of a working service. The cost is
    that during such a blip a revoked token works again. Chosen because the
    alternative fails CLOSED on an outage: a Mongo hiccup becomes a total
    outage for everyone, to defend against a window that also requires an
    attacker to hold a stolen token at that exact moment.

    THE SAME RULE COVERS "NEVER WIRED UP". A Denylist built with a getter that
    raises, returns None, or was never configured also reports "not revoked".
    That is a second route to the same fail-open path and it is deliberate: a
    boot-order mistake must not reject every request in the service. It is
    logged at WARNING on every call, and `ensure_indexes` logs it too, so the
    condition is visible rather than silent.

    If that trade is ever wrong for a deployment, `is_revoked` is the one
    function to change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

COLLECTION = "revoked_tokens"


class Denylist:
    """Revocation over one sync Mongo collection.

    `collection` is a CALLABLE, not a collection: at import time the database
    does not exist yet, and in tests it is swapped per case. It may return None
    (no database configured) and may raise — both mean fail open.
    """

    def __init__(
        self,
        collection: Callable[[], Any],
        *,
        default_ttl_minutes: int,
    ) -> None:
        self._collection = collection
        self._default_ttl_minutes = default_ttl_minutes

    def _coll(self) -> Any | None:
        try:
            return self._collection()
        except Exception:  # noqa: BLE001 — no database configured, or boot order
            return None

    def ensure_indexes(self) -> None:
        """TTL on the absolute expiry, so rows retire themselves.

        expireAfterSeconds=0 on a DATE field means "delete when that date
        passes" — not "delete immediately". An offset from a creation date would
        instead re-date every existing row the moment the token lifetime config
        changed.
        """
        coll = self._coll()
        if coll is None:
            logger.warning(
                "Denylist has no collection; the TTL index was not created and "
                "revocation checks will fail OPEN"
            )
            return
        try:
            coll.create_index([("expires_at", 1)], expireAfterSeconds=0, background=True)
        except Exception:  # noqa: BLE001 — a missing index must not stop startup
            logger.warning("Could not create the revoked-token TTL index", exc_info=True)

    def revoke(
        self,
        jti: Optional[str],
        expires_at: Optional[datetime],
        *,
        user_id: str = "",
        reason: str = "logout",
    ) -> bool:
        """Record a token as revoked. Returns whether it was stored.

        Idempotent: logging out twice records the same jti once.
        """
        coll = self._coll()
        if coll is None or not jti:
            return False
        # Without a usable expiry the row would never retire. Fall back to the
        # longest token life rather than storing it forever.
        if not isinstance(expires_at, datetime):
            expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=self._default_ttl_minutes
            )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        try:
            coll.update_one(
                {"_id": jti},
                {"$set": {"expires_at": expires_at, "user_id": user_id,
                          "reason": reason, "revoked_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            return True
        except Exception:  # noqa: BLE001
            # Logout must still succeed from the user's point of view — the
            # cookies are cleared either way. Log loudly: a revocation that
            # silently did not happen is worth knowing about.
            logger.exception("Could not record a revoked token (jti=%s)", jti)
            return False

    def is_revoked(self, jti: Optional[str]) -> bool:
        """Whether this token has been revoked. Fails OPEN — see the module
        docstring, including for the never-configured case."""
        coll = self._coll()
        if coll is None:
            if jti:
                logger.warning(
                    "Denylist unreachable or unconfigured; treating the token as "
                    "valid (fail open)"
                )
            return False
        if not jti:
            return False
        try:
            return coll.find_one({"_id": jti}, {"_id": 1}) is not None
        except Exception:  # noqa: BLE001
            logger.warning("Denylist unreachable; treating the token as valid",
                           exc_info=True)
            return False
