"""文档管理 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: str
    knowledge_base_id: str
    source_path: str
    folder_id: str | None = None
    original_filename: str
    file_hash: str
    file_size: int
    status: str
    chunk_count: int
    current_version_no: int
    metadata_json: dict | None = None
    index_schema_version: int
    embedding_provider: str
    embedding_model: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None = None

    model_config = {"from_attributes": True}


class IndexJobOut(BaseModel):
    id: str
    knowledge_base_id: str
    job_type: str
    status: str
    total_items: int
    processed_items: int
    succeeded_items: int
    failed_items: int
    error_message: str | None = None
    attempt_count: int
    max_attempts: int
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class IndexJobEventOut(BaseModel):
    id: int
    knowledge_base_id: str
    job_id: str
    event_type: str
    status: str
    processed_items: int
    total_items: int
    message: str | None = None
    payload_json: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SyncRequest(BaseModel):
    pass
