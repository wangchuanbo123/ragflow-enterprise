"""Versioned, tenant-scoped PostgreSQL vector collections.

Revision ID: h3e5f7a9b102
Revises: h2d4c6e8f901
"""

from alembic import op
import sqlalchemy as sa

revision = "h3e5f7a9b102"
down_revision = "h2d4c6e8f901"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    from pgvector.sqlalchemy import VECTOR
    from sqlalchemy.dialects.postgresql import JSONB, UUID

    # A shared extension must not accidentally be installed inside a temporary
    # tenant/test search_path schema and disappear when that schema is removed.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_table("pgvector_collections",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("index_version", sa.String(255)),
        sa.Column("embedding_profile_id", sa.String(255)),
        sa.Column("dimension", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("knowledge_base_id", "name", name="uq_pgvector_collection_kb_name"),
        sa.CheckConstraint("dimension IS NULL OR (dimension >= 1 AND dimension <= 16000)", name="ck_pgvector_collection_dimension"),
    )
    op.create_table("pgvector_embeddings",
        sa.Column("collection_id", UUID(as_uuid=False), sa.ForeignKey("pgvector_collections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
    )
    op.create_index("ix_pgvector_embeddings_metadata", "pgvector_embeddings", ["metadata"], postgresql_using="gin")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.drop_table("pgvector_embeddings")
        op.drop_table("pgvector_collections")
    # The extension may be used by another schema; never drop it implicitly.
