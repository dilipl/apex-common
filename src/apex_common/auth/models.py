"""The user model shared by every service that authenticates against `users`.

These were duplicated per service with only import ORDER differing. They have
to agree: both services read and write the same `users` collection, so a field
one of them did not know about is a field it would drop on write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from bson import ObjectId
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    constr,
)

PyObjectId = Annotated[
    str,
    BeforeValidator(str),
    PlainSerializer(lambda x: str(x), return_type=str, when_used="always"),
]


class UserBase(BaseModel):
    name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UserCreate(UserBase):
    password: constr(min_length=8)
    password_confirm: str
    verified: bool = False


class LoginUser(BaseModel):
    email: str
    password: constr(min_length=8)


class User(UserBase):
    id: PyObjectId = Field(default_factory=ObjectId, alias="_id")
    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


class UserResponse(UserBase):
    status: str
    id: str | None = None
    verified: bool | None = None
    access_token: str | None = None
    # refresh_token is part of the SHAPE but is never populated by the login
    # route — it goes out as an httpOnly cookie and nothing else. Kept on the
    # model because a client may still deserialise an older response.
    refresh_token: str | None = None
