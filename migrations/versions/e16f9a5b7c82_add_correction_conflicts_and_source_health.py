"""add graph correction conflict and data source health state

Revision ID: e16f9a5b7c82
Revises: d05e8f4a6b71
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e16f9a5b7c82"
down_revision: Union[str, None] = "d05e8f4a6b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("kg_graph_corrections") as batch_op:
        batch_op.add_column(sa.Column("conflict_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("checked_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("knowledge_data_sources") as batch_op:
        batch_op.add_column(sa.Column("health_status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("health_detail", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("last_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_data_sources") as batch_op:
        batch_op.drop_column("last_checked_at")
        batch_op.drop_column("health_detail")
        batch_op.drop_column("health_status")
    with op.batch_alter_table("kg_graph_corrections") as batch_op:
        batch_op.drop_column("checked_at")
        batch_op.drop_column("conflict_reason")
