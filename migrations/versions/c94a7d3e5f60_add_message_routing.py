"""persist explainable retrieval routing on chat messages

Revision ID: c94a7d3e5f60
Revises: b83f6e1d2a40
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c94a7d3e5f60"
down_revision: Union[str, None] = "b83f6e1d2a40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "routing_json",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("routing_json")
