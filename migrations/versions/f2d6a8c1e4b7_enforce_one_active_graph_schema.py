"""enforce one active graph schema per knowledge base

Revision ID: f2d6a8c1e4b7
Revises: e7a1c9d4b2f0
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2d6a8c1e4b7"
down_revision: Union[str, None] = "e7a1c9d4b2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Historical databases may contain multiple active rows. Keep the newest
    # version active before installing the database-level invariant.
    op.execute(sa.text("""
        UPDATE graph_schema_definitions AS current
        SET status = 'superseded'
        WHERE current.status = 'active'
          AND EXISTS (
            SELECT 1
            FROM graph_schema_definitions AS newer
            WHERE newer.knowledge_base_id = current.knowledge_base_id
              AND newer.status = 'active'
              AND newer.version > current.version
          )
    """))
    op.create_index(
        "uq_graph_schema_one_active_per_kb",
        "graph_schema_definitions",
        ["knowledge_base_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_graph_schema_one_active_per_kb",
        table_name="graph_schema_definitions",
    )
