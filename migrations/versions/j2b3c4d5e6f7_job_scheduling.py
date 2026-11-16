"""Persist task priorities and index scheduling decisions."""
from alembic import op
import sqlalchemy as sa

revision = "j2b3c4d5e6f7"
down_revision = "j1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("index_jobs", sa.Column("priority", sa.Integer(), nullable=False, server_default="50"))
    table = sa.table("index_jobs", sa.column("job_type", sa.String()), sa.column("priority", sa.Integer()))
    priorities = {"delete": 0, "file": 10, "connector_sync": 20, "sync": 20,
                  "schema_generation": 30, "schema_drift": 30, "agent_workflow": 30,
                  "entity_vector_rebuild": 40, "relation_vector_rebuild": 40,
                  "code_vector_rebuild": 40, "rebuild": 60, "graph_maintenance": 70}
    for kind, priority in priorities.items():
        op.execute(table.update().where(table.c.job_type == kind).values(priority=priority))
    op.create_index("ix_index_jobs_schedule", "index_jobs", ["status", "priority", "created_at"])


def downgrade():
    op.drop_index("ix_index_jobs_schedule", table_name="index_jobs")
    op.drop_column("index_jobs", "priority")
