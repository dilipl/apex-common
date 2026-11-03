"""The FastAPI dependencies every authenticated route hangs off.

FACTORIES, NOT MODULE-LEVEL FUNCTIONS. A dependency needs the users collection
and the service's AuthJWT class, and reaching for those through module globals
is what made the per-service copies necessary in the first place. Each service
builds them once (its `app/auth/oauth2.py` is now that wiring) and re-exports,
so existing call sites — `Depends(get_current_user)` — do not change.

`require_project_member` and `require_project_owner` are deliberately NOT here:
they are about a service's own domain (memberships, project ownership) and
belong to whichever service owns those records.
"""

from __future__ import annotations

from typing import Any, Callable

from bson import ObjectId
from fastapi import Depends, HTTPException, status

# Global roles live on the user document (`users.role`) — the SAME Mongo
# collection every service reads, so a single grant makes a user an admin
# everywhere. Distinct from any per-project role in `memberships`.
ADMIN_ROLE = "admin"


class NotVerified(Exception):
    pass


class UserNotFound(Exception):
    pass


def make_require_user(
    *,
    auth_jwt_cls: Any,
    users: Callable[[], Any],
) -> Callable[..., str]:
    """Build the dependency that turns a token into a user id.

    Returns the user_id (the JWT subject), not the user document: nearly every
    caller wants only the id, and returning the document would invite handlers
    to trust a snapshot read at dependency time.
    """

    def require_user(Authorize: Any = Depends(auth_jwt_cls)) -> str:
        try:
            Authorize.jwt_required()
            user_id = Authorize.get_jwt_subject()
            user = users().find_one({"_id": ObjectId(user_id)})
            if not user:
                raise UserNotFound("User not found")
            if not user["is_active"]:
                raise NotVerified("User not verified")
        except Exception as e:  # noqa: BLE001 — library raises by class name
            error = e.__class__.__name__
            if error == "MissingTokenError":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="You are not logged in",
                )
            if error == "UserNotFound":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User no longer exists",
                )
            if error == "NotVerified":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Please verify your account",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token is invalid or has expired",
            )
        return user_id

    return require_user


def make_require_admin(
    *,
    require_user: Callable[..., str],
    users: Callable[[], Any],
) -> Callable[..., str]:
    """Build the dependency that gates a route on the global admin role.

    Read from Mongo on EVERY request rather than carried as a JWT claim: that
    costs one indexed lookup and buys immediate revocation, where a role baked
    into a token stays valid until the token expires.

    Hiding a nav item in the frontend is not access control — every admin route
    must depend on this.
    """

    def require_admin(user_id: str = Depends(require_user)) -> str:
        user = users().find_one({"_id": ObjectId(user_id)})
        if not user or user.get("role") != ADMIN_ROLE:
            # Same 403 whether the role is missing or the user is: an admin
            # surface should not confirm what it is guarding.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access required",
            )
        return user_id

    return require_admin
