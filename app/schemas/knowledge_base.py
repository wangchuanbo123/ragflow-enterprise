"""Knowledge-base API schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1000)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=3, max_length=64)
    description: str | None = Field(default=None, max_length=1000)


class KnowledgeBaseCapabilities(BaseModel):
    can_read: bool = False
    can_edit: bool = False
    can_manage_members: bool = False
    can_archive: bool = False


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    owner_id: str | None = None
    is_default: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    current_user_role: str | None = None
    capabilities: KnowledgeBaseCapabilities = Field(default_factory=KnowledgeBaseCapabilities)

    model_config = {"from_attributes": True}


class KnowledgeBaseMemberUpsert(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    role: Literal["viewer", "editor"] = "viewer"


class KnowledgeBaseMemberOut(BaseModel):
    id: str
    user_id: str
    username: str
    display_name: str
    role: str
    created_at: datetime


class DataSourceCreate(BaseModel):
    source_type: Literal["upload", "local_directory", "git", "s3"]
    name: str = Field(min_length=1, max_length=128)
    connection_ref: str | None = Field(default=None, max_length=255)
    config: dict = Field(default_factory=dict)
    sync_interval_minutes: int | None = Field(default=None, ge=1, le=43200)
    deletion_policy: Literal["ignore", "delete"] = "ignore"

    @field_validator("config")
    @classmethod
    def reject_inline_secrets(cls, value: dict) -> dict:
        forbidden = {"password", "secret", "secret_key", "access_key", "api_key", "token"}
        def contains_secret(item) -> bool:
            if isinstance(item, dict):
                return any(str(key).lower() in forbidden or contains_secret(child) for key, child in item.items())
            return isinstance(item, list) and any(contains_secret(child) for child in item)
        if contains_secret(value):
            raise ValueError("敏感凭据不能直接写入配置，请使用 connection_ref")
        return value


class DataSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection_ref: str | None = Field(default=None, max_length=255)
    config: dict | None = None
    status: Literal["active", "disabled"] | None = None
    sync_interval_minutes: int | None = Field(default=None, ge=1, le=43200)
    deletion_policy: Literal["ignore", "delete"] | None = None

    @field_validator("config")
    @classmethod
    def reject_inline_secrets(cls, value: dict | None) -> dict | None:
        if value is None:
            return value
        return DataSourceCreate.reject_inline_secrets(value)


class DataSourceOut(BaseModel):
    id: str
    knowledge_base_id: str
    source_type: str
    name: str
    connection_ref: str | None = None
    config: dict | None = Field(default=None, validation_alias="config_json")
    status: str
    health_status: str | None = None
    health_detail: str | None = None
    last_checked_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_detail: str | None = None
    last_sync_job_id: str | None = None
    last_synced_at: datetime | None = None
    sync_interval_minutes: int | None = None
    next_sync_at: datetime | None = None
    deletion_policy: str = "ignore"
    sync_cursor_json: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DataSourceSyncOut(BaseModel):
    source_id: str
    job_id: str
    status: str
    accepted: int = 0
    skipped: int = 0
    rejected: list[dict] = Field(default_factory=list)


class ConnectorCredentialOut(BaseModel):
    id: str
    connection_ref: str
    source_type: Literal["git", "s3"]
    allowed_locations: list[str]
