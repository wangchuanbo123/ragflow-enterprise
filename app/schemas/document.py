"""文档管理 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: str
    source_path: str
    original_filename: str
    file_hash: str
    file_size: int
    status: str
    chunk_count: int
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
    job_type: str
    status: str
    total_items: int
    processed_items: int
    succeeded_items: int
    failed_items: int
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class SyncRequest(BaseModel):
    pass
