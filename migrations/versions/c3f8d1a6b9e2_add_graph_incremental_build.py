"""add graph incremental build support

Revision ID: c3f8d1a6b9e2
Revises: f6b2c3d4e5a7
Create Date: 2026-08-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f8d1a6b9e2"
down_revision: Union[str, None] = "f6b2c3d4e5a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("visible_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_index_jobs_visible_at", ["visible_at"])

    op.create_table(
        "graph_community_states",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "knowledge_base_id",
            sa.String(length=36),
            sa.ForeignKey(
                "knowledge_bases.id",
                name="fk_graph_community_states_kb",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="empty"),
        sa.Column("communities", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entities", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("built_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", name="uq_graph_community_state_kb"
        ),
    )


def downgrade() -> None:
    op.drop_table("graph_community_states")
    with op.batch_alter_table("index_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_index_jobs_visible_at")
        batch_op.drop_column("visible_at")
