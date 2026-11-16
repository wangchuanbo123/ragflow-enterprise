"""Consolidate legacy perspectives into one lossless knowledge-base Schema."""

from alembic import op
import sqlalchemy as sa

revision = "i1a2b3c4d5e6"
down_revision = "h4f6a8b0c213"
branch_labels = None
depends_on = None


def upgrade():
    from app.services.schema_consolidation import consolidate_connection, normalize_pending_schema_jobs

    op.drop_index("uq_graph_schema_one_active_per_kind", table_name="graph_schema_definitions")
    consolidate_connection(op.get_bind())
    normalize_pending_schema_jobs(op.get_bind())
    with op.batch_alter_table("graph_schema_definitions") as batch:
        batch.create_check_constraint("ck_graph_schema_active_general", "status <> 'active' OR graph_kind = 'general'")
    op.create_index("uq_graph_schema_one_active_per_kb", "graph_schema_definitions", ["knowledge_base_id"],
                    unique=True, sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))


def downgrade():
    # Consolidated versions are audit history; never delete later user work or
    # reactivate superseded contracts during a binary rollback.
    op.drop_index("uq_graph_schema_one_active_per_kb", table_name="graph_schema_definitions")
    with op.batch_alter_table("graph_schema_definitions") as batch:
        batch.drop_constraint("ck_graph_schema_active_general", type_="check")
    op.create_index("uq_graph_schema_one_active_per_kind", "graph_schema_definitions", ["knowledge_base_id", "graph_kind"],
                    unique=True, sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
