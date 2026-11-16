"""鉴权相关 Pydantic 模型。"""

from pydantic import BaseModel, Field
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

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    user: UserOut


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
