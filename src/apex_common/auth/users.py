"""Register, credential validation, and user lookup over the `users` collection.

Both services read and write the SAME collection, so this logic had to agree
between them and did — it was copied. The collection arrives as a callable for
the same reason as everywhere else here: at import time there is no database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from bson import ObjectId

from apex_common.auth.models import UserCreate, UserResponse
from apex_common.auth.passwords import hash_password, verify_password


class UserStore:
    def __init__(self, collection: Callable[[], Any]) -> None:
        self._collection = collection

    def register(self, user: UserCreate) -> UserResponse:
        """Create a new user. Raises ValueError if the email exists or the
        passwords do not match."""
        coll = self._collection()
        if coll.find_one({"email": user.email.lower()}):
            raise ValueError("Email already registered")
        if user.password != user.password_confirm:
            raise ValueError("Passwords do not match")

        user_data = {
            "name": user.name,
            "email": user.email.lower(),
            "password": hash_password(user.password),
            "role": "user",
            "is_active": True,
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "verified": True,
        }
        result = coll.insert_one(user_data)
        return UserResponse(
            status="success",
            id=str(result.inserted_id),
            name=user_data["name"],
            email=user_data["email"],
            role=user_data["role"],
            is_active=user_data["is_active"],
            created_at=user_data["created_at"],
            updated_at=user_data["updated_at"],
            verified=user_data["verified"],
        )

    def validate_login(self, email: str, password: str) -> dict | None:
        """Validate credentials. Returns the user dict if valid, None otherwise.

        One return value for "no such email" and "wrong password" on purpose:
        the caller answers both with the same 400, so a login form cannot be
        used to enumerate which addresses are registered.
        """
        db_user = self._collection().find_one({"email": email.lower()})
        if not db_user:
            return None
        if not verify_password(password, db_user["password"]):
            return None
        return db_user

    def get_user_by_id(self, user_id: str) -> dict | None:
        """Get a user by id (from the JWT subject). None if not found, and also
        None for a malformed id — a bad `sub` is an invalid token, not a 500."""
        try:
            return self._collection().find_one({"_id": ObjectId(user_id)})
        except Exception:  # noqa: BLE001
            return None
