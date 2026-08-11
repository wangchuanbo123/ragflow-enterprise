"""add document folders tags and versions

Revision ID: a72e5d4c9b30
Revises: f19c2b7a4e61
Create Date: 2026-08-10
"""

from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "a72e5d4c9b30"
down_revision: Union[str, None] = "f19c2b7a4e61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_folders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["knowledge_folders.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "path", name="uq_knowledge_folder_path"
        ),
    )
    op.create_index("ix_knowledge_folders_kb", "knowledge_folders", ["knowledge_base_id"])

    op.create_table(
        "document_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "name", name="uq_document_tag_name"
        ),
    )
    op.create_index("ix_document_tags_kb", "document_tags", ["knowledge_base_id"])

    op.create_table(
        "document_tag_links",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["document_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id", "tag_id"),
    )
    op.create_index(
        "ix_document_tag_links_knowledge_base_id",
        "document_tag_links",
        ["knowledge_base_id"],
    )

    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.add_column(sa.Column("folder_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("current_version_no", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("metadata_json", sa.JSON(), nullable=True))
        batch_op.create_foreign_key(
            "fk_knowledge_documents_folder",
            "knowledge_folders",
            ["folder_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_knowledge_documents_folder_id", ["folder_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "version_no", name="uq_document_version_number"
        ),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_kb", "document_versions", ["knowledge_base_id"])

    bind = op.get_bind()
    documents = bind.execute(
        sa.text(
            "SELECT id, knowledge_base_id, source_path, file_hash, file_size, mime_type "
            "FROM knowledge_documents"
        )
    ).mappings().all()
    version_table = sa.table(
        "document_versions",
        sa.column("id", sa.String()),
        sa.column("knowledge_base_id", sa.String()),
        sa.column("document_id", sa.String()),
        sa.column("version_no", sa.Integer()),
        sa.column("object_key", sa.Text()),
        sa.column("file_hash", sa.String()),
        sa.column("file_size", sa.BigInteger()),
        sa.column("mime_type", sa.String()),
        sa.column("status", sa.String()),
    )
    if documents:
        op.bulk_insert(
            version_table,
            [
                {
                    "id": str(uuid.uuid4()),
                    "knowledge_base_id": row["knowledge_base_id"],
                    "document_id": row["id"],
                    "version_no": 1,
                    "object_key": row["source_path"],
                    "file_hash": row["file_hash"],
                    "file_size": row["file_size"],
                    "mime_type": row["mime_type"],
                    "status": "active",
                }
                for row in documents
            ],
        )


def downgrade() -> None:
    op.drop_index("ix_document_versions_kb", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")

    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.drop_index("ix_knowledge_documents_folder_id")
        batch_op.drop_constraint("fk_knowledge_documents_folder", type_="foreignkey")
        batch_op.drop_column("metadata_json")
        batch_op.drop_column("current_version_no")
        batch_op.drop_column("folder_id")

    op.drop_index(
        "ix_document_tag_links_knowledge_base_id",
        table_name="document_tag_links",
    )
    op.drop_table("document_tag_links")
    op.drop_index("ix_document_tags_kb", table_name="document_tags")
    op.drop_table("document_tags")
    op.drop_index("ix_knowledge_folders_kb", table_name="knowledge_folders")
    op.drop_table("knowledge_folders")

