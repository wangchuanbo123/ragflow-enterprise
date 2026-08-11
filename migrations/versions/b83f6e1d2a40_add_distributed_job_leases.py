"""add distributed job leases and progress events

Revision ID: b83f6e1d2a40
Revises: a72e5d4c9b30
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b83f6e1d2a40"
down_revision: Union[str, None] = "a72e5d4c9b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False)
        )
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.create_index("ix_index_jobs_lease_owner", ["lease_owner"])
        batch_op.create_index("ix_index_jobs_lease_expires_at", ["lease_expires_at"])

    op.create_table(
        "index_job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("processed_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["index_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_index_job_events_job_id", "index_job_events", ["job_id"])
    op.create_index(
        "ix_index_job_events_knowledge_base_id",
        "index_job_events",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_index_job_events_created_at",
        "index_job_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_index_job_events_created_at", table_name="index_job_events")
    op.drop_index(
        "ix_index_job_events_knowledge_base_id",
        table_name="index_job_events",
    )
    op.drop_index("ix_index_job_events_job_id", table_name="index_job_events")
    op.drop_table("index_job_events")

    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.drop_index("ix_index_jobs_lease_expires_at")
        batch_op.drop_index("ix_index_jobs_lease_owner")
        batch_op.drop_column("cancel_requested")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("attempt_count")
