"""add relation and code vector index states

Revision ID: f6b2c3d4e5a7
Revises: a38c7d9e0f14
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "f6b2c3d4e5a7"
down_revision = "a38c7d9e0f14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relation_vector_index_state",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_base_id",
            sa.String(36),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("index_name", sa.String(255), nullable=True),
        sa.Column("embedding_fingerprint", sa.String(512), nullable=False),
        sa.Column("graph_fingerprint", sa.String(64), nullable=False),
        sa.Column("relation_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", name="uq_relation_vector_state_kb"
        ),
    )
    op.create_table(
        "code_vector_index_state",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_base_id",
            sa.String(36),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("code_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("index_name", sa.String(255), nullable=True),
        sa.Column("embedding_fingerprint", sa.String(512), nullable=False),
        sa.Column("symbol_fingerprint", sa.String(64), nullable=False),
        sa.Column("symbol_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("knowledge_base_id", name="uq_code_vector_state_kb"),
    )


def downgrade() -> None:
    op.drop_table("code_vector_index_state")
    op.drop_table("relation_vector_index_state")
