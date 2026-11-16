"""Transaction versions for complete graph snapshot caches."""

from alembic import op
import sqlalchemy as sa

revision = "j1a2b3c4d5e6"
down_revision = "i2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_graph_revisions",
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("incarnation", sa.String(32), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"))
    from app.services.graph_revision import install_revision_triggers
    install_revision_triggers(op.get_bind())


def downgrade():
    from app.services.graph_revision import GRAPH_TABLES
    connection = op.get_bind()
    names = set(sa.inspect(connection).get_table_names())
    quote = connection.dialect.identifier_preparer.quote
    for name in GRAPH_TABLES:
        if name not in names:
            continue
        if connection.dialect.name == "postgresql":
            op.execute(f"DROP TRIGGER IF EXISTS graph_revision_changed ON {quote(name)}")
            op.execute(f"DROP TRIGGER IF EXISTS graph_revision_truncated ON {quote(name)}")
            for action in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS graph_revision_{action} ON {quote(name)}")
        elif connection.dialect.name == "sqlite":
            for action in ("insert", "update", "delete", "move"):
                op.execute(f"DROP TRIGGER IF EXISTS {quote(f'graph_revision_{name}_{action}')}")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS bump_knowledge_graph_revision()")
    op.drop_table("knowledge_graph_revisions")
