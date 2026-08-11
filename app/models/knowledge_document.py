"""知识文档元数据模型。"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id",
            "source_path",
            name="uq_knowledge_documents_kb_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        default=DEFAULT_KNOWLEDGE_BASE_ID,
        server_default=DEFAULT_KNOWLEDGE_BASE_ID,
        index=True,
        nullable=False,
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    folder_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_folders.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    index_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    embedding_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
