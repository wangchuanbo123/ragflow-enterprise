"""Persist knowledge lifecycle workflows and audit events.

Revision ID: g18a9c7d6e42
Revises: f07d6a1b3c92
"""

from alembic import op
import sqlalchemy as sa

revision = "g18a9c7d6e42"
down_revision = "f07d6a1b3c92"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("index_jobs.id", ondelete="SET NULL")),
        sa.Column("parent_run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("task", sa.String(40), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        *[sa.Column(name, sa.JSON(), nullable=False) for name in ("document_ids_json", "options_json", "context_json", "output_json", "usage_json", "progress_json")],
        sa.Column("backend", sa.String(80)), sa.Column("model", sa.String(255)), sa.Column("session_id", sa.String(255)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"), sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime()), sa.Column("finished_at", sa.DateTime()),
    )
    for name in ("knowledge_base_id", "requested_by", "job_id", "status"):
        op.create_index(f"ix_agent_runs_{name}", "agent_runs", [name])
    op.create_table("agent_run_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("type", sa.String(60), nullable=False),
        sa.Column("message", sa.Text(), nullable=False), sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_run_event_sequence"),
    )
    for name in ("knowledge_base_id", "run_id"):
        op.create_index(f"ix_agent_run_events_{name}", "agent_run_events", [name])


def downgrade():
    op.drop_table("agent_run_events")
    op.drop_table("agent_runs")
