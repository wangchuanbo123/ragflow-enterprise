"""add independent graph kind schemas

Revision ID: d3f1b8a690ce
Revises: c7d94e2a81f0
"""

from alembic import op
import sqlalchemy as sa


revision = "d3f1b8a690ce"
down_revision = "c7d94e2a81f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("graph_schema_definitions") as batch_op:
        batch_op.add_column(
            sa.Column("graph_kind", sa.String(length=24), nullable=False, server_default="general")
        )
        batch_op.drop_index("uq_graph_schema_one_active_per_kb")
        batch_op.create_index(
            "uq_graph_schema_one_active_per_kind",
            ["knowledge_base_id", "graph_kind"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    # Keep one active schema per knowledge base before restoring the old index.
    op.execute(
        "UPDATE graph_schema_definitions SET status = 'superseded' "
        "WHERE status = 'active' AND graph_kind <> 'general'"
    )
    with op.batch_alter_table("graph_schema_definitions") as batch_op:
        batch_op.drop_index("uq_graph_schema_one_active_per_kind")
        batch_op.drop_column("graph_kind")
        batch_op.create_index(
            "uq_graph_schema_one_active_per_kb",
            ["knowledge_base_id"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )
