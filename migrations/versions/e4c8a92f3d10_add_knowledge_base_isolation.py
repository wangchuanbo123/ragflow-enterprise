"""add knowledge base isolation

Revision ID: e4c8a92f3d10
Revises: d92a54d331c7
Create Date: 2026-08-10
"""

from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "e4c8a92f3d10"
down_revision: Union[str, None] = "d92a54d331c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_KB_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_SOURCE_ID = "00000000-0000-0000-0000-000000000002"
NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _kb_column() -> sa.Column:
    return sa.Column(
        "knowledge_base_id",
        sa.String(length=36),
        server_default=DEFAULT_KB_ID,
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_knowledge_bases_slug"),
    )
    op.create_index("ix_knowledge_bases_slug", "knowledge_bases", ["slug"])
    op.create_index("ix_knowledge_bases_owner_id", "knowledge_bases", ["owner_id"])

    op.create_table(
        "knowledge_base_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "user_id", name="uq_knowledge_base_member"
        ),
    )
    op.create_index(
        "ix_knowledge_base_members_kb",
        "knowledge_base_members",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_knowledge_base_members_user",
        "knowledge_base_members",
        ["user_id"],
    )

    op.create_table(
        "knowledge_data_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("connection_ref", sa.String(length=255), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "name", name="uq_knowledge_data_source_name"
        ),
    )
    op.create_index(
        "ix_knowledge_data_sources_kb",
        "knowledge_data_sources",
        ["knowledge_base_id"],
    )

    op.execute(
        sa.text(
            "INSERT INTO knowledge_bases "
            "(id, name, slug, is_default, is_active) "
            "VALUES (:id, :name, :slug, :is_default, :is_active)"
        ).bindparams(
            id=DEFAULT_KB_ID,
            name="默认知识库",
            slug="default",
            is_default=True,
            is_active=True,
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO knowledge_data_sources "
            "(id, knowledge_base_id, source_type, name, status) "
            "VALUES (:id, :kb_id, :source_type, :name, :status)"
        ).bindparams(
            id=DEFAULT_SOURCE_ID,
            kb_id=DEFAULT_KB_ID,
            source_type="upload",
            name="本地上传",
            status="active",
        )
    )

    source_path_constraint = "uq_knowledge_documents_source_path"
    if op.get_bind().dialect.name == "postgresql":
        # Releases created before explicit naming used PostgreSQL's generated
        # name. SQLite's batch naming convention already handles this case.
        source_path_constraint = next(
            item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints("knowledge_documents")
            if item["column_names"] == ["source_path"]
        )
    with op.batch_alter_table(
        "knowledge_documents", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.add_column(_kb_column())
        batch_op.drop_constraint(
            source_path_constraint, type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_knowledge_documents_kb_source",
            ["knowledge_base_id", "source_path"],
        )
        batch_op.create_foreign_key(
            "fk_knowledge_documents_kb",
            "knowledge_bases",
            ["knowledge_base_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_knowledge_documents_knowledge_base_id",
            ["knowledge_base_id"],
        )

    for table_name in ("knowledge_chunks", "index_jobs", "index_job_items"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(_kb_column())
            batch_op.create_foreign_key(
                f"fk_{table_name}_kb",
                "knowledge_bases",
                ["knowledge_base_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.create_index(
                f"ix_{table_name}_knowledge_base_id",
                ["knowledge_base_id"],
            )

    with op.batch_alter_table("knowledge_index_state") as batch_op:
        batch_op.add_column(_kb_column())
        batch_op.create_foreign_key(
            "fk_knowledge_index_state_kb",
            "knowledge_bases",
            ["knowledge_base_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_knowledge_index_state_kb", ["knowledge_base_id"]
        )
        batch_op.create_index(
            "ix_knowledge_index_state_knowledge_base_id",
            ["knowledge_base_id"],
        )

    bind = op.get_bind()
    users = bind.execute(sa.text("SELECT id, role FROM users")).mappings().all()
    first_admin_id: str | None = None
    member_table = sa.table(
        "knowledge_base_members",
        sa.column("id", sa.String()),
        sa.column("knowledge_base_id", sa.String()),
        sa.column("user_id", sa.String()),
        sa.column("role", sa.String()),
    )
    member_rows = []
    for user in users:
        is_admin = user["role"] == "admin"
        if is_admin and first_admin_id is None:
            first_admin_id = user["id"]
        member_rows.append(
            {
                "id": str(uuid.uuid4()),
                "knowledge_base_id": DEFAULT_KB_ID,
                "user_id": user["id"],
                "role": "owner" if is_admin else "viewer",
            }
        )
    if member_rows:
        op.bulk_insert(member_table, member_rows)
    if first_admin_id:
        bind.execute(
            sa.text(
                "UPDATE knowledge_bases SET owner_id = :owner_id WHERE id = :kb_id"
            ),
            {"owner_id": first_admin_id, "kb_id": DEFAULT_KB_ID},
        )


def downgrade() -> None:
    with op.batch_alter_table("knowledge_index_state") as batch_op:
        batch_op.drop_index("ix_knowledge_index_state_knowledge_base_id")
        batch_op.drop_constraint("uq_knowledge_index_state_kb", type_="unique")
        batch_op.drop_constraint("fk_knowledge_index_state_kb", type_="foreignkey")
        batch_op.drop_column("knowledge_base_id")

    for table_name in ("index_job_items", "index_jobs", "knowledge_chunks"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_knowledge_base_id")
            batch_op.drop_constraint(f"fk_{table_name}_kb", type_="foreignkey")
            batch_op.drop_column("knowledge_base_id")

    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.drop_index("ix_knowledge_documents_knowledge_base_id")
        batch_op.drop_constraint("fk_knowledge_documents_kb", type_="foreignkey")
        batch_op.drop_constraint("uq_knowledge_documents_kb_source", type_="unique")
        batch_op.create_unique_constraint(
            "uq_knowledge_documents_source_path", ["source_path"]
        )
        batch_op.drop_column("knowledge_base_id")

    op.drop_index("ix_knowledge_data_sources_kb", table_name="knowledge_data_sources")
    op.drop_table("knowledge_data_sources")
    op.drop_index("ix_knowledge_base_members_user", table_name="knowledge_base_members")
    op.drop_index("ix_knowledge_base_members_kb", table_name="knowledge_base_members")
    op.drop_table("knowledge_base_members")
    op.drop_index("ix_knowledge_bases_owner_id", table_name="knowledge_bases")
    op.drop_index("ix_knowledge_bases_slug", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")

