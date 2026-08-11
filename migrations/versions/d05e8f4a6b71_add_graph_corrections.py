"""add auditable knowledge graph correction overlays

Revision ID: d05e8f4a6b71
Revises: c94a7d3e5f60
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d05e8f4a6b71"
down_revision: Union[str, None] = "c94a7d3e5f60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kg_graph_corrections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("reverted_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reverted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kg_graph_corrections_knowledge_base_id",
        "kg_graph_corrections",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_kg_graph_corrections_kb_status",
        "kg_graph_corrections",
        ["knowledge_base_id", "status"],
    )
    op.create_index(
        "ix_kg_graph_corrections_target",
        "kg_graph_corrections",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_graph_corrections_target", table_name="kg_graph_corrections")
    op.drop_index("ix_kg_graph_corrections_kb_status", table_name="kg_graph_corrections")
    op.drop_index("ix_kg_graph_corrections_knowledge_base_id", table_name="kg_graph_corrections")
    op.drop_table("kg_graph_corrections")
