"""add schema induction provenance metadata

Revision ID: e7a1c9d4b2f0
Revises: c3f8d1a6b9e2
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a1c9d4b2f0"
down_revision: Union[str, None] = "c3f8d1a6b9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("graph_schema_definitions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "generation_method",
                sa.String(length=20),
                nullable=False,
                server_default="manual",
            )
        )
        batch_op.add_column(sa.Column("source_document_ids_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("generation_summary_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("graph_schema_definitions", schema=None) as batch_op:
        batch_op.drop_column("generation_summary_json")
        batch_op.drop_column("source_document_ids_json")
        batch_op.drop_column("generation_method")
