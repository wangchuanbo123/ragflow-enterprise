"""add versioned knowledge backups

Revision ID: b91e7c4a2d63
Revises: f2d6a8c1e4b7
"""

from alembic import op
import sqlalchemy as sa

revision = "b91e7c4a2d63"
down_revision = "f2d6a8c1e4b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_backups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("backup_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("restored_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("restored_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["restored_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_backups_scope",
        "knowledge_backups",
        ["knowledge_base_id", "scope_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_backups_scope", table_name="knowledge_backups")
    op.drop_table("knowledge_backups")
