"""Persist a target-deployment identity for safe import resumption."""

from alembic import op
import sqlalchemy as sa

revision = "h4f6a8b0c213"
down_revision = "h3e5f7a9b102"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("server_deployment_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))


def downgrade():
    op.drop_table("server_deployment_state")
