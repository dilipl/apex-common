"""The four auth routes: register, login, refresh, logout.

This was 84 code lines duplicated between two services with only import
ORDERING differing. The cookie handling in it is the most bug-prone part of the
system and has already been fixed twice — the localhost cookie-jar collision
and the cross-site SameSite pair — each time in two places by hand.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Response, status

from apex_common.auth.config import AuthConfig
from apex_common.auth.denylist import Denylist
from apex_common.auth.models import LoginUser, UserCreate, UserResponse
from apex_common.auth.users import UserStore


def set_auth_cookies(
    response: Response,
    config: AuthConfig,
    access_token: str,
    refresh_token: str | None = None,
) -> None:
    """Set the auth cookies.

    THE REFRESH TOKEN LIVES ONLY HERE. It is httpOnly, so no script can read
    it, and it is not returned in the login response body — which is what used
    to put a 30-day credential into localStorage. The ACCESS token is still
    returned in the body as well, because the client holds it in memory and
    sends it as a bearer header; it is short-lived and replaceable.

    `samesite` and `secure` come from config because the production topology is
    CROSS-SITE (web-….run.app -> task-….run.app) and a `lax` cookie is simply
    not sent on those requests. Locally the pair is same-site, so `lax` works
    and the production path cannot be exercised.

    `logged_in` is deliberately NOT httpOnly: it carries no authority and lets
    the frontend show a signed-in shell before its first refresh completes.
    """
    samesite = config.cookie_samesite.strip().lower()
    secure = config.cookie_secure
    domain = config.cookie_domain
    prefix = config.cookie_prefix
    max_age = config.access_token_expires_in * 60
    response.set_cookie(
        f"{prefix}access_token", access_token, max_age, max_age, "/", domain,
        secure, True, samesite,
    )
    if refresh_token:
        refresh_max = config.refresh_token_expires_in * 60
        response.set_cookie(
            f"{prefix}refresh_token", refresh_token, refresh_max, refresh_max,
            # Scoped to /auth, not "/": a path of "/" sent a 30-day credential
            # on every single API call, which is a wider blast radius for no
            # benefit. NOT /auth/refresh either — the browser would then not
            # send it to /auth/logout, and logout could not revoke it, which is
            # the whole point of the denylist.
            "/auth", domain, secure, True, samesite,
        )
    response.set_cookie(
        f"{prefix}logged_in", "True", max_age, max_age, "/", domain,
        secure, False, samesite,
    )


def build_auth_router(
    *,
    auth_jwt_cls: Any,
    config_provider: Callable[[], AuthConfig],
    users: UserStore,
    denylist: Denylist,
) -> APIRouter:
    """Build /auth for one service.

    `config_provider` is called per request rather than captured, so a setting
    changed after import (which tests do constantly) is honoured.
    """
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/register", response_model=UserResponse)
    async def register(user: UserCreate):
        try:
            return users.register(user)
        except ValueError as e:
            if "already registered" in str(e).lower():
                raise HTTPException(status_code=400, detail="Email already registered")
            if "password" in str(e).lower():
                raise HTTPException(status_code=400, detail="Passwords do not match")
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/login", response_model=UserResponse)
    async def login(
        user: LoginUser, response: Response, Authorize: Any = Depends(auth_jwt_cls)
    ):
        config = config_provider()
        db_user = users.validate_login(user.email, user.password)
        if not db_user:
            # One message for a bad address and a bad password alike.
            raise HTTPException(status_code=400, detail="Invalid credentials")

        access_token = Authorize.create_access_token(
            subject=str(db_user["_id"]),
            expires_time=timedelta(minutes=config.access_token_expires_in),
        )
        refresh_token = Authorize.create_refresh_token(
            subject=str(db_user["_id"]),
            expires_time=timedelta(minutes=config.refresh_token_expires_in),
        )
        set_auth_cookies(response, config, access_token, refresh_token)

        return UserResponse(
            status="success",
            id=str(db_user["_id"]),
            name=db_user["name"],
            email=db_user["email"],
            role=db_user["role"],
            is_active=db_user["is_active"],
            created_at=db_user["created_at"],
            updated_at=db_user["updated_at"],
            verified=db_user.get("verified", False),
            access_token=access_token,
            # refresh_token is DELIBERATELY ABSENT. It goes out as an httpOnly
            # cookie above and nothing else; returning it here is what put a
            # 30-day credential in localStorage, where any script could read it
            # and nothing could revoke it.
        )

    @router.get("/refresh")
    async def refresh(response: Response, Authorize: Any = Depends(auth_jwt_cls)):
        config = config_provider()
        try:
            Authorize.jwt_refresh_token_required()
            user_id = Authorize.get_jwt_subject()
            if not user_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            user = users.get_user_by_id(user_id)
            if not user:
                raise HTTPException(status_code=401, detail="Unauthorized")

            access_token = Authorize.create_access_token(
                subject=str(user["_id"]),
                expires_time=timedelta(minutes=config.access_token_expires_in),
            )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            error = e.__class__.__name__
            if error == "MissingTokenError":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Please provide refresh token",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token is invalid or has expired",
            )

        set_auth_cookies(response, config, access_token)
        return {"access_token": access_token}

    @router.get("/logout", status_code=status.HTTP_200_OK)
    async def logout(response: Response, Authorize: Any = Depends(auth_jwt_cls)):
        """Clear the cookies AND revoke the tokens.

        Clearing cookies alone was not logging anyone out: a JWT is valid
        because it is signed, so the refresh token stayed usable for its full
        30 days no matter what the browser did with its copy. Both tokens are
        now recorded in the denylist, which every authenticated request
        consults.

        STILL DOES NOT REQUIRE AUTHENTICATION. Logout must work with an expired
        or broken token — refusing it would leave the user stuck signed in. So
        each token is revoked only if it is present and parseable, and anything
        missing is skipped silently.
        """
        for kind in ("access", "refresh"):
            try:
                if kind == "access":
                    Authorize.jwt_optional()
                else:
                    Authorize.jwt_refresh_token_required()
                raw = Authorize.get_raw_jwt() or {}
            except Exception:  # noqa: BLE001 — absent or unparseable
                continue
            exp = raw.get("exp")
            denylist.revoke(
                raw.get("jti"),
                datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None,
                user_id=str(raw.get("sub") or ""),
                reason="logout",
            )

        prefix = config_provider().cookie_prefix
        Authorize.unset_jwt_cookies(response)
        response.delete_cookie(f"{prefix}logged_in")
        # The refresh cookie is scoped to /auth, so it must be deleted with the
        # SAME path — a delete at "/" leaves the browser holding it.
        response.delete_cookie(f"{prefix}refresh_token", path="/auth")
        return {"status": "success"}

    return router
