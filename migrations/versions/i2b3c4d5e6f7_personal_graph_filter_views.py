"""Persist user-owned natural-language views of the one knowledge-base graph."""

from alembic import op
import sqlalchemy as sa

revision = "i2b3c4d5e6f7"
down_revision = "i1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("graph_filter_views",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("principles_json", sa.JSON(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("schema_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("knowledge_base_id", "user_id", "name", name="uq_graph_filter_owner_name"))
    op.create_index("ix_graph_filter_views_knowledge_base_id", "graph_filter_views", ["knowledge_base_id"])
    op.create_index("ix_graph_filter_views_user_id", "graph_filter_views", ["user_id"])


def downgrade():
    op.drop_index("ix_graph_filter_views_user_id", table_name="graph_filter_views")
    op.drop_index("ix_graph_filter_views_knowledge_base_id", table_name="graph_filter_views")
    op.drop_table("graph_filter_views")
