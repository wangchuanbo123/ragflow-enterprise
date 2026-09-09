"""add graph profile schemas

Revision ID: c7d94e2a81f0
Revises: fa82b4c6d901
"""

from alembic import op
import sqlalchemy as sa


revision = "c7d94e2a81f0"
down_revision = "fa82b4c6d901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_schema_definitions",
        sa.Column("graph_profiles_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("graph_schema_definitions", "graph_profiles_json")
