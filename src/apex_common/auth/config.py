"""What a service must tell the shared auth code about itself.

EVERY FIELD HERE IS A PLACE THE TWO SERVICES LEGITIMATELY DIFFER. There are
only two that actually do today — `cookie_prefix` and `jwt_secret_key` — and
those two are the entire reason this could not be one deployment: they are
separate auth REALMS over one `users` collection.

WHEN THE PROVIDER IS READ, EXACTLY
    `config_provider` is a callable, and it is read at two different times:

      * ONCE, at install(), for the fastapi-jwt-auth settings. That library's
        `load_config` invokes its callback at DECORATION time and copies the
        values onto class attributes (fastapi_jwt_auth/auth_config.py) — it is
        never consulted per request. So the signing key, the cookie key names
        and the denylist switches are fixed for the process, and
        `check_cookie_policy` is therefore a STARTUP guard. That is what it is
        for: failing at boot turns a mystifying production outage into a
        message.
      * PER REQUEST, by the auth router, for the cookie attributes it sets
        itself. That is where a callable genuinely earns its keep.

    The per-service copies carried a comment claiming the guard read config "at
    CALL time, not the module-level snapshot taken at import". Both readings
    happen at import, so that distinction was not real; the guard is still
    correct, for a different reason than the comment gave.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthConfig:
    # --- token signing -----------------------------------------------------
    jwt_algorithm: str = "HS256"
    jwt_secret_key: str = ""          # HS*
    jwt_public_key: str = ""          # RS*
    jwt_private_key: str = ""         # RS*

    # --- cookies -----------------------------------------------------------
    # Namespaced per service. Cookies are scoped by host and path and IGNORE
    # THE PORT, so on localhost two services are one cookie jar: unprefixed
    # names clobber each other, and the loser's refresh then arrives signed by
    # the other service's key. Production hosts differ and would not collide,
    # which is the worse half — it only breaks where it is developed.
    cookie_prefix: str = ""
    cookie_samesite: str = "lax"
    cookie_secure: bool = False
    cookie_domain: str | None = None

    # --- lifetimes (minutes) ----------------------------------------------
    access_token_expires_in: int = 60
    refresh_token_expires_in: int = 43200

    def check_cookie_policy(self) -> None:
        """Refuse a combination the browser would silently discard.

        `SameSite=None` without `Secure` is REJECTED by every current browser,
        so the cookie is never stored and every request arrives
        unauthenticated — with no error anywhere to explain it. Failing at
        startup turns a mystifying production outage into a message.
        """
        if self.cookie_samesite.strip().lower() == "none" and not self.cookie_secure:
            raise RuntimeError(
                "COOKIE_SAMESITE=none requires COOKIE_SECURE=true — browsers "
                "reject the combination and the auth cookies would never be stored."
            )


def load_pem_key(key_value: str, key_type: str) -> str:
    """Load a PEM key from config. Accepts raw PEM, base64-encoded PEM, or
    base64 of the PEM body."""
    import base64

    if not key_value or not key_value.strip():
        return ""
    key_value = key_value.strip()
    key_clean = key_value.replace("\n", "").replace("\r", "").replace(" ", "")

    if key_value.startswith("-----BEGIN"):
        return key_value.replace("\\n", "\n")

    try:
        decoded = base64.b64decode(key_clean)
        text = decoded.decode("utf-8")
        if text.startswith("-----BEGIN"):
            return text
        if text.startswith("MI"):
            lines = [text[i : i + 64] for i in range(0, len(text), 64)]
            return (
                f"-----BEGIN {key_type}-----\n"
                + "\n".join(lines)
                + f"\n-----END {key_type}-----\n"
            )
    except Exception:  # noqa: BLE001 — not base64; fall through to wrapping
        pass
    lines = [key_clean[i : i + 64] for i in range(0, len(key_clean), 64)]
    return (
        f"-----BEGIN {key_type}-----\n"
        + "\n".join(lines)
        + f"\n-----END {key_type}-----\n"
    )


def jwt_keys(config: AuthConfig) -> dict[str, str | None]:
    """The three key fields fastapi-jwt-auth wants, resolved for the algorithm."""
    algo = (config.jwt_algorithm or "HS256").upper()
    if algo in ("HS256", "HS384", "HS512"):
        return {
            "authjwt_secret_key": config.jwt_secret_key or "change-me-in-production",
            "authjwt_public_key": None,
            "authjwt_private_key": None,
        }
    return {
        "authjwt_secret_key": None,
        "authjwt_public_key": load_pem_key(config.jwt_public_key, "PUBLIC KEY"),
        "authjwt_private_key": load_pem_key(config.jwt_private_key, "PRIVATE KEY"),
    }


def authjwt_settings_dict(config: AuthConfig) -> dict:
    """The full fastapi-jwt-auth settings mapping.

    Returned as a dict rather than a pydantic model because the library wants a
    model instance whose CLASS it does not care about, and building that model
    is the consumer's one line — it keeps this package free of a
    fastapi-jwt-auth import. See `apex_common.auth.install`.
    """
    keys = jwt_keys(config)
    algo = config.jwt_algorithm or "HS256"
    return {
        "authjwt_algorithm": algo,
        "authjwt_decode_algorithms": [algo],
        "authjwt_token_location": {"cookies", "headers"},
        "authjwt_access_cookie_key": f"{config.cookie_prefix}access_token",
        "authjwt_refresh_cookie_key": f"{config.cookie_prefix}refresh_token",
        "authjwt_cookie_csrf_protect": False,
        "authjwt_cookie_samesite": config.cookie_samesite,
        "authjwt_cookie_secure": config.cookie_secure,
        "authjwt_cookie_domain": config.cookie_domain,
        # Revocation. Without this, logout cleared the browser's copy and the
        # token itself stayed valid — 30 days, for a refresh token.
        "authjwt_denylist_enabled": True,
        "authjwt_denylist_token_checks": {"access", "refresh"},
        **keys,
    }


def build_authjwt_settings(config: AuthConfig):
    """The settings OBJECT fastapi-jwt-auth's `load_config` must return.

    The library wants an object it can read attributes off and does not inspect
    the class, so this builds one from `authjwt_settings_dict` — one source of
    truth for the mapping, rather than a hand-declared model that could fall out
    of step with it.
    """
    from pydantic import BaseModel, ConfigDict

    fields = authjwt_settings_dict(config)
    return type(
        "AuthJWTSettings",
        (BaseModel,),
        {
            "__annotations__": {k: Any for k in fields},
            "model_config": ConfigDict(arbitrary_types_allowed=True),
            **fields,
        },
    )()
