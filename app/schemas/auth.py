"""鉴权相关 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    registration_status: Literal["pending", "approved", "rejected"]
    registration_reviewed_by: str | None
    registration_reviewed_at: datetime | None
    registration_review_note: str
    created_at: datetime

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    user: UserOut


class RegistrationRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=72)
    display_name: str = Field(default="", max_length=64)

    model_config = ConfigDict(extra="forbid")


class RegistrationResponse(BaseModel):
    message: str
    registration_status: Literal["pending"] = "pending"


class RegistrationReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)

    model_config = ConfigDict(extra="forbid")


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=72)
    display_name: str = Field(default="", max_length=64)
    role: Literal["admin", "user"] = "user"
    is_active: bool = True


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=72)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=72)
