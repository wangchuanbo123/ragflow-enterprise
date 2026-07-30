"""知识图谱 ORM 模型：实体、别名、提及、关系、证据。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KgEntity(Base):
    __tablename__ = "kg_entities"
    __table_args__ = (
        UniqueConstraint("entity_type", "normalized_name", name="uq_kg_entity_type_norm"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KgAlias(Base):
    __tablename__ = "kg_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "normalized_alias", name="uq_kg_alias_entity_norm"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), index=True, nullable=False)


class KgEntityMention(Base):
    __tablename__ = "kg_entity_mentions"
    __table_args__ = (
        UniqueConstraint("entity_id", "chunk_id", name="uq_kg_mention_entity_chunk"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_chunks.id", ondelete="CASCADE"), nullable=False
    )
    mention: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class KgRelation(Base):
    __tablename__ = "kg_relations"
    __table_args__ = (
        UniqueConstraint("subject_entity_id", "predicate", "object_entity_id", name="uq_kg_relation_triple"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subject_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kg_entities.id"), index=True, nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(32), nullable=False)
    object_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kg_entities.id"), index=True, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KgRelationEvidence(Base):
    __tablename__ = "kg_relation_evidence"
    __table_args__ = (
        UniqueConstraint("relation_id", "chunk_id", "quote_hash", name="uq_kg_evidence_rel_chunk_quote"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    relation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kg_relations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_chunks.id", ondelete="CASCADE"), nullable=False
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
