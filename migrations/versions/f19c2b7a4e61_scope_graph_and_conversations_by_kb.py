"""scope graph and conversations by knowledge base

Revision ID: f19c2b7a4e61
Revises: e4c8a92f3d10
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f19c2b7a4e61"
down_revision: Union[str, None] = "e4c8a92f3d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_KB_ID = "00000000-0000-0000-0000-000000000001"


def _kb_column() -> sa.Column:
    return sa.Column(
        "knowledge_base_id",
        sa.String(length=36),
        server_default=DEFAULT_KB_ID,
        nullable=False,
    )


def _add_kb_column(table_name: str) -> None:
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


def upgrade() -> None:
    _add_kb_column("conversations")
    _add_kb_column("messages")

    graph_constraints = {
        "kg_entities": (
            "uq_kg_entity_type_norm",
            ["knowledge_base_id", "entity_type", "normalized_name"],
        ),
        "kg_aliases": (
            "uq_kg_alias_entity_norm",
            ["knowledge_base_id", "entity_id", "normalized_alias"],
        ),
        "kg_entity_mentions": (
            "uq_kg_mention_entity_chunk",
            ["knowledge_base_id", "entity_id", "chunk_id"],
        ),
        "kg_relations": (
            "uq_kg_relation_triple",
            [
                "knowledge_base_id",
                "subject_entity_id",
                "predicate",
                "object_entity_id",
            ],
        ),
        "kg_relation_evidence": (
            "uq_kg_evidence_rel_chunk_quote",
            ["knowledge_base_id", "relation_id", "chunk_id", "quote_hash"],
        ),
    }
    for table_name, (constraint_name, columns) in graph_constraints.items():
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(_kb_column())
            batch_op.drop_constraint(constraint_name, type_="unique")
            batch_op.create_unique_constraint(constraint_name, columns)
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


def downgrade() -> None:
    old_graph_constraints = {
        "kg_relation_evidence": (
            "uq_kg_evidence_rel_chunk_quote",
            ["relation_id", "chunk_id", "quote_hash"],
        ),
        "kg_relations": (
            "uq_kg_relation_triple",
            ["subject_entity_id", "predicate", "object_entity_id"],
        ),
        "kg_entity_mentions": (
            "uq_kg_mention_entity_chunk",
            ["entity_id", "chunk_id"],
        ),
        "kg_aliases": (
            "uq_kg_alias_entity_norm",
            ["entity_id", "normalized_alias"],
        ),
        "kg_entities": (
            "uq_kg_entity_type_norm",
            ["entity_type", "normalized_name"],
        ),
    }
    for table_name, (constraint_name, columns) in old_graph_constraints.items():
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_knowledge_base_id")
            batch_op.drop_constraint(f"fk_{table_name}_kb", type_="foreignkey")
            batch_op.drop_constraint(constraint_name, type_="unique")
            batch_op.create_unique_constraint(constraint_name, columns)
            batch_op.drop_column("knowledge_base_id")

    for table_name in ("messages", "conversations"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_knowledge_base_id")
            batch_op.drop_constraint(f"fk_{table_name}_kb", type_="foreignkey")
            batch_op.drop_column("knowledge_base_id")

