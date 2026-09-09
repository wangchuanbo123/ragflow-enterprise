"""Knowledge-graph schema, quality, review, merge, and hierarchy metadata."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class GraphSchemaDefinition(Base):
    __tablename__ = "graph_schema_definitions"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id", "version", name="uq_graph_schema_kb_version"
        ),
        Index(
            "uq_graph_schema_one_active_per_kind",
            "knowledge_base_id",
            "graph_kind",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    graph_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="general", server_default="general")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    entity_types_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    relation_types_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    graph_profiles_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    normalization_rules_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extraction_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    source_document_ids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    generation_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GraphExtractionCache(Base):
    __tablename__ = "graph_extraction_cache"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "cache_key", name="uq_graph_cache_kb_key"),
        Index("ix_graph_cache_last_used", "last_used_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class GraphQualityReport(Base):
    __tablename__ = "graph_quality_reports"
    __table_args__ = (Index("ix_graph_quality_kb_created", "knowledge_base_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    metrics_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    gate_results_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class KgEntityMerge(Base):
    __tablename__ = "kg_entity_merges"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "merged_entity_id", name="uq_kg_entity_merge_source"),
        Index("ix_kg_entity_merge_canonical", "canonical_entity_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    canonical_entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False)
    merged_entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reverted_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GraphReviewItem(Base):
    __tablename__ = "graph_review_items"
    __table_args__ = (Index("ix_graph_review_kb_status", "knowledge_base_id", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GraphCommunity(Base):
    __tablename__ = "graph_communities"
    __table_args__ = (Index("ix_graph_communities_kb_level", "knowledge_base_id", "level"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("graph_communities.id", ondelete="CASCADE"), nullable=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class GraphCommunityState(Base):
    """Deferred community lifecycle: empty -> stale -> ready (stale after graph edits)."""

    __tablename__ = "graph_community_states"
    __table_args__ = (UniqueConstraint("knowledge_base_id", name="uq_graph_community_state_kb"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="empty")
    communities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class GraphCommunityMember(Base):
    __tablename__ = "graph_community_members"
    __table_args__ = (UniqueConstraint("community_id", "entity_id", name="uq_graph_community_member"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    community_id: Mapped[str] = mapped_column(String(36), ForeignKey("graph_communities.id", ondelete="CASCADE"), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False)
