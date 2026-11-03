"""Shared authentication: one implementation, one realm per service.

WHY THIS IS SHARED
    ingestor-service and task-service-agent each held ~394 code lines of auth
    that were ~94% identical — the register/login/refresh/logout routes were
    byte-identical logic differing only in import order. Both read the SAME
    `users` collection. They are two JWT REALMS, not two implementations, and
    they differ in exactly two settings: `cookie_prefix` and `jwt_secret_key`.

    The copies had already drifted in three ways by 2026-11-03: a stray
    `print()` in one service's auth exception path, one 401 message spelled two
    ways, and a docstring claiming a refresh token lasted twelve hours when
    both services set 43200 minutes. The last is the shape that matters — a
    duplicated file whose comment confidently describes the other copy, wrong,
    about how long a leaked credential lives.

WHAT IS NOT HERE
    `require_project_member` / `require_project_owner` — those read a service's
    own records (`memberships`, project ownership) and belong to its owner.

THE SEAM
    This package does NOT import fastapi_jwt_auth. The consumer passes its
    `AuthJWT` class in. Two reasons: the service keeps ownership of that
    dependency's version (it is a git dependency pinned to HEAD upstream), and
    a service that needs no auth at all — heavy-processing-engine is a private
    worker — does not drag fastapi-jwt-auth and bcrypt in through a library it
    uses for log formatting.

    `AuthJWT` carries PROCESS-GLOBAL configuration, registered through class
    decorators. `install()` performs that registration, so calling it is a
    process-level act: once, at import of the service's auth module.

USAGE — a service's app/auth/oauth2.py becomes this
    from fastapi_jwt_auth import AuthJWT
    from apex_common.auth import AuthConfig, install

    def _config() -> AuthConfig:
        s = get_settings()
        return AuthConfig(jwt_algorithm=s.jwt_algorithm, ..., cookie_prefix=s.cookie_prefix)

    bundle = install(
        auth_jwt_cls=AuthJWT,
        config_provider=_config,
        users=get_users_collection,
        denylist_collection=lambda: get_database().get_collection(DENYLIST_COLLECTION),
    )
    require_user = bundle.require_user
    get_current_user = bundle.get_current_user
    require_admin = bundle.require_admin
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends

from apex_common.auth.config import AuthConfig, build_authjwt_settings
from apex_common.auth.denylist import COLLECTION as DENYLIST_COLLECTION
from apex_common.auth.denylist import Denylist
from apex_common.auth.dependencies import (
    ADMIN_ROLE,
    NotVerified,
    UserNotFound,
    make_require_admin,
    make_require_user,
)
from apex_common.auth.models import (
    LoginUser,
    PyObjectId,
    User,
    UserBase,
    UserCreate,
    UserResponse,
)
from apex_common.auth.passwords import hash_password, verify_password
from apex_common.auth.router import build_auth_router, set_auth_cookies
from apex_common.auth.users import UserStore

__all__ = [
    "ADMIN_ROLE", "AuthBundle", "AuthConfig", "DENYLIST_COLLECTION", "Denylist",
    "LoginUser", "NotVerified", "PyObjectId", "User", "UserBase", "UserCreate",
    "UserNotFound", "UserResponse", "UserStore", "hash_password", "install",
    "set_auth_cookies", "verify_password",
]


@dataclass(frozen=True)
class AuthBundle:
    """Everything a service needs to wire auth up, built for its own realm."""

    router: Any                       # APIRouter — mount at the app root
    require_user: Callable[..., str]
    get_current_user: Callable[..., str]
    require_admin: Callable[..., str]
    users: UserStore
    denylist: Denylist
    config_provider: Callable[[], AuthConfig]


def install(
    *,
    auth_jwt_cls: Any,
    config_provider: Callable[[], AuthConfig],
    users: Callable[[], Any],
    denylist_collection: Callable[[], Any],
) -> AuthBundle:
    """Configure `auth_jwt_cls` for this service and build its auth surface.

    REGISTERS PROCESS-GLOBAL STATE on `auth_jwt_cls` (its config loader and its
    denylist loader), because that is the only interface fastapi-jwt-auth
    offers. Call it once, from the module that owns a service's auth.

    `config_provider` is read ONCE here for the fastapi-jwt-auth settings (that
    library reads its config at decoration time and never again), and PER
    REQUEST by the router for the cookie attributes it sets itself. The cookie
    policy guard therefore fires at startup — which is what it is for.
    """
    config = config_provider()

    # BEFORE the loader is registered, and deliberately OUTSIDE it.
    #
    # fastapi-jwt-auth's load_config wraps its callback in
    # `except Exception: raise TypeError("Config must be pydantic 'BaseSettings'
    # or list of tuple")` (fastapi_jwt_auth/auth_config.py). So a guard raised
    # from inside the callback is SWALLOWED and the operator sees a complaint
    # about the config object's type instead of the cookie settings that are
    # actually wrong — which defeats the entire purpose of a guard whose job is
    # to turn a mystifying outage into a message. Both services shipped it
    # inside the callback and therefore never showed this message.
    config.check_cookie_policy()

    denylist = Denylist(
        denylist_collection,
        default_ttl_minutes=config.refresh_token_expires_in,
    )
    store = UserStore(users)

    @auth_jwt_cls.load_config
    def _get_config():  # noqa: ANN202 — library callback
        """Build the library's settings.

        CALLED ONCE, at decoration — fastapi-jwt-auth invokes this callback
        immediately and copies the result onto class attributes. See
        apex_common/auth/config.py, "WHEN THE PROVIDER IS READ".
        """
        return build_authjwt_settings(config_provider())

    @auth_jwt_cls.token_in_denylist_loader
    def _is_revoked(decrypted_token) -> bool:  # noqa: ANN001 — library signature
        """Consulted on every authenticated request. Fails OPEN — see
        apex_common/auth/denylist.py for why that trade was made."""
        return denylist.is_revoked((decrypted_token or {}).get("jti"))

    require_user = make_require_user(auth_jwt_cls=auth_jwt_cls, users=users)
    require_admin = make_require_admin(require_user=require_user, users=users)

    # A separate name from require_user, kept because every route in both
    # services already depends on `get_current_user` by that name and the two
    # read differently at a call site: "who is this" vs "there must be a user".
    def get_current_user(user_id: str = Depends(require_user)) -> str:
        return user_id

    router = build_auth_router(
        auth_jwt_cls=auth_jwt_cls,
        config_provider=config_provider,
        users=store,
        denylist=denylist,
    )
    return AuthBundle(
        router=router,
        require_user=require_user,
        get_current_user=get_current_user,
        require_admin=require_admin,
        users=store,
        denylist=denylist,
        config_provider=config_provider,
    )
