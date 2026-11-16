"""Keep active-schema uniqueness partial on PostgreSQL as on SQLite.

Revision ID: h2d4c6e8f901
Revises: g18a9c7d6e42
"""

from alembic import op
import sqlalchemy as sa

revision = "h2d4c6e8f901"
down_revision = "g18a9c7d6e42"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("uq_graph_schema_one_active_per_kind", table_name="graph_schema_definitions")
        op.create_index("uq_graph_schema_one_active_per_kind", "graph_schema_definitions",
                        ["knowledge_base_id", "graph_kind"], unique=True,
                        postgresql_where=sa.text("status = 'active'"))


def downgrade():
    # The preceding migrations now express this invariant correctly too.
    # Keep the valid partial index when moving back across this repair marker.
    pass
