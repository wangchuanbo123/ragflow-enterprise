"""add code graph, graph governance, and connector scheduling

Revision ID: a38c7d9e0f14
Revises: f27a6b8c9d03
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "a38c7d9e0f14"
down_revision = "f27a6b8c9d03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.alter_column(
            "job_type",
            existing_type=sa.String(length=20),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
    with op.batch_alter_table("knowledge_data_sources") as batch:
        batch.add_column(sa.Column("sync_interval_minutes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("next_sync_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("deletion_policy", sa.String(20), nullable=False, server_default="ignore"))
        batch.add_column(sa.Column("sync_cursor_json", sa.JSON(), nullable=True))
        batch.create_index("ix_knowledge_data_sources_next_sync_at", ["next_sync_at"])

    op.create_table(
        "code_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data_source_id", sa.String(36), sa.ForeignKey("knowledge_data_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revision", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("language_stats_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_code_snapshots_kb_status", "code_snapshots", ["knowledge_base_id", "status"])
    op.create_table(
        "code_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("code_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("parse_status", sa.String(20), nullable=False),
        sa.Column("parse_errors_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("snapshot_id", "path", name="uq_code_file_snapshot_path"),
    )
    op.create_index("ix_code_files_kb_language", "code_files", ["knowledge_base_id", "language"])
    op.create_table(
        "code_symbols",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("code_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_symbol_id", sa.String(36), sa.ForeignKey("code_symbols.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stable_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("snapshot_id", "stable_key", name="uq_code_symbol_snapshot_key"),
    )
    op.create_index("ix_code_symbols_kb_name", "code_symbols", ["knowledge_base_id", "name"])
    op.create_index("ix_code_symbols_snapshot_kind", "code_symbols", ["snapshot_id", "kind"])
    op.create_table(
        "code_relations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("code_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_symbol_id", sa.String(36), sa.ForeignKey("code_symbols.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_symbol_id", sa.String(36), sa.ForeignKey("code_symbols.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_qualified_name", sa.Text(), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("resolution_status", sa.String(20), nullable=False),
        sa.Column("evidence_path", sa.Text(), nullable=False),
        sa.Column("evidence_start_line", sa.Integer(), nullable=False),
        sa.Column("evidence_end_line", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_code_relations_snapshot_type", "code_relations", ["snapshot_id", "relation_type"])
    op.create_index("ix_code_relations_subject", "code_relations", ["subject_symbol_id"])
    op.create_index("ix_code_relations_target", "code_relations", ["target_symbol_id"])
    op.create_table(
        "entity_vector_index_state",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_name", sa.String(255), nullable=True),
        sa.Column("embedding_fingerprint", sa.String(512), nullable=False),
        sa.Column("graph_fingerprint", sa.String(64), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("knowledge_base_id", name="uq_entity_vector_state_kb"),
    )
    op.create_table(
        "graph_schema_definitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("entity_types_json", sa.JSON(), nullable=False),
        sa.Column("relation_types_json", sa.JSON(), nullable=False),
        sa.Column("normalization_rules_json", sa.JSON(), nullable=True),
        sa.Column("extraction_prompt", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("knowledge_base_id", "version", name="uq_graph_schema_kb_version"),
    )
    op.create_table(
        "graph_extraction_cache",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_fingerprint", sa.String(512), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("knowledge_base_id", "cache_key", name="uq_graph_cache_kb_key"),
    )
    op.create_index("ix_graph_cache_last_used", "graph_extraction_cache", ["last_used_at"])
    op.create_table(
        "graph_quality_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("graph_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("gate_results_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_graph_quality_kb_created", "graph_quality_reports", ["knowledge_base_id", "created_at"])
    op.create_table(
        "kg_entity_merges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_entity_id", sa.String(36), sa.ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("merged_entity_id", sa.String(36), sa.ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reverted_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("knowledge_base_id", "merged_entity_id", name="uq_kg_entity_merge_source"),
    )
    op.create_index("ix_kg_entity_merge_canonical", "kg_entity_merges", ["canonical_entity_id"])
    op.create_table(
        "graph_review_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reviewed_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_graph_review_kb_status", "graph_review_items", ["knowledge_base_id", "status"])
    op.create_table(
        "graph_communities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("graph_communities.id", ondelete="CASCADE"), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_graph_communities_kb_level", "graph_communities", ["knowledge_base_id", "level"])
    op.create_table(
        "graph_community_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("community_id", sa.String(36), sa.ForeignKey("graph_communities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.String(36), sa.ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("community_id", "entity_id", name="uq_graph_community_member"),
    )


def downgrade() -> None:
    for table in [
        "graph_community_members", "graph_communities", "graph_review_items",
        "kg_entity_merges", "graph_quality_reports", "graph_extraction_cache",
        "graph_schema_definitions", "entity_vector_index_state", "code_relations",
        "code_symbols", "code_files", "code_snapshots",
    ]:
        op.drop_table(table)
    with op.batch_alter_table("knowledge_data_sources") as batch:
        batch.drop_index("ix_knowledge_data_sources_next_sync_at")
        batch.drop_column("sync_cursor_json")
        batch.drop_column("deletion_policy")
        batch.drop_column("next_sync_at")
        batch.drop_column("sync_interval_minutes")
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.alter_column(
            "job_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
