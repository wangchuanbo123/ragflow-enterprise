"""add data source sync state

Revision ID: f27a6b8c9d03
Revises: e16f9a5b7c82
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "f27a6b8c9d03"
down_revision = "e16f9a5b7c82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_data_sources") as batch_op:
        batch_op.add_column(sa.Column("last_sync_status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("last_sync_detail", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("last_sync_job_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("last_synced_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_data_sources") as batch_op:
        batch_op.drop_column("last_synced_at")
        batch_op.drop_column("last_sync_job_id")
        batch_op.drop_column("last_sync_detail")
        batch_op.drop_column("last_sync_status")
