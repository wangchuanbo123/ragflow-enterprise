"""Backup API schemas."""

from datetime import datetime

from pydantic import BaseModel, Field


class BackupCreate(BaseModel):
    backup_type: str
    document_id: str | None = None
    label: str | None = Field(default=None, max_length=255)


class BackupOut(BaseModel):
    id: str
    knowledge_base_id: str
    backup_type: str
    scope_key: str
    document_id: str | None
    version_no: int
    label: str | None
    file_size: int
    status: str
    trigger: str
    manifest_json: dict | None
    created_at: datetime
    restored_at: datetime | None

    model_config = {"from_attributes": True}


class BackupSettingsOut(BaseModel):
    monthly_enabled: bool
    interval_days: int
    retention_versions: int
