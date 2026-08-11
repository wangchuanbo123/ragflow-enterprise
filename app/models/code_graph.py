"""Versioned code snapshots, symbols, and graph relations."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class CodeSnapshot(Base):
    __tablename__ = "code_snapshots"
    __table_args__ = (Index("ix_code_snapshots_kb_status", "knowledge_base_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    data_source_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("knowledge_data_sources.id", ondelete="SET NULL"), nullable=True)
    revision: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="building")
    language_stats_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CodeFile(Base):
    __tablename__ = "code_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path", name="uq_code_file_snapshot_path"),
        Index("ix_code_files_kb_language", "knowledge_base_id", "language"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_snapshots.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    parse_errors_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class CodeSymbol(Base):
    __tablename__ = "code_symbols"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "stable_key", name="uq_code_symbol_snapshot_key"),
        Index("ix_code_symbols_kb_name", "knowledge_base_id", "name"),
        Index("ix_code_symbols_snapshot_kind", "snapshot_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_snapshots.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False)
    parent_symbol_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("code_symbols.id", ondelete="SET NULL"), nullable=True)
    stable_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class CodeRelation(Base):
    __tablename__ = "code_relations"
    __table_args__ = (
        Index("ix_code_relations_snapshot_type", "snapshot_id", "relation_type"),
        Index("ix_code_relations_subject", "subject_symbol_id"),
        Index("ix_code_relations_target", "target_symbol_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_snapshots.id", ondelete="CASCADE"), nullable=False)
    subject_symbol_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_symbols.id", ondelete="CASCADE"), nullable=False)
    target_symbol_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("code_symbols.id", ondelete="SET NULL"), nullable=True)
    target_qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unresolved")
    evidence_path: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_start_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    evidence_end_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class EntityVectorIndexState(Base):
    __tablename__ = "entity_vector_index_state"
    __table_args__ = (UniqueConstraint("knowledge_base_id", name="uq_entity_vector_state_kb"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    index_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    graph_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="empty")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
